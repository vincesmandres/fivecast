import logging
import threading
from datetime import timedelta

import httpx
import pytest

from fivecast.collector import Collector, SettlementWorker, collect, settle_pending
from fivecast.config import Settings
from fivecast.main import Observer
from fivecast.models import MarketResolution
from fivecast.storage.sqlite import SnapshotStore


@pytest.fixture
def clock(inputs, monkeypatch):
    class Clock:
        elapsed = 0.0

        def now(self):
            return inputs["timestamp"] + timedelta(seconds=self.elapsed)

        def sleep(self, seconds):
            self.elapsed += seconds

    value = Clock()
    monkeypatch.setattr("fivecast.collector.utc_now", value.now)
    monkeypatch.setattr("fivecast.main.utc_now", value.now)
    monkeypatch.setattr("fivecast.collector.time.monotonic", lambda: value.elapsed)
    monkeypatch.setattr("fivecast.collector.time.sleep", value.sleep)
    return value


def configure_observer(observer, inputs, monkeypatch):
    monkeypatch.setattr(observer.polymarket, "discover_market", lambda _: inputs["market"])
    monkeypatch.setattr(observer.btc, "get_window_open", lambda _: inputs["opening"])
    monkeypatch.setattr(observer.btc, "get_quote", lambda: inputs["btc"])
    monkeypatch.setattr(observer.polymarket, "get_quote", lambda _, side: inputs[side.lower()])


def test_collector_records_failures_and_continues(tmp_path, inputs, clock, monkeypatch, caplog):
    with httpx.Client() as client, SnapshotStore(tmp_path / "data.db") as store:
        observer = Observer(client, store, Settings())
        configure_observer(observer, inputs, monkeypatch)
        calls = []

        def quote():
            calls.append(1)
            clock.sleep(0.2)
            if len(calls) == 1:
                raise httpx.ReadTimeout("offline simulated timeout")
            return inputs["btc"]

        monkeypatch.setattr(observer.btc, "get_quote", quote)
        collector = Collector(observer)
        collector.run(iterations=2)
        assert collector.failed == 1
        assert collector.saved == 1
        assert store.count_snapshots() == 1
        polls = store.connection.execute("SELECT * FROM polls ORDER BY id").fetchall()
        assert [row["status"] for row in polls] == ["failed", "success"]
        assert polls[0]["failure_kind"] == "ReadTimeout"
        assert polls[0]["market_id"] == inputs["market"].market_id
        assert "bounded retries" in caplog.text


def test_invalid_payload_is_logged_without_snapshot(tmp_path, inputs, clock, monkeypatch, caplog):
    with httpx.Client() as client, SnapshotStore(tmp_path / "data.db") as store:
        observer = Observer(client, store, Settings())
        configure_observer(observer, inputs, monkeypatch)

        def invalid():
            raise ValueError("Malformed public payload")

        monkeypatch.setattr(observer.btc, "get_quote", invalid)
        Collector(observer).run(iterations=2)
        assert store.count_snapshots() == 0
        assert (
            store.connection.execute("SELECT COUNT(*) FROM polls WHERE status='failed'").fetchone()[
                0
            ]
            == 2
        )
        assert "Invalid observation rejected" in caplog.text


def test_slow_polls_skip_slots_without_catchup_burst(tmp_path, inputs, clock, monkeypatch):
    with httpx.Client() as client, SnapshotStore(tmp_path / "data.db") as store:
        observer = Observer(client, store, Settings())

        def slow_failure():
            clock.sleep(12)
            raise httpx.ReadTimeout("slow feed")

        monkeypatch.setattr(observer, "observe", slow_failure)
        Collector(observer).run(iterations=2)
        rows = store.connection.execute("SELECT scheduled_at_utc FROM polls ORDER BY id").fetchall()
        from datetime import datetime

        assert (
            datetime.fromisoformat(rows[1][0]) - datetime.fromisoformat(rows[0][0])
        ).total_seconds() == 15
        assert clock.elapsed == 27


def test_duration_is_bounded_and_shutdown_summarized(tmp_path, inputs, clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="fivecast")
    with httpx.Client() as client, SnapshotStore(tmp_path / "data.db") as store:
        observer = Observer(client, store, Settings())
        configure_observer(observer, inputs, monkeypatch)
        collector = Collector(observer)
        collector.run(duration=11)
        assert collector.attempts == 3
        assert clock.elapsed == 11
        assert store.connection.execute("SELECT stopped_at_utc FROM collection_runs").fetchone()[0]
        assert "COLLECTION SUMMARY" in caplog.text
        assert store.count_snapshots() == 3


def test_interrupt_records_partial_poll_and_finishes_run(tmp_path, inputs, clock, monkeypatch):
    with httpx.Client() as client, SnapshotStore(tmp_path / "data.db") as store:
        observer = Observer(client, store, Settings())

        def interrupt():
            clock.sleep(1)
            raise KeyboardInterrupt

        monkeypatch.setattr(observer, "observe", interrupt)
        with pytest.raises(KeyboardInterrupt):
            Collector(observer).run()
        assert store.connection.execute("SELECT status FROM polls").fetchone()[0] == "interrupted"
        assert store.connection.execute("SELECT stopped_at_utc FROM collection_runs").fetchone()[0]
        assert store.count_snapshots() == 0


def test_settlement_can_be_delayed_without_a_fabricated_label(tmp_path, market, monkeypatch):
    now = market.end_time_utc + timedelta(seconds=10)
    monkeypatch.setattr("fivecast.collector.utc_now", lambda: now)

    class Feed:
        calls = 0

        def get_market_by_slug(self, slug):
            return market

        def get_resolution(self, metadata):
            self.calls += 1
            if self.calls == 1:
                return None
            return MarketResolution(market_id=market.market_id, outcome="DOWN", observed_at_utc=now)

    feed = Feed()
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_market(market)
        settle_pending(store, feed, Settings(), threading.Event())
        assert store.get_market(market.market_id)["resolved"] == 0
        assert store.get_market(market.market_id)["outcome"] is None
        settle_pending(store, feed, Settings(), threading.Event())
        assert feed.calls == 1
        now += timedelta(seconds=30)
        settle_pending(store, feed, Settings(), threading.Event())
        assert store.get_market(market.market_id)["outcome"] == "DOWN"


def test_blocked_settlement_worker_does_not_block_snapshot_storage(tmp_path, snapshot, monkeypatch):
    settings = Settings(db_path=tmp_path / "data.db")
    entered = threading.Event()
    release = threading.Event()
    stop = threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr("fivecast.collector.settle_pending", blocked)
    with SnapshotStore(settings.db_path) as store:
        worker = SettlementWorker(settings, stop)
        worker.start()
        try:
            assert entered.wait(5)
            assert store.save_snapshot(snapshot)
            assert store.count_snapshots() == 1
        finally:
            stop.set()
            release.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker.error is None


def test_collect_ctrl_c_stops_worker_and_closes_store(tmp_path, monkeypatch):
    events = []

    class Worker:
        error = None

        def __init__(self, settings, stop):
            self.stop = stop

        def start(self):
            events.append("started")

        def join(self):
            assert self.stop.is_set()
            events.append("joined")

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr("fivecast.collector.SettlementWorker", Worker)
    monkeypatch.setattr(Collector, "run", interrupt)
    collect(Settings(db_path=tmp_path / "data.db"))
    assert events == ["started", "joined"]
    with SnapshotStore(tmp_path / "data.db") as store:
        assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_unexpected_worker_exception_is_transferred_to_owner(tmp_path, monkeypatch, caplog):
    settings = Settings(db_path=tmp_path / "data.db")

    def broken(*args):
        raise RuntimeError("Unexpected settlement implementation failure")

    monkeypatch.setattr("fivecast.collector.settle_pending", broken)
    with SnapshotStore(settings.db_path):
        worker = SettlementWorker(settings, threading.Event())
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert isinstance(worker.error, RuntimeError)
        assert "stopped unexpectedly" in caplog.text


def test_bounded_collection_cannot_hide_worker_failure(tmp_path, monkeypatch):
    class FailedWorker:
        error = RuntimeError("Worker failed during final foreground poll")

        def __init__(self, *args):
            pass

        def start(self):
            pass

        def join(self):
            pass

    monkeypatch.setattr("fivecast.collector.SettlementWorker", FailedWorker)
    monkeypatch.setattr(Collector, "run", lambda *args: None)
    with pytest.raises(RuntimeError, match="Settlement worker failed"):
        collect(Settings(db_path=tmp_path / "data.db"), iterations=1)


def test_duration_oversleep_does_not_fabricate_an_extra_expected_slot(
    tmp_path, inputs, clock, monkeypatch
):
    from fivecast.report import generate_report

    path = tmp_path / "data.db"
    with httpx.Client() as client, SnapshotStore(path) as store:
        observer = Observer(client, store, Settings())
        configure_observer(observer, inputs, monkeypatch)
        monkeypatch.setattr(
            "fivecast.collector.time.sleep", lambda delay: clock.sleep(delay + 0.01)
        )
        Collector(observer).run(duration=10)
    report = generate_report(path)["global"]
    assert report["snapshots_stored"] == 2
    assert report["expected_sample_count"] == 2
    assert report["coverage_pct"] == 100
