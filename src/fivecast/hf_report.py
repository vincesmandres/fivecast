"""Read-only metrics for M4B high-frequency evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import median


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def report(path: Path) -> dict:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT local_receive_timestamp, source_event_timestamp, source_to_receive_latency_ms, "
            "duplicate, out_of_order FROM btc_events UNION ALL SELECT local_receive_timestamp, "
            "source_event_timestamp, source_to_receive_latency_ms, duplicate, out_of_order "
            "FROM market_events "
            "ORDER BY local_receive_timestamp"
        ).fetchall()
        connections = connection.execute(
            "SELECT status, reconnect_attempt FROM hf_connections"
        ).fetchall()
        times = [datetime.fromisoformat(row["local_receive_timestamp"]) for row in rows]
        runtime = (max(times) - min(times)).total_seconds() if len(times) > 1 else 0.0
        latencies = [
            row["source_to_receive_latency_ms"]
            for row in rows
            if row["source_to_receive_latency_ms"] is not None
        ]
        gaps = [(times[index] - times[index - 1]).total_seconds() for index in range(1, len(times))]
        return {
            "btc_event_count": connection.execute("SELECT COUNT(*) FROM btc_events").fetchone()[0],
            "market_event_count": connection.execute(
                "SELECT COUNT(*) FROM market_events"
            ).fetchone()[0],
            "event_count": len(rows),
            "source_timestamp_available_count": sum(
                row["source_event_timestamp"] is not None for row in rows
            ),
            "source_timestamp_missing_count": sum(
                row["source_event_timestamp"] is None for row in rows
            ),
            "runtime_seconds": runtime,
            "events_per_second": len(rows) / runtime if runtime else None,
            "disconnect_count": sum(row["status"] != "connected" for row in connections),
            "reconnect_count": sum(row["reconnect_attempt"] > 0 for row in connections),
            "median_latency_ms": median(latencies) if latencies else None,
            "p95_latency_ms": _percentile(latencies, 0.95),
            "duplicate_count": sum(row["duplicate"] for row in rows),
            "out_of_order_count": sum(row["out_of_order"] for row in rows),
            "largest_gap_seconds": max(gaps, default=None),
            "database_bytes": path.stat().st_size,
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="FiveCast M4B HF report")
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    values = report(args.db_path)
    if args.json:
        print(json.dumps(values, indent=2, sort_keys=True))
    else:
        for key, value in values.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
