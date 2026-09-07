"""Transactional schema upgrades, retaining every original snapshot and its ID."""

import sqlite3
from datetime import datetime
from pathlib import Path

from fivecast.market.snapshot import measure_quality
from fivecast.models import MarketSnapshot

VERSION = 6
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

RESEARCH_TABLES = (
    """CREATE TABLE strategy_runs (
        id INTEGER PRIMARY KEY,
        strategy_name TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        dataset_start_utc TEXT NOT NULL,
        dataset_end_utc TEXT NOT NULL,
        split_name TEXT NOT NULL CHECK (split_name IN ('research', 'holdout')),
        quality_filters_json TEXT NOT NULL,
        source_snapshot_count INTEGER NOT NULL,
        eligible_market_count INTEGER NOT NULL,
        excluded_market_count INTEGER NOT NULL
    )""",
    """CREATE TABLE shadow_trades (
        id INTEGER PRIMARY KEY,
        strategy_run_id INTEGER NOT NULL REFERENCES strategy_runs(id),
        market_id TEXT NOT NULL REFERENCES markets(market_id),
        signal_timestamp_utc TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY_UP', 'BUY_DOWN')),
        entry_price TEXT NOT NULL,
        slippage TEXT NOT NULL,
        fees TEXT NOT NULL,
        btc_delta_usd TEXT NOT NULL,
        btc_delta_pct TEXT NOT NULL,
        seconds_remaining REAL NOT NULL,
        spread TEXT NOT NULL,
        source_skew_ms REAL NOT NULL,
        official_outcome TEXT NOT NULL CHECK (official_outcome IN ('UP', 'DOWN')),
        gross_pnl TEXT NOT NULL,
        net_pnl TEXT NOT NULL,
        roi TEXT NOT NULL,
        UNIQUE(strategy_run_id, market_id)
    )""",
)

HF_TABLES = (
    """CREATE TABLE IF NOT EXISTS btc_events (
        id INTEGER PRIMARY KEY,
        source_event_timestamp TEXT,
        local_receive_timestamp TEXT NOT NULL,
        source TEXT NOT NULL,
        market_id TEXT,
        token_id TEXT,
        sequence INTEGER,
        event_type TEXT NOT NULL,
        price TEXT,
        bid TEXT,
        ask TEXT,
        size TEXT,
        raw_payload TEXT NOT NULL,
        duplicate INTEGER NOT NULL DEFAULT 0 CHECK (duplicate IN (0, 1)),
        out_of_order INTEGER NOT NULL DEFAULT 0 CHECK (out_of_order IN (0, 1)),
        source_to_receive_latency_ms REAL,
        CHECK (source_event_timestamp IS NULL OR source_event_timestamp <> '')
    )""",
    """CREATE TABLE IF NOT EXISTS market_events (
        id INTEGER PRIMARY KEY,
        source_event_timestamp TEXT,
        local_receive_timestamp TEXT NOT NULL,
        source TEXT NOT NULL,
        market_id TEXT,
        token_id TEXT,
        sequence INTEGER,
        event_type TEXT NOT NULL,
        price TEXT,
        bid TEXT,
        ask TEXT,
        size TEXT,
        raw_payload TEXT NOT NULL,
        duplicate INTEGER NOT NULL DEFAULT 0 CHECK (duplicate IN (0, 1)),
        out_of_order INTEGER NOT NULL DEFAULT 0 CHECK (out_of_order IN (0, 1)),
        source_to_receive_latency_ms REAL
    )""",
    """CREATE TABLE IF NOT EXISTS hf_connections (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        connected_at_utc TEXT NOT NULL,
        disconnected_at_utc TEXT,
        status TEXT NOT NULL CHECK (status IN ('connected', 'disconnected', 'failed')),
        reconnect_attempt INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )""",
)

HF_INDEXES = (
    "CREATE INDEX IF NOT EXISTS btc_events_timestamp ON btc_events(local_receive_timestamp)",
    "CREATE INDEX IF NOT EXISTS btc_events_source ON btc_events(source, local_receive_timestamp)",
    "CREATE INDEX IF NOT EXISTS market_events_timestamp ON market_events(local_receive_timestamp)",
    "CREATE INDEX IF NOT EXISTS market_events_source_token ON market_events("
    "source, token_id, local_receive_timestamp)",
    "CREATE INDEX IF NOT EXISTS market_events_market_timestamp ON market_events("
    "market_id, local_receive_timestamp)",
    "CREATE INDEX IF NOT EXISTS hf_connections_source ON hf_connections(source, connected_at_utc)",
)

FORWARD_TABLES = (
    """CREATE TABLE IF NOT EXISTS model_versions (
        id INTEGER PRIMARY KEY,
        version TEXT NOT NULL UNIQUE,
        artifact_json TEXT NOT NULL,
        fingerprint TEXT NOT NULL UNIQUE,
        dataset_fingerprint TEXT NOT NULL,
        training_cutoff_utc TEXT NOT NULL,
        created_at_utc TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS forward_predictions (
        id INTEGER PRIMARY KEY,
        model_version_id INTEGER NOT NULL REFERENCES model_versions(id),
        market_id TEXT NOT NULL REFERENCES markets(market_id),
        timestamp_utc TEXT NOT NULL,
        features_json TEXT NOT NULL,
        features_fingerprint TEXT NOT NULL,
        predicted_up_probability TEXT NOT NULL,
        up_ask TEXT,
        down_ask TEXT,
        selected_side TEXT,
        selected_ask TEXT,
        raw_edge TEXT,
        estimated_fees TEXT,
        estimated_slippage TEXT,
        estimated_latency TEXT,
        net_edge TEXT,
        eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
        rejection_reason TEXT,
        official_outcome TEXT CHECK (official_outcome IN ('UP', 'DOWN')),
        paper_pnl TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(model_version_id, market_id, timestamp_utc)
    )""",
    """CREATE TABLE IF NOT EXISTS forward_paper_trades (
        id INTEGER PRIMARY KEY,
        prediction_id INTEGER NOT NULL UNIQUE REFERENCES forward_predictions(id),
        model_version_id INTEGER NOT NULL REFERENCES model_versions(id),
        market_id TEXT NOT NULL REFERENCES markets(market_id),
        timestamp_utc TEXT NOT NULL,
        side TEXT NOT NULL,
        entry_price TEXT NOT NULL,
        gross_pnl TEXT,
        estimated_fees TEXT,
        estimated_slippage TEXT,
        estimated_latency TEXT,
        net_pnl TEXT,
        official_outcome TEXT CHECK (official_outcome IN ('UP', 'DOWN')),
        created_at_utc TEXT NOT NULL,
        UNIQUE(model_version_id, market_id)
    )""",
)

M6_TABLES = (
    """CREATE TABLE IF NOT EXISTS m6_experiments (
        id INTEGER PRIMARY KEY,
        experiment_id TEXT NOT NULL UNIQUE,
        model_version_id INTEGER NOT NULL REFERENCES model_versions(id),
        experiment_start_utc TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        manifest_hash TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('active', 'complete')),
        created_at_utc TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS m6_prediction_links (
        experiment_id INTEGER NOT NULL REFERENCES m6_experiments(id),
        prediction_id INTEGER NOT NULL REFERENCES forward_predictions(id),
        linked_at_utc TEXT NOT NULL,
        PRIMARY KEY (experiment_id, prediction_id)
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
    if version == 2:
        migrate_v2_to_v3(connection)
        version = 3
    if version == 3:
        migrate_v3_to_v4(connection)
        version = 4
    if version == 4:
        migrate_v4_to_v5(connection)
        version = 5
    if version == 5:
        migrate_v5_to_v6(connection)
        return
    if version != 0:
        raise ValueError(
            f"Unsupported database schema version {version}; expected 0 through {VERSION}"
        )
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
        connection.execute("PRAGMA user_version = 2")
    migrate_v2_to_v3(connection)
    migrate_v3_to_v4(connection)
    migrate_v4_to_v5(connection)
    migrate_v5_to_v6(connection)


def migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Add research evidence tables without rewriting any existing table."""
    with connection:
        for statement in RESEARCH_TABLES:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")


def migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Add append-only high-frequency evidence without touching prior tables."""
    with connection:
        for statement in HF_TABLES:
            connection.execute(statement)
        for statement in HF_INDEXES:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 4")


def migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    with connection:
        for statement in FORWARD_TABLES:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 5")


def migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    with connection:
        for statement in M6_TABLES:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 6")
