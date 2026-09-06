"""Transactional v0 -> v2 upgrade, retaining every original snapshot and its ID."""

import sqlite3
from datetime import datetime
from pathlib import Path

from fivecast.market.snapshot import measure_quality
from fivecast.models import MarketSnapshot

VERSION = 2
TABLES = (
    """CREATE TABLE markets (
        market_id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        condition_id TEXT,
        question TEXT,
        start_time_utc TEXT NOT NULL,
        end_time_utc TEXT NOT NULL,
        up_token_id TEXT NOT NULL,
        down_token_id TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0, 1)),
        resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
        outcome TEXT CHECK (outcome IN ('UP', 'DOWN')),
        resolution_time_utc TEXT,
        resolution_time_source TEXT,
        resolution_observed_at_utc TEXT,
        resolution_source TEXT,
        metadata_source TEXT NOT NULL,
        settlement_checked_at_utc TEXT,
        next_settlement_check_utc TEXT,
        settlement_error_count INTEGER NOT NULL DEFAULT 0,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        CHECK ((resolved = 0 AND outcome IS NULL) OR
               (resolved = 1 AND outcome IS NOT NULL AND resolution_observed_at_utc IS NOT NULL))
    )""",
    """CREATE TABLE collection_runs (
        id INTEGER PRIMARY KEY,
        started_at_utc TEXT NOT NULL,
        heartbeat_at_utc TEXT NOT NULL,
        stopped_at_utc TEXT,
        interval_seconds REAL NOT NULL CHECK (interval_seconds > 0)
    )""",
    """CREATE TABLE polls (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        scheduled_at_utc TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        finished_at_utc TEXT,
        market_slug TEXT NOT NULL,
        market_id TEXT REFERENCES markets(market_id),
        status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed', 'interrupted')),
        failure_kind TEXT,
        poll_latency_ms REAL CHECK (poll_latency_ms >= 0)
    )""",
)


def iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Database timestamps must be aware")
    from datetime import UTC

    return value.astimezone(UTC).isoformat(timespec="microseconds")


def ensure_snapshot_market(
    connection: sqlite3.Connection, snapshot: MarketSnapshot, created: str
) -> None:
    values = (
        f"btc-updown-5m-{int(snapshot.market_start_utc.timestamp())}",
        iso(snapshot.market_start_utc),
        iso(snapshot.market_end_utc),
        snapshot.up_token_id,
        snapshot.down_token_id,
    )
    previous = connection.execute(
        "SELECT slug, start_time_utc, end_time_utc, up_token_id, down_token_id "
        "FROM markets WHERE market_id = ?",
        (snapshot.market_id,),
    ).fetchone()
    if previous is not None:
        if tuple(previous) != values:
            raise ValueError("Snapshot identity conflicts with persisted market metadata")
        return
    connection.execute(
        "INSERT INTO markets (market_id, slug, start_time_utc, end_time_utc, up_token_id, "
        "down_token_id, metadata_source, created_at_utc, updated_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, 'snapshot_backfill', ?, ?)",
        (snapshot.market_id, *values, created, created),
    )


def migrate(connection: sqlite3.Connection, path: Path, legacy_schema: str) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == VERSION:
        return
    if version != 0:
        raise ValueError(f"Unsupported database schema version {version}; expected 0 or {VERSION}")
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshots'"
    ).fetchone()
    if existing and connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]:
        backup_path = path.with_name(path.name + ".v0.bak")
        if not backup_path.exists():
            backup = sqlite3.connect(backup_path)
            try:
                connection.backup(backup)
            finally:
                backup.close()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in TABLES:
            connection.execute(statement)
        if existing:
            connection.execute("ALTER TABLE snapshots RENAME TO snapshots_v0")
        statement = legacy_schema.split(";")[0].replace(
            "market_id TEXT NOT NULL,", "market_id TEXT NOT NULL REFERENCES markets(market_id),"
        )
        statement = statement.replace(
            "created_at_utc TEXT NOT NULL,",
            """created_at_utc TEXT NOT NULL,
            btc_source_timestamp TEXT NOT NULL,
            market_source_timestamp TEXT NOT NULL,
            source_skew_ms REAL NOT NULL CHECK (source_skew_ms >= 0),
            poll_latency_ms REAL CHECK (poll_latency_ms >= 0),
            is_stale INTEGER CHECK (is_stale IN (0, 1)),
            run_id INTEGER REFERENCES collection_runs(id),""",
        )
        connection.execute(statement)
        if existing:
            for row in connection.execute("SELECT * FROM snapshots_v0"):
                values = dict(row)
                snapshot = MarketSnapshot.model_validate(
                    {key: values[key] for key in MarketSnapshot.model_fields}
                )
                ensure_snapshot_market(connection, snapshot, values["created_at_utc"])
                quality = measure_quality(snapshot)
                values.update(quality.model_dump(mode="json"))
                values["btc_source_timestamp"] = iso(quality.btc_source_timestamp)
                values["market_source_timestamp"] = iso(quality.market_source_timestamp)
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO snapshots ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
            connection.execute("DROP TABLE snapshots_v0")
        connection.execute("CREATE INDEX snapshots_timestamp ON snapshots(timestamp_utc)")
        connection.execute("CREATE INDEX snapshots_market ON snapshots(market_id, timestamp_utc)")
        connection.execute("CREATE INDEX polls_slug ON polls(market_slug)")
        connection.execute(f"PRAGMA user_version = {VERSION}")
