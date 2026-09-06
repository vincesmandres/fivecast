"""Local read-only observer: discover, validate, synchronize, print and persist."""

import argparse
import logging
import time
from collections.abc import Sequence
from pathlib import Path

import httpx

from fivecast.config import Settings, load_settings
from fivecast.feeds.btc import BTCFeed, OpenPriceUnavailable
from fivecast.feeds.polymarket import NoActiveMarket, PolymarketFeed
from fivecast.market.snapshot import SnapshotUnavailable, build_snapshot, measure_quality
from fivecast.models import MarketSnapshot, PredictionMarket, WindowOpen, utc_now
from fivecast.storage.sqlite import SnapshotStore

logger = logging.getLogger("fivecast")


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s UTC %(levelname)s %(name)s: %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def format_snapshot(snapshot: MarketSnapshot) -> str:
    def probability(value: object) -> str:
        return "N/A" if value is None else str(value)

    return (
        f"[{snapshot.timestamp_utc:%Y-%m-%d %H:%M:%S} UTC]\n"
        f"Market      BTC 5m UP/DOWN ({snapshot.market_id})\n"
        f"Window      {snapshot.market_start_utc:%H:%M:%S} -> "
        f"{snapshot.market_end_utc:%H:%M:%S} UTC\n"
        f"Remaining   {snapshot.seconds_remaining:.1f}s\n\n"
        f"BTC open    ${snapshot.btc_window_open_price:,.2f} (Coinbase 1m candle open)\n"
        f"BTC now     ${snapshot.btc_price:,.2f}\n"
        f"Delta       ${snapshot.btc_delta_usd:+,.2f} ({snapshot.btc_delta_pct:+.4%})\n\n"
        f"UP          bid {probability(snapshot.up_bid)}  ask {probability(snapshot.up_ask)}"
        f"  spread {probability(snapshot.up_spread)}\n"
        f"DOWN        bid {probability(snapshot.down_bid)}  ask {probability(snapshot.down_ask)}"
        f"  spread {probability(snapshot.down_spread)}\n"
        f"Sources     {snapshot.source_btc} / {snapshot.source_prediction_market}"
    )


class Observer:
    def __init__(self, client: httpx.Client, store: SnapshotStore, settings: Settings):
        self.btc = BTCFeed(client)
        self.polymarket = PolymarketFeed(client)
        self.store = store
        self.settings = settings
        self.opening: WindowOpen | None = None
        self.current_market: PredictionMarket | None = None
        self.run_id: int | None = None
        self.verbose = True
        self.last_inserted = False

    def observe(self) -> MarketSnapshot:
        started = time.monotonic()
        market = self.polymarket.discover_market(utc_now())
        self.store.save_market(market)
        if self.current_market is None or self.current_market.market_id != market.market_id:
            logger.info("DISCOVERED market=%s slug=%s", market.market_id, market.slug)
        self.current_market = market
        if self.opening is None or self.opening.window_start_utc != market.start_time_utc:
            self.opening = self.btc.get_window_open(market.start_time_utc)
        btc = self.btc.get_quote()
        up = self.polymarket.get_quote(market, "UP")
        down = self.polymarket.get_quote(market, "DOWN")
        timestamp = utc_now()
        lead = (
            max(btc.timestamp_utc, up.timestamp_utc, down.timestamp_utc) - timestamp
        ).total_seconds()
        # Public server clocks can lead the host by milliseconds. Wait, rather than
        # backdating inputs or admitting future observations into a snapshot.
        if 0 < lead <= 1:
            logger.debug("Waiting %.3fs for a small source clock lead", lead)
            time.sleep(lead)
            timestamp = utc_now()
        snapshot = build_snapshot(market, btc, self.opening, up, down, timestamp, self.settings)
        quality = measure_quality(
            snapshot, (time.monotonic() - started) * 1000, self.settings.interval_seconds
        )
        inserted = self.store.save_snapshot(snapshot, quality, self.run_id)
        self.last_inserted = inserted
        if self.verbose:
            print(format_snapshot(snapshot), flush=True)
            print(
                "Snapshot saved.\n" if inserted else "Identical snapshot already stored.\n",
                flush=True,
            )
        else:
            logger.info(
                "SNAPSHOT market=%s remaining=%.1fs delta=%s skew_ms=%.1f latency_ms=%.1f stale=%s",
                snapshot.market_id,
                snapshot.seconds_remaining,
                snapshot.btc_delta_usd,
                quality.source_skew_ms,
                quality.poll_latency_ms,
                quality.is_stale,
            )
        return snapshot

    def run(self, iterations: int | None = None) -> None:
        completed = 0
        while iterations is None or completed < iterations:
            started = time.monotonic()
            try:
                self.observe()
            except (NoActiveMarket, OpenPriceUnavailable, SnapshotUnavailable) as error:
                if iterations is not None:
                    raise
                logger.warning("Observation skipped: %s", error)
            except httpx.RequestError as error:
                if iterations is not None:
                    raise
                logger.warning("Public feed transport failure: %s", error)
            except httpx.HTTPStatusError as error:
                if iterations is not None or (
                    error.response.status_code != 429 and error.response.status_code < 500
                ):
                    raise
                logger.warning("Public feed temporarily unavailable: %s", error)
            completed += 1
            if iterations is None or completed < iterations:
                time.sleep(max(0, self.settings.interval_seconds - (time.monotonic() - started)))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="FiveCast read-only BTC market observer")
    parser.add_argument("mode", nargs="?", choices=["collect"], help="Resilient dataset collection")
    parser.add_argument("--config", type=Path, help="Optional TOML settings file")
    parser.add_argument("--db-path", type=Path, help="Override local SQLite path")
    bounded = parser.add_mutually_exclusive_group()
    bounded.add_argument("--once", action="store_true", help="Store one snapshot, then exit")
    bounded.add_argument("--iterations", type=int, help="Store N snapshots, then exit")
    bounded.add_argument("--duration", type=float, help="Collect for this many seconds")
    args = parser.parse_args(argv)
    if args.iterations is not None and args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.duration is not None:
        import math

        if args.mode != "collect" or not math.isfinite(args.duration) or args.duration <= 0:
            parser.error("--duration requires collect mode and a finite positive value")
    settings = load_settings(args.config)
    if args.db_path is not None:
        settings = Settings.model_validate({**settings.model_dump(), "db_path": args.db_path})
    configure_logging(settings.log_level)
    logger.info("Read-only observer; database=%s", settings.db_path)
    if args.mode == "collect":
        from fivecast.collector import collect

        collect(settings, iterations=1 if args.once else args.iterations, duration=args.duration)
        return
    try:
        with (
            httpx.Client(
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": "FiveCast/0.1 read-only-research"},
                follow_redirects=False,
            ) as client,
            SnapshotStore(settings.db_path) as store,
        ):
            Observer(client, store, settings).run(1 if args.once else args.iterations)
    except KeyboardInterrupt:
        logger.info("Observer stopped; HTTP and SQLite resources closed")


if __name__ == "__main__":
    main()
