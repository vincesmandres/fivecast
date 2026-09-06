import json
import sqlite3
from datetime import timedelta

import pytest

from fivecast.market.snapshot import measure_quality
from fivecast.report import generate_report, main
from fivecast.storage.sqlite import SnapshotStore


def _at(snapshot, seconds):
    timestamp = snapshot.market_start_utc + timedelta(seconds=seconds)
    return snapshot.model_validate(
        {
            **snapshot.model_dump(),
            "timestamp_utc": timestamp,
            "seconds_remaining": (snapshot.market_end_utc - timestamp).total_seconds(),
            "btc_timestamp_utc": timestamp,
            "up_timestamp_utc": timestamp,
            "down_timestamp_utc": timestamp,
            "btc_open_observed_at_utc": snapshot.market_start_utc,
        }
    )


def test_partial_window_grid_and_downtime_are_not_resampled(tmp_path, snapshot):
    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        run = store.start_run(snapshot.market_start_utc + timedelta(seconds=10), 30)
        store.save_snapshot(_at(snapshot, 40), run_id=run)
        store.save_snapshot(_at(snapshot, 70), run_id=run)
        store.finish_run(run, snapshot.market_start_utc + timedelta(seconds=75))
        later = store.start_run(snapshot.market_start_utc + timedelta(seconds=180), 30)
        store.save_snapshot(_at(snapshot, 190), run_id=later)
        store.finish_run(later, snapshot.market_start_utc + timedelta(seconds=250))

    report = generate_report(path)["global"]
    assert report["expected_sample_count"] == 6  # 10, 40, 70 and 180, 210, 240
    assert report["coverage_sample_count"] == 3
    assert report["coverage_pct"] == 50.0


def test_quality_gaps_stale_and_legacy_are_reported(tmp_path, snapshot):
    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        run = store.start_run(snapshot.market_start_utc, 10)
        first = _at(snapshot, 10)
        second = _at(snapshot, 30)
        quality = measure_quality(first).model_copy(update={"is_stale": True})
        store.save_snapshot(first, quality, run)
        store.save_snapshot(second, run_id=run)
        store.save_snapshot(_at(snapshot, 50))
        store.finish_run(run, snapshot.market_start_utc + timedelta(seconds=40))

    metrics = generate_report(path)["global"]
    assert metrics["stale_snapshot_count"] == 1
    assert metrics["unknown_stale_snapshot_count"] == 2
    assert metrics["legacy_snapshot_count"] == 1
    assert metrics["mean_gap_seconds"] == 20.0
    assert metrics["max_gap_seconds"] == 20.0


def test_discovery_failure_is_attributed_by_slug_and_resolution_metadata(
    tmp_path, snapshot, market
):
    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        store.save_market(market)
        run = store.start_run(snapshot.market_start_utc, 5)
        poll = store.start_poll(run, snapshot.market_start_utc, snapshot.market_start_utc)
        store.finish_poll(poll, snapshot.market_start_utc, 1, failure_kind="not_found")
        store.finish_run(run, snapshot.market_start_utc + timedelta(seconds=5))
        store.save_snapshot(snapshot, run_id=run)

    market_report = generate_report(path)["markets"][0]
    assert market_report["failed_poll_count"] == 1
    assert market_report["resolved"] is False
    assert market_report["outcome"] is None


def test_cli_json_and_read_only_errors(tmp_path, snapshot, capsys):
    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        store.save_snapshot(snapshot)
    assert main(["--db-path", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshots_stored"] == 1
    assert main(["--db-path", str(tmp_path / "missing.db"), "--json"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_schema_v0_is_refused_without_writing(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.close()
    with pytest.raises(RuntimeError, match="Run the existing collector migration first"):
        generate_report(path)
    assert path.stat().st_size > 0


def test_grid_clips_to_market_boundaries_with_microsecond_precision(tmp_path, market):
    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        store.save_market(market)
        start = market.start_time_utc - timedelta(seconds=2, microseconds=1)
        run = store.start_run(start, 5)
        store.finish_run(run, market.start_time_utc + timedelta(seconds=13))
    report = generate_report(path)
    assert report["global"]["expected_sample_count"] == 4
    assert report["markets"][0]["expected_sample_count"] == 3
    assert report["markets"][0]["coverage_pct"] == 0
    assert report["global"]["markets_observed"] == 1


def test_resolved_metadata_counted_even_without_successful_snapshots(tmp_path, market):
    from fivecast.models import MarketResolution

    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        store.save_market(market)
        store.save_resolution(
            MarketResolution(
                market_id=market.market_id, outcome="UP", observed_at_utc=market.end_time_utc
            )
        )
    result = generate_report(path)["global"]
    assert result["markets_observed"] == 1
    assert result["markets_resolved"] == 1
    assert result["snapshots_stored"] == 0


def test_unfinished_run_uses_heartbeat_not_report_wall_clock(tmp_path, market):
    path = tmp_path / "report.db"
    with SnapshotStore(path) as store:
        store.save_market(market)
        run = store.start_run(market.start_time_utc, 5)
        poll = store.start_poll(run, market.start_time_utc, market.start_time_utc)
        store.finish_poll(poll, market.start_time_utc + timedelta(seconds=1), 1000, "ReadTimeout")
    assert generate_report(path)["global"]["expected_sample_count"] == 1
    assert generate_report(path)["global"]["failed_poll_count"] == 1


def test_report_missing_database_does_not_create_file(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        generate_report(path)
    assert not path.exists()
