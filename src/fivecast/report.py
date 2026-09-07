"""Read-only collection quality reports."""

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 6
MICROS_PER_SECOND = 1_000_000


def _time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("Stored timestamps must be timezone-aware")
    return moment.astimezone(UTC)


def _micros(value: str | datetime) -> int:
    moment = _time(value) if isinstance(value, str) else value.astimezone(UTC)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * MICROS_PER_SECOND + delta.microseconds


def _grid_count(start: int, end: int, interval: float) -> int:
    if end <= start:
        return 0
    interval_us = max(1, round(interval * MICROS_PER_SECOND))
    # The first grid point is start + n*interval and the right edge is open.
    return max(0, (end - start - 1) // interval_us + 1)


def _grid_intersection_count(run_start: int, lower: int, upper: int, interval: float) -> int:
    """Count run grid points in the half-open [lower, upper) interval."""
    if upper <= lower:
        return 0
    interval_us = max(1, round(interval * MICROS_PER_SECOND))
    offset = max(0, lower - run_start)
    first_index = (offset + interval_us - 1) // interval_us
    first = run_start + first_index * interval_us
    return _grid_count(first, upper, interval)


def _empty_metrics() -> dict[str, Any]:
    return {
        "markets_observed": 0,
        "markets_resolved": 0,
        "snapshots_stored": 0,
        "sample_count": 0,
        "expected_sample_count": 0,
        "coverage_pct": None,
        "coverage_sample_count": 0,
        "mean_gap_seconds": None,
        "max_gap_seconds": None,
        "mean_skew_ms": None,
        "max_skew_ms": None,
        "stale_snapshot_count": 0,
        "unknown_stale_snapshot_count": 0,
        "failed_poll_count": 0,
        "interrupted_poll_count": 0,
        "pending_poll_count": 0,
        "legacy_snapshot_count": 0,
    }


def _gaps(connection: sqlite3.Connection) -> dict[str, tuple[int, float, float]]:
    previous: dict[tuple[int | None, str], int] = {}
    gaps: dict[str, tuple[int, float, float]] = {}
    rows = connection.execute(
        "SELECT run_id, market_id, timestamp_utc FROM snapshots "
        "ORDER BY run_id, market_id, timestamp_utc"
    )
    for row in rows:
        key = (row["run_id"], row["market_id"])
        current = _micros(row["timestamp_utc"])
        if key in previous:
            gap = (current - previous[key]) / MICROS_PER_SECOND
            count, total, largest = gaps.get(row["market_id"], (0, 0.0, 0.0))
            gaps[row["market_id"]] = (count + 1, total + gap, max(largest, gap))
        previous[key] = current
    return gaps


def _run_expected(runs: list[sqlite3.Row]) -> int:
    return sum(
        _grid_count(
            _micros(run["started_at_utc"]),
            _micros(run["stopped_at_utc"] or run["heartbeat_at_utc"]),
            run["interval_seconds"],
        )
        for run in runs
    )


def _market_expected(runs: list[sqlite3.Row], market: sqlite3.Row) -> int:
    market_start = _micros(market["start_time_utc"])
    market_end = _micros(market["end_time_utc"])
    total = 0
    for run in runs:
        run_start = _micros(run["started_at_utc"])
        end = min(_micros(run["stopped_at_utc"] or run["heartbeat_at_utc"]), market_end)
        total += _grid_intersection_count(run_start, market_start, end, run["interval_seconds"])
    return total


def _base_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    metrics = _empty_metrics()
    snapshot = connection.execute(
        "SELECT COUNT(*) AS total, COUNT(run_id) AS measured, "
        "COUNT(CASE WHEN run_id IS NULL THEN 1 END) AS legacy, "
        "COUNT(CASE WHEN is_stale = 1 THEN 1 END) AS stale, "
        "COUNT(CASE WHEN is_stale IS NULL THEN 1 END) AS unknown, "
        "AVG(source_skew_ms) AS mean_skew, MAX(source_skew_ms) AS max_skew "
        "FROM snapshots"
    ).fetchone()
    metrics.update(
        snapshots_stored=snapshot["total"],
        sample_count=snapshot["total"],
        coverage_sample_count=snapshot["measured"],
        legacy_snapshot_count=snapshot["legacy"],
        stale_snapshot_count=snapshot["stale"],
        unknown_stale_snapshot_count=snapshot["unknown"],
        mean_skew_ms=snapshot["mean_skew"],
        max_skew_ms=snapshot["max_skew"],
    )
    polls = connection.execute(
        "SELECT COUNT(CASE WHEN status = 'failed' THEN 1 END) AS failed, "
        "COUNT(CASE WHEN status = 'interrupted' THEN 1 END) AS interrupted, "
        "COUNT(CASE WHEN status = 'pending' THEN 1 END) AS pending FROM polls"
    ).fetchone()
    metrics.update(
        failed_poll_count=polls["failed"],
        interrupted_poll_count=polls["interrupted"],
        pending_poll_count=polls["pending"],
    )
    return metrics


def generate_report(path: Path) -> dict[str, Any]:
    """Generate a report without creating, migrating, or changing *path*."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Database does not exist: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot open database read-only: {path}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")  # One consistent WAL read snapshot, even while collecting.
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported database schema version {version}; expected {SCHEMA_VERSION}. "
                "Run the existing collector migration first."
            )
        runs = connection.execute("SELECT * FROM collection_runs ORDER BY id").fetchall()
        metrics = _base_metrics(connection)
        markets = connection.execute("SELECT * FROM markets ORDER BY start_time_utc").fetchall()
        metrics["markets_observed"] = len(markets)
        metrics["markets_resolved"] = sum(market["resolved"] for market in markets)
        metrics["unattributed_failed_poll_count"] = connection.execute(
            "SELECT COUNT(*) FROM polls p LEFT JOIN markets m ON m.slug=p.market_slug "
            "WHERE p.status='failed' AND m.market_id IS NULL"
        ).fetchone()[0]
        metrics["expected_sample_count"] = _run_expected(runs)
        metrics["coverage_pct"] = (
            None
            if not metrics["expected_sample_count"]
            else metrics["coverage_sample_count"] * 100 / metrics["expected_sample_count"]
        )
        gap_data = _gaps(connection)
        gap_values = list(gap_data.values())
        if gap_values:
            total_gap_count = sum(value[0] for value in gap_values)
            metrics["mean_gap_seconds"] = sum(value[1] for value in gap_values) / total_gap_count
            metrics["max_gap_seconds"] = max(value[2] for value in gap_values)
        market_reports = []
        for market in markets:
            market_id = market["market_id"]
            item = _empty_metrics()
            item.update(
                market_id=market_id,
                slug=market["slug"],
                outcome=market["outcome"],
                resolved=bool(market["resolved"]),
                start_time_utc=market["start_time_utc"],
                end_time_utc=market["end_time_utc"],
                resolution_time_utc=market["resolution_time_utc"],
                resolution_time_source=market["resolution_time_source"],
                resolution_observed_at_utc=market["resolution_observed_at_utc"],
                settlement_error_count=market["settlement_error_count"],
            )
            counts = connection.execute(
                "SELECT COUNT(*) AS total, COUNT(run_id) AS measured, "
                "COUNT(CASE WHEN run_id IS NULL THEN 1 END) AS legacy, "
                "COUNT(CASE WHEN is_stale = 1 THEN 1 END) AS stale, "
                "COUNT(CASE WHEN is_stale IS NULL THEN 1 END) AS unknown, "
                "AVG(source_skew_ms) AS mean_skew, MAX(source_skew_ms) AS max_skew "
                "FROM snapshots WHERE market_id = ?",
                (market_id,),
            ).fetchone()
            item.update(
                snapshots_stored=counts["total"],
                sample_count=counts["total"],
                coverage_sample_count=counts["measured"],
                legacy_snapshot_count=counts["legacy"],
                stale_snapshot_count=counts["stale"],
                unknown_stale_snapshot_count=counts["unknown"],
                mean_skew_ms=counts["mean_skew"],
                max_skew_ms=counts["max_skew"],
            )
            poll_counts = connection.execute(
                "SELECT COUNT(CASE WHEN status = 'failed' THEN 1 END), "
                "COUNT(CASE WHEN status = 'interrupted' THEN 1 END), "
                "COUNT(CASE WHEN status = 'pending' THEN 1 END) FROM polls "
                "WHERE market_slug = ?",
                (market["slug"],),
            ).fetchone()
            item.update(
                failed_poll_count=poll_counts[0],
                interrupted_poll_count=poll_counts[1],
                pending_poll_count=poll_counts[2],
            )
            item["markets_observed"] = 1
            item["markets_resolved"] = int(bool(market["resolved"]))
            item["expected_sample_count"] = _market_expected(runs, market)
            item["coverage_pct"] = (
                None
                if not item["expected_sample_count"]
                else item["coverage_sample_count"] * 100 / item["expected_sample_count"]
            )
            gap = gap_data.get(market_id)
            item["mean_gap_seconds"] = None if gap is None else gap[1] / gap[0]
            item["max_gap_seconds"] = None if gap is None else gap[2]
            market_reports.append(item)
        return {"global": metrics, "markets": market_reports}
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report FiveCast collection quality")
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--per-market", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.db_path is not None:
            db_path = args.db_path
        else:
            from fivecast.config import load_settings

            db_path = load_settings(args.config).db_path
        report = generate_report(db_path)
    except (FileNotFoundError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        output: Any = report if args.per_market else report["global"]
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        sections = [("Overall", report["global"])]
        if args.per_market:
            sections.extend((f"Market {item['market_id']}", item) for item in report["markets"])
        for title, metrics in sections:
            print(f"\n{title}")
            for name, value in metrics.items():
                display = (
                    "N/A"
                    if value is None
                    else f"{value:.3f}"
                    if isinstance(value, float)
                    else value
                )
                print(f"{name:30} {display}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
