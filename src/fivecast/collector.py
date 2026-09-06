"""Resilient foreground sampling with an independent read-only settlement worker."""

import logging
import math
import threading
import time
from datetime import timedelta

import httpx

from fivecast.config import Settings
from fivecast.feeds.btc import OpenPriceUnavailable
from fivecast.feeds.http import CollectorStopped, RetryingClient
from fivecast.feeds.polymarket import NoActiveMarket, PolymarketFeed
from fivecast.main import Observer
from fivecast.market.snapshot import SnapshotUnavailable
from fivecast.models import utc_now
from fivecast.storage.sqlite import SnapshotStore

logger = logging.getLogger("fivecast")


def settle_pending(
    store: SnapshotStore, feed: PolymarketFeed, settings: Settings, stop: threading.Event
) -> None:
    for row in store.pending_settlements(utc_now()):
        if stop.is_set():
            return
        failed = False
        try:
            market = feed.get_market_by_slug(row["slug"])
            if market.market_id != row["market_id"]:
                raise ValueError("Settlement metadata ID does not match the persisted market")
            store.save_market(market)
            resolution = feed.get_resolution(market)
            if resolution is not None and store.save_resolution(resolution):
                logger.info("RESOLVED market=%s outcome=%s", market.market_id, resolution.outcome)
        except CollectorStopped:
            return
        except (httpx.RequestError, httpx.HTTPStatusError, NoActiveMarket) as error:
            failed = True
            logger.warning("Settlement unavailable market=%s: %s", row["market_id"], error)
        except ValueError as error:
            failed = True
            logger.error("Settlement rejected market=%s: %s", row["market_id"], error)
        store.record_settlement_check(
            row["market_id"], utc_now(), settings.settlement_poll_interval_seconds, failed
        )


class SettlementWorker(threading.Thread):
    def __init__(self, settings: Settings, stop: threading.Event):
        super().__init__(name="fivecast-settlement", daemon=False)
        self.settings = settings
        self.stop = stop
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            # Each thread owns its client and SQLite connection. No network call
            # holds a database transaction; WAL keeps report reads independent.
            with (
                RetryingClient(self.settings, self.stop) as client,
                SnapshotStore(self.settings.db_path) as store,
            ):
                while not self.stop.is_set():
                    settle_pending(store, PolymarketFeed(client), self.settings, self.stop)
                    self.stop.wait(self.settings.settlement_poll_interval_seconds)
        except Exception as error:
            # This thread boundary transfers fatal errors to the owning thread;
            # it never retries or suppresses unexpected programming/storage failures.
            self.error = error
            logger.exception("Settlement worker stopped unexpectedly")


class Collector:
    def __init__(self, observer: Observer, worker: SettlementWorker | None = None):
        self.observer = observer
        self.store = observer.store
        self.settings = observer.settings
        self.worker = worker
        self.attempts = 0
        self.saved = 0
        self.failed = 0
        self.interrupted = 0

    def run(self, iterations: int | None = None, duration: float | None = None) -> None:
        started = utc_now()
        monotonic_start = time.monotonic()
        run_id = self.store.start_run(started, self.settings.interval_seconds)
        self.observer.run_id = run_id
        self.observer.verbose = False
        slot = 0
        try:
            while iterations is None or self.attempts < iterations:
                if duration is not None and time.monotonic() - monotonic_start >= duration:
                    break
                if self.worker is not None and not self.worker.is_alive():
                    raise RuntimeError(
                        "Settlement worker is no longer running"
                    ) from self.worker.error
                now = utc_now()
                for market_id in self.store.close_elapsed_markets(now):
                    logger.info("WINDOW CLOSED market=%s", market_id)
                scheduled = started + timedelta(seconds=slot * self.settings.interval_seconds)
                poll_id = self.store.start_poll(run_id, scheduled, now)
                poll_started = time.monotonic()
                failure_kind = "unexpected_collector_error"
                market_id = None
                self.attempts += 1
                try:
                    snapshot = self.observer.observe()
                    market_id = snapshot.market_id
                    self.saved += int(self.observer.last_inserted)
                    failure_kind = None
                except (NoActiveMarket, OpenPriceUnavailable, SnapshotUnavailable) as error:
                    failure_kind = type(error).__name__
                    logger.warning("Observation skipped: %s", error)
                except (httpx.RequestError, httpx.HTTPStatusError) as error:
                    failure_kind = type(error).__name__
                    logger.warning("Public feed poll failed after bounded retries: %s", error)
                except ValueError as error:
                    failure_kind = type(error).__name__
                    logger.error("Invalid observation rejected: %s", error)
                except KeyboardInterrupt:
                    failure_kind = "KeyboardInterrupt"
                    self.interrupted += 1
                    raise
                finally:
                    if failure_kind is not None and failure_kind != "KeyboardInterrupt":
                        self.failed += 1
                    current = self.observer.current_market
                    if (
                        market_id is None
                        and current is not None
                        and current.start_time_utc <= now < current.end_time_utc
                    ):
                        market_id = current.market_id
                    self.store.finish_poll(
                        poll_id,
                        utc_now(),
                        (time.monotonic() - poll_started) * 1000,
                        failure_kind,
                        market_id,
                    )
                if iterations is not None and self.attempts >= iterations:
                    break
                elapsed = time.monotonic() - monotonic_start
                # Missed schedule slots stay missing. Never issue a catch-up burst.
                slot = max(slot + 1, math.ceil(elapsed / self.settings.interval_seconds))
                wait = max(0, slot * self.settings.interval_seconds - elapsed)
                if duration is not None:
                    wait = min(wait, max(0, duration - elapsed))
                time.sleep(wait)
        finally:
            end = utc_now()
            if duration is not None:
                # No polls are scheduled at/after the requested duration, even if
                # sleep or an in-flight request finishes slightly past that deadline.
                end = min(end, started + timedelta(seconds=duration))
            self.store.finish_run(run_id, end)
            logger.info(
                "COLLECTION SUMMARY run=%s attempted=%s saved=%s failed=%s interrupted=%s",
                run_id,
                self.attempts,
                self.saved,
                self.failed,
                self.interrupted,
            )


def collect(
    settings: Settings, iterations: int | None = None, duration: float | None = None
) -> None:
    stop = threading.Event()
    with (
        SnapshotStore(settings.db_path) as store,
        RetryingClient(settings, stop) as client,
    ):
        worker = SettlementWorker(settings, stop)
        worker.start()
        try:
            Collector(Observer(client, store, settings), worker).run(iterations, duration)
        except KeyboardInterrupt:
            logger.info("Shutdown requested; finishing database work and closing clients")
        finally:
            stop.set()
            worker.join()
        if worker.error is not None:
            raise RuntimeError("Settlement worker failed") from worker.error
