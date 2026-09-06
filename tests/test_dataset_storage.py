import sqlite3
from datetime import timedelta

import pytest

from fivecast.market.snapshot import measure_quality
from fivecast.models import MarketResolution, MarketSnapshot, PredictionMarket
from fivecast.storage.sqlite import SCHEMA, SnapshotStore


def create_legacy(path, snapshot):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    values = {"id": 77, **snapshot.model_dump(mode="json")}
    for field in values:
        if field.endswith("_utc"):
            values[field] = getattr(snapshot, field).isoformat(timespec="microseconds")
    values["created_at_utc"] = snapshot.timestamp_utc.isoformat(timespec="microseconds")
    connection.execute(
        f"INSERT INTO snapshots ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )
    connection.commit()
    connection.close()


def test_v0_migration_preserves_rows_ids_decimals_and_unknown_metrics(tmp_path, snapshot):
    path = tmp_path / "legacy.db"
    create_legacy(path, snapshot)
    with SnapshotStore(path) as store:
        assert store.count_snapshots() == 1
        assert store.get_latest_snapshot() == snapshot
        row = store.connection.execute("SELECT * FROM snapshots").fetchone()
        assert row["id"] == 77
        assert row["btc_delta_pct"] == "0.001"
        assert row["poll_latency_ms"] is None
        assert row["is_stale"] is None
        assert row["run_id"] is None
        assert row["source_skew_ms"] == 0
        market = store.get_market(snapshot.market_id)
        assert market["metadata_source"] == "snapshot_backfill"
        assert market["condition_id"] is None
        assert market["resolved"] == 0
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert path.with_name("legacy.db.v0.bak").exists()
    with SnapshotStore(path) as reopened:
        assert reopened.count_snapshots() == 1
        assert reopened.get_latest_snapshot() == snapshot


def test_migration_rolls_back_on_invalid_legacy_data(tmp_path, snapshot):
    path = tmp_path / "legacy.db"
    create_legacy(path, snapshot)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE snapshots SET btc_price='-1'")
    with pytest.raises(ValueError):
        SnapshotStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT btc_price FROM snapshots").fetchone()[0] == "-1"
        assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='markets'").fetchone()
            is None
        )


def test_unknown_schema_version_rejected_without_changes(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported database schema"):
        SnapshotStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_market_hydration_update_and_resolution_preserve_identity(tmp_path, snapshot, market):
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_snapshot(snapshot)
        assert store.get_market(market.market_id)["condition_id"] is None
        store.save_market(market)
        updated = PredictionMarket.model_validate(
            {**market.model_dump(), "question": "Updated title"}
        )
        store.save_market(updated)
        row = store.get_market(market.market_id)
        assert row["question"] == "Updated title"
        assert row["condition_id"] == market.condition_id
        assert row["metadata_source"] == "gamma"
        result = MarketResolution(
            market_id=market.market_id, outcome="UP", observed_at_utc=market.end_time_utc
        )
        assert store.save_resolution(result)
        store.save_market(updated)
        assert store.get_market(market.market_id)["outcome"] == "UP"
        assert store.get_market(market.market_id)["resolved"] == 1
        assert store.get_market(market.market_id)["resolution_time_utc"] is None
        assert store.save_resolution(result) is False
        with pytest.raises(ValueError, match="Conflicting official settlement"):
            store.save_resolution(result.model_copy(update={"outcome": "DOWN"}))
        assert store.get_market(market.market_id)["outcome"] == "UP"


def test_immutable_market_metadata_cannot_be_overwritten(tmp_path, market):
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_market(market)
        changed = PredictionMarket.model_validate({**market.model_dump(), "up_token_id": "999"})
        with pytest.raises(ValueError, match="immutable field"):
            store.save_market(changed)
        assert store.get_market(market.market_id)["up_token_id"] == market.up_token_id


def test_official_resolution_time_and_provenance(tmp_path, market):
    result = MarketResolution(
        market_id=market.market_id,
        outcome="DOWN",
        resolution_time_utc=market.end_time_utc + timedelta(seconds=90),
        resolution_time_source="gamma_closedTime",
        observed_at_utc=market.end_time_utc + timedelta(seconds=120),
    )
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_market(market)
        store.save_resolution(result)
        row = store.get_market(market.market_id)
        assert row["resolution_time_source"] == "gamma_closedTime"
        assert row["resolution_source"] == "polymarket_clob_winner+gamma_final"
        assert row["resolution_time_utc"] != row["resolution_observed_at_utc"]


def test_early_resolution_and_unknown_market_are_rejected(tmp_path, market):
    result = MarketResolution(
        market_id=market.market_id, outcome="UP", observed_at_utc=market.start_time_utc
    )
    with SnapshotStore(tmp_path / "data.db") as store:
        with pytest.raises(ValueError, match="unknown market"):
            store.save_resolution(result)
        store.save_market(market)
        with pytest.raises(ValueError, match="before its window ends"):
            store.save_resolution(result)


def test_closed_market_pending_queue_and_backoff(tmp_path, market):
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_market(market)
        assert store.close_elapsed_markets(market.start_time_utc) == []
        assert store.pending_settlements(market.start_time_utc) == []
        assert store.close_elapsed_markets(market.end_time_utc) == [market.market_id]
        assert store.close_elapsed_markets(market.end_time_utc) == []
        assert len(store.pending_settlements(market.end_time_utc)) == 1
        store.record_settlement_check(market.market_id, market.end_time_utc, 30, failed=True)
        assert store.pending_settlements(market.end_time_utc + timedelta(seconds=29)) == []
        assert len(store.pending_settlements(market.end_time_utc + timedelta(seconds=30))) == 1
        assert store.get_market(market.market_id)["settlement_error_count"] == 1


def test_quality_skew_staleness_and_latency(tmp_path, snapshot):
    staggered = MarketSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "up_timestamp_utc": snapshot.up_timestamp_utc + timedelta(seconds=1),
        }
    )
    fresh = measure_quality(staggered, 123.5, 5)
    stale = measure_quality(staggered, 123.5, 1)
    assert fresh.is_stale is False
    assert stale.is_stale is True
    assert stale.source_skew_ms == 1000
    assert stale.market_source_timestamp == staggered.down_timestamp_utc
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_snapshot(staggered, stale)
        row = store.connection.execute("SELECT * FROM snapshots").fetchone()
        assert row["source_skew_ms"] == 1000
        assert row["poll_latency_ms"] == 123.5
        assert row["is_stale"] == 1


def test_inconsistent_quality_not_persisted(tmp_path, snapshot):
    wrong = measure_quality(snapshot).model_copy(update={"source_skew_ms": 1000})
    with SnapshotStore(tmp_path / "data.db") as store:
        with pytest.raises(ValueError, match="Quality timing"):
            store.save_snapshot(snapshot, wrong)
        assert store.count_snapshots() == 0


def test_snapshots_enforce_market_foreign_key(tmp_path, snapshot):
    with SnapshotStore(tmp_path / "data.db") as store:
        store.save_snapshot(snapshot)
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute("DELETE FROM markets")
        assert store.count_snapshots() == 1
