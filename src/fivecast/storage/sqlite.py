"""SQLite snapshots with exact decimal text and idempotent identical inserts."""

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from fivecast.market.snapshot import measure_quality
from fivecast.models import (
    MarketResolution,
    MarketSnapshot,
    PredictionMarket,
    SnapshotQuality,
    utc_now,
)
from fivecast.storage.migrations import ensure_snapshot_market, iso, migrate

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    timestamp_utc TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_start_utc TEXT NOT NULL,
    market_end_utc TEXT NOT NULL,
    seconds_remaining REAL NOT NULL CHECK (seconds_remaining > 0 AND seconds_remaining <= 300),
    btc_price TEXT NOT NULL,
    btc_window_open_price TEXT NOT NULL,
    btc_delta_usd TEXT NOT NULL,
    btc_delta_pct TEXT NOT NULL,
    up_bid TEXT,
    up_ask TEXT,
    down_bid TEXT,
    down_ask TEXT,
    up_spread TEXT,
    down_spread TEXT,
    source_btc TEXT NOT NULL,
    source_prediction_market TEXT NOT NULL,
    btc_timestamp_utc TEXT NOT NULL,
    up_timestamp_utc TEXT NOT NULL,
    down_timestamp_utc TEXT NOT NULL,
    up_token_id TEXT NOT NULL,
    down_token_id TEXT NOT NULL,
    btc_open_observed_at_utc TEXT NOT NULL,
    btc_open_method TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(market_id, timestamp_utc)
);
CREATE INDEX IF NOT EXISTS snapshots_timestamp ON snapshots(timestamp_utc);
"""

STRATEGY_RUN_FIELDS = (
    "strategy_name",
    "strategy_version",
    "parameters_json",
    "created_at_utc",
    "dataset_start_utc",
    "dataset_end_utc",
    "split_name",
    "quality_filters_json",
    "source_snapshot_count",
    "eligible_market_count",
    "excluded_market_count",
)
SHADOW_TRADE_FIELDS = (
    "strategy_run_id",
    "market_id",
    "signal_timestamp_utc",
    "side",
    "entry_price",
    "slippage",
    "fees",
    "btc_delta_usd",
    "btc_delta_pct",
    "seconds_remaining",
    "spread",
    "source_skew_ms",
    "official_outcome",
    "gross_pnl",
    "net_pnl",
    "roi",
)
HF_EVENT_FIELDS = (
    "source_event_timestamp",
    "local_receive_timestamp",
    "source",
    "market_id",
    "token_id",
    "sequence",
    "event_type",
    "price",
    "bid",
    "ask",
    "size",
    "raw_payload",
    "duplicate",
    "out_of_order",
    "source_to_receive_latency_ms",
)
MODEL_VERSION_FIELDS = (
    "version",
    "artifact_json",
    "fingerprint",
    "dataset_fingerprint",
    "training_cutoff_utc",
    "created_at_utc",
)
FORWARD_PREDICTION_FIELDS = (
    "model_version_id",
    "market_id",
    "timestamp_utc",
    "features_json",
    "features_fingerprint",
    "predicted_up_probability",
    "up_ask",
    "down_ask",
    "selected_side",
    "selected_ask",
    "raw_edge",
    "estimated_fees",
    "estimated_slippage",
    "estimated_latency",
    "net_edge",
    "eligible",
    "rejection_reason",
    "official_outcome",
    "paper_pnl",
    "created_at_utc",
)
FORWARD_PAPER_TRADE_FIELDS = (
    "prediction_id",
    "model_version_id",
    "market_id",
    "timestamp_utc",
    "side",
    "entry_price",
    "gross_pnl",
    "estimated_fees",
    "estimated_slippage",
    "estimated_latency",
    "net_pnl",
    "official_outcome",
    "created_at_utc",
)


class SnapshotStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        try:
            migrate(self.connection, path, SCHEMA)
            self.connection.execute("PRAGMA journal_mode = WAL")
        except (sqlite3.Error, ValueError):
            self.connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def save_snapshot(
        self,
        snapshot: MarketSnapshot,
        quality: SnapshotQuality | None = None,
        run_id: int | None = None,
    ) -> bool:
        derived = measure_quality(snapshot)
        if quality is None:
            quality = derived
        if (
            quality.btc_source_timestamp != derived.btc_source_timestamp
            or quality.market_source_timestamp != derived.market_source_timestamp
            or quality.source_skew_ms != derived.source_skew_ms
        ):
            raise ValueError("Quality timing does not match the validated snapshot")
        values = snapshot.model_dump(mode="json")
        # Fixed-width UTC timestamps sort chronologically even at exact second boundaries.
        for field in values:
            if field.endswith("_utc"):
                values[field] = getattr(snapshot, field).isoformat(timespec="microseconds")
        values["created_at_utc"] = utc_now().isoformat(timespec="microseconds")
        values.update(quality.model_dump(mode="json"))
        values["btc_source_timestamp"] = iso(quality.btc_source_timestamp)
        values["market_source_timestamp"] = iso(quality.market_source_timestamp)
        values["run_id"] = run_id
        columns = ", ".join(values)  # Names come only from the internal model, never the network.
        placeholders = ", ".join("?" for _ in values)
        with self.connection:
            ensure_snapshot_market(self.connection, snapshot, values["created_at_utc"])
            cursor = self.connection.execute(
                f"INSERT INTO snapshots ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT(market_id, timestamp_utc) DO NOTHING",
                tuple(values.values()),
            )
            if cursor.rowcount == 1:
                return True
            row = self.connection.execute(
                "SELECT * FROM snapshots WHERE market_id = ? AND timestamp_utc = ?",
                (snapshot.market_id, values["timestamp_utc"]),
            ).fetchone()
            if self._decode(row) != snapshot:
                raise ValueError("Conflicting snapshot at the same market and timestamp")
            return False

    def count_snapshots(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    def get_latest_snapshot(self) -> MarketSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM snapshots ORDER BY timestamp_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> MarketSnapshot:
        return MarketSnapshot.model_validate({key: row[key] for key in MarketSnapshot.model_fields})

    def save_market(self, market: PredictionMarket) -> None:
        now = iso(utc_now())
        previous = self.get_market(market.market_id)
        values = {
            "market_id": market.market_id,
            "slug": market.slug,
            "condition_id": market.condition_id.lower(),
            "question": market.question,
            "start_time_utc": iso(market.start_time_utc),
            "end_time_utc": iso(market.end_time_utc),
            "up_token_id": market.up_token_id,
            "down_token_id": market.down_token_id,
        }
        if previous:
            for key in ("slug", "start_time_utc", "end_time_utc", "up_token_id", "down_token_id"):
                if previous[key] != values[key]:
                    raise ValueError(f"Market metadata changed immutable field {key}")
            if previous["condition_id"] not in (None, values["condition_id"]):
                raise ValueError("Market condition identity changed")
        with self.connection:
            self.connection.execute(
                "INSERT INTO markets (market_id, slug, condition_id, question, start_time_utc, "
                "end_time_utc, up_token_id, down_token_id, metadata_source, created_at_utc, "
                "updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'gamma', ?, ?) "
                "ON CONFLICT(market_id) DO UPDATE SET condition_id=excluded.condition_id, "
                "question=excluded.question, metadata_source='gamma', "
                "updated_at_utc=excluded.updated_at_utc",
                (*values.values(), now, now),
            )

    def get_market(self, market_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM markets WHERE market_id = ?", (market_id,)
        ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _research_values(record: Mapping[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
        if set(record) != set(fields):
            raise ValueError(f"Record fields must be exactly: {', '.join(fields)}")
        return tuple(record[field] for field in fields)

    def save_strategy_run(self, record: Mapping[str, Any]) -> int:
        values = self._research_values(record, STRATEGY_RUN_FIELDS)
        columns = ", ".join(STRATEGY_RUN_FIELDS)
        placeholders = ", ".join("?" for _ in STRATEGY_RUN_FIELDS)
        with self.connection:
            return self.connection.execute(
                f"INSERT INTO strategy_runs ({columns}) VALUES ({placeholders})", values
            ).lastrowid

    def save_shadow_trade(self, record: Mapping[str, Any]) -> bool:
        values = self._research_values(record, SHADOW_TRADE_FIELDS)
        columns = ", ".join(SHADOW_TRADE_FIELDS)
        placeholders = ", ".join("?" for _ in SHADOW_TRADE_FIELDS)
        with self.connection:
            cursor = self.connection.execute(
                f"INSERT INTO shadow_trades ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT(strategy_run_id, market_id) DO NOTHING",
                values,
            )
        return cursor.rowcount == 1

    def get_research_run(self, run_id: int) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM strategy_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def save_hf_event(self, table: str, record: Mapping[str, Any]) -> int:
        if table not in {"btc_events", "market_events"}:
            raise ValueError("HF event table must be btc_events or market_events")
        values = self._research_values(record, HF_EVENT_FIELDS)
        columns = ", ".join(HF_EVENT_FIELDS)
        placeholders = ", ".join("?" for _ in HF_EVENT_FIELDS)
        with self.connection:
            return self.connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values
            ).lastrowid

    def save_hf_connection(self, record: Mapping[str, Any]) -> int:
        fields = (
            "source",
            "connected_at_utc",
            "disconnected_at_utc",
            "status",
            "reconnect_attempt",
            "error",
        )
        values = self._research_values(record, fields)
        with self.connection:
            return self.connection.execute(
                f"INSERT INTO hf_connections ({', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in fields)})",
                values,
            ).lastrowid

    def persist_model_version(self, record: Mapping[str, Any]) -> int:
        values = self._research_values(record, MODEL_VERSION_FIELDS)
        columns = ", ".join(MODEL_VERSION_FIELDS)
        with self.connection:
            return self.connection.execute(
                f"INSERT INTO model_versions ({columns}) VALUES ({', '.join('?' for _ in values)})",
                values,
            ).lastrowid

    def get_model_version(self, identifier: int | str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM model_versions WHERE id = ? OR version = ? OR fingerprint = ? "
            "ORDER BY id LIMIT 1",
            (identifier, str(identifier), str(identifier)),
        ).fetchone()
        return None if row is None else dict(row)

    def save_m6_experiment(self, record: Mapping[str, Any]) -> int:
        fields = (
            "experiment_id",
            "model_version_id",
            "experiment_start_utc",
            "manifest_json",
            "manifest_hash",
            "status",
            "created_at_utc",
        )
        values = self._research_values(record, fields)
        with self.connection:
            cursor = self.connection.execute(
                f"INSERT INTO m6_experiments ({', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in fields)}) "
                "ON CONFLICT(experiment_id) DO NOTHING",
                values,
            )
            if cursor.rowcount == 1:
                return cursor.lastrowid
            row = self.connection.execute(
                "SELECT id, manifest_hash FROM m6_experiments WHERE experiment_id=?",
                (record["experiment_id"],),
            ).fetchone()
            if row["manifest_hash"] != record["manifest_hash"]:
                raise ValueError("Existing M6 experiment manifest is immutable")
            return row["id"]

    def get_m6_experiment(self, identifier: int | str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM m6_experiments WHERE id=? OR experiment_id=? ORDER BY id LIMIT 1",
            (identifier, str(identifier)),
        ).fetchone()
        return None if row is None else dict(row)

    def link_m6_prediction(
        self, experiment_id: int, prediction_id: int, linked_at: datetime
    ) -> bool:
        with self.connection:
            return (
                self.connection.execute(
                    "INSERT INTO m6_prediction_links "
                    "(experiment_id, prediction_id, linked_at_utc) VALUES (?, ?, ?) "
                    "ON CONFLICT(experiment_id, prediction_id) DO NOTHING",
                    (experiment_id, prediction_id, iso(linked_at)),
                ).rowcount
                == 1
            )

    def save_forward_prediction(self, record: Mapping[str, Any]) -> int:
        values = self._research_values(record, FORWARD_PREDICTION_FIELDS)
        columns = ", ".join(FORWARD_PREDICTION_FIELDS)
        with self.connection:
            return (
                self.connection.execute(
                    f"INSERT INTO forward_predictions ({columns}) "
                    f"VALUES ({', '.join('?' for _ in values)}) "
                    "ON CONFLICT(model_version_id, market_id, timestamp_utc) DO NOTHING",
                    values,
                ).lastrowid
                or self.connection.execute(
                    "SELECT id FROM forward_predictions WHERE model_version_id=? AND market_id=? "
                    "AND timestamp_utc=?",
                    (record["model_version_id"], record["market_id"], record["timestamp_utc"]),
                ).fetchone()[0]
            )

    def attach_forward_settlement(
        self,
        market_id: str | MarketResolution | Mapping[str, Any],
        outcome: str | None = None,
        paper_pnl: str | None = None,
    ) -> int:
        if isinstance(market_id, MarketResolution):
            resolution = market_id
            market_id, outcome = resolution.market_id, resolution.outcome
        elif isinstance(market_id, Mapping):
            result = market_id
            market_id, outcome = result["market_id"], result["outcome"]
            paper_pnl = result.get("paper_pnl", paper_pnl)
        if outcome is None:
            raise ValueError("Forward settlement outcome is required")
        if outcome not in {"UP", "DOWN"}:
            raise ValueError("Forward settlement outcome must be UP or DOWN")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE forward_predictions SET official_outcome=?, "
                "paper_pnl=COALESCE(?, paper_pnl) "
                "WHERE market_id=? AND official_outcome IS NULL",
                (outcome, paper_pnl, market_id),
            )
            self.connection.execute(
                "UPDATE forward_paper_trades SET official_outcome=?, "
                "net_pnl=COALESCE(?, net_pnl) "
                "WHERE market_id=? AND official_outcome IS NULL",
                (outcome, paper_pnl, market_id),
            )
        return cursor.rowcount

    def save_forward_paper_trade(self, record: Mapping[str, Any]) -> int:
        values = self._research_values(record, FORWARD_PAPER_TRADE_FIELDS)
        columns = ", ".join(FORWARD_PAPER_TRADE_FIELDS)
        with self.connection:
            return self.connection.execute(
                f"INSERT INTO forward_paper_trades ({columns}) "
                f"VALUES ({', '.join('?' for _ in values)}) "
                "ON CONFLICT(model_version_id, market_id) DO NOTHING",
                values,
            ).lastrowid

    def close_hf_connection(
        self,
        connection_id: int,
        disconnected_at: datetime,
        status: str = "disconnected",
        error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE hf_connections SET disconnected_at_utc=?, status=?, error=? WHERE id=?",
                (iso(disconnected_at), status, error, connection_id),
            )

    def close_elapsed_markets(self, now: datetime) -> list[str]:
        with self.connection:
            rows = self.connection.execute(
                "UPDATE markets SET closed=1, updated_at_utc=? "
                "WHERE closed=0 AND end_time_utc <= ? RETURNING market_id",
                (iso(now), iso(now)),
            ).fetchall()
        return [row[0] for row in rows]

    def pending_settlements(self, now: datetime, limit: int = 10) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM markets WHERE resolved=0 AND end_time_utc <= ? "
                "AND (next_settlement_check_utc IS NULL OR next_settlement_check_utc <= ?) "
                "ORDER BY COALESCE(settlement_checked_at_utc, created_at_utc) LIMIT ?",
                (iso(now), iso(now), limit),
            )
        ]

    def record_settlement_check(
        self, market_id: str, now: datetime, interval: float, failed: bool = False
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE markets SET settlement_checked_at_utc=?, next_settlement_check_utc=?, "
                "settlement_error_count=settlement_error_count+?, "
                "updated_at_utc=? WHERE market_id=?",
                (
                    iso(now),
                    iso(now + timedelta(seconds=interval)),
                    int(failed),
                    iso(now),
                    market_id,
                ),
            )

    def save_resolution(self, resolution: MarketResolution) -> bool:
        market = self.get_market(resolution.market_id)
        if market is None:
            raise ValueError("Cannot resolve an unknown market")
        end = datetime.fromisoformat(market["end_time_utc"])
        if resolution.observed_at_utc < end or (
            resolution.resolution_time_utc is not None and resolution.resolution_time_utc < end
        ):
            raise ValueError("Cannot resolve a market before its window ends")
        if market["resolved"]:
            if market["outcome"] != resolution.outcome:
                raise ValueError("Conflicting official settlement; existing evidence retained")
            return False
        with self.connection:
            self.connection.execute(
                "UPDATE markets SET closed=1, resolved=1, outcome=?, resolution_time_utc=?, "
                "resolution_time_source=?, resolution_observed_at_utc=?, resolution_source=?, "
                "updated_at_utc=? WHERE market_id=?",
                (
                    resolution.outcome,
                    None
                    if resolution.resolution_time_utc is None
                    else iso(resolution.resolution_time_utc),
                    resolution.resolution_time_source,
                    iso(resolution.observed_at_utc),
                    resolution.source,
                    iso(utc_now()),
                    resolution.market_id,
                ),
            )
        return True

    def start_run(self, now: datetime, interval: float) -> int:
        with self.connection:
            return self.connection.execute(
                "INSERT INTO collection_runs (started_at_utc, heartbeat_at_utc, interval_seconds) "
                "VALUES (?, ?, ?)",
                (iso(now), iso(now), interval),
            ).lastrowid

    def finish_run(self, run_id: int, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE collection_runs SET heartbeat_at_utc=?, stopped_at_utc=? WHERE id=?",
                (iso(now), iso(now), run_id),
            )

    def start_poll(self, run_id: int, scheduled: datetime, now: datetime) -> int:
        slug = f"btc-updown-5m-{int(now.timestamp()) // 300 * 300}"
        with self.connection:
            self.connection.execute(
                "UPDATE collection_runs SET heartbeat_at_utc=? WHERE id=?", (iso(now), run_id)
            )
            return self.connection.execute(
                "INSERT INTO polls (run_id, scheduled_at_utc, started_at_utc, market_slug, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (run_id, iso(scheduled), iso(now), slug),
            ).lastrowid

    def finish_poll(
        self,
        poll_id: int,
        now: datetime,
        latency_ms: float,
        failure_kind: str | None = None,
        market_id: str | None = None,
    ) -> None:
        status = "success" if failure_kind is None else "failed"
        if failure_kind == "KeyboardInterrupt":
            status = "interrupted"
        with self.connection:
            self.connection.execute(
                "UPDATE polls SET finished_at_utc=?, poll_latency_ms=?, status=?, failure_kind=?, "
                "market_id=?, market_slug=COALESCE((SELECT slug FROM markets WHERE market_id=?), "
                "market_slug) WHERE id=?",
                (iso(now), latency_ms, status, failure_kind, market_id, market_id, poll_id),
            )
            self.connection.execute(
                "UPDATE collection_runs SET heartbeat_at_utc=? "
                "WHERE id=(SELECT run_id FROM polls WHERE id=?)",
                (iso(now), poll_id),
            )
