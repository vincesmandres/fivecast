import sqlite3

import pytest

from fivecast.storage.sqlite import SnapshotStore


def run_record() -> dict:
    return {
        "strategy_name": "baseline",
        "strategy_version": "1",
        "parameters_json": '{"threshold":"0.01"}',
        "created_at_utc": "2026-09-06T15:00:00.000000+00:00",
        "dataset_start_utc": "2026-09-01T00:00:00.000000+00:00",
        "dataset_end_utc": "2026-09-06T00:00:00.000000+00:00",
        "split_name": "research",
        "quality_filters_json": '{"max_skew_ms":1000}',
        "source_snapshot_count": 10,
        "eligible_market_count": 2,
        "excluded_market_count": 1,
    }


def trade_record(run_id: int, market_id: str) -> dict:
    return {
        "strategy_run_id": run_id,
        "market_id": market_id,
        "signal_timestamp_utc": "2026-09-05T12:00:00.000000+00:00",
        "side": "BUY_UP",
        "entry_price": "0.60",
        "slippage": "0.01",
        "fees": "0.002",
        "btc_delta_usd": "12.34",
        "btc_delta_pct": "0.0002",
        "seconds_remaining": 45.5,
        "spread": "0.02",
        "source_skew_ms": 100.0,
        "official_outcome": "UP",
        "gross_pnl": "0.40",
        "net_pnl": "0.388",
        "roi": "0.6466666667",
    }


def test_empty_database_is_v3_with_research_tables(tmp_path):
    with SnapshotStore(tmp_path / "research.db") as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        for table in ("strategy_runs", "shadow_trades"):
            assert (
                store.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                == 1
            )


def test_v2_upgrade_is_additive_and_preserves_snapshot_rows(tmp_path, snapshot):
    path = tmp_path / "research.db"
    with SnapshotStore(path) as store:
        store.save_snapshot(snapshot)
        before = tuple(store.connection.execute("SELECT * FROM snapshots").fetchone())
        before_count = store.count_snapshots()
        store.connection.execute("DROP TABLE shadow_trades")
        store.connection.execute("DROP TABLE strategy_runs")
        store.connection.execute("PRAGMA user_version = 2")

    with SnapshotStore(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert store.count_snapshots() == before_count
        assert tuple(store.connection.execute("SELECT * FROM snapshots").fetchone()) == before
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_research_records_enforce_foreign_keys_and_idempotent_trade(tmp_path, snapshot):
    with SnapshotStore(tmp_path / "research.db") as store:
        store.save_snapshot(snapshot)
        run_id = store.save_strategy_run(run_record())
        assert store.get_research_run(run_id)["strategy_name"] == "baseline"
        trade = trade_record(run_id, snapshot.market_id)
        assert store.save_shadow_trade(trade) is True
        assert store.save_shadow_trade(trade) is False
        assert store.connection.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            store.save_shadow_trade(trade_record(run_id, "unknown-market"))
        with pytest.raises(sqlite3.IntegrityError):
            store.save_strategy_run({**run_record(), "split_name": "invalid"})
