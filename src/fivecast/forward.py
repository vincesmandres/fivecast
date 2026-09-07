"""Frozen-artifact forward research records; no orders, wallets, or retraining."""

import argparse
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fivecast.fair_value import FrictionConfig, fair_value
from fivecast.model import (
    FEATURE_NAMES,
    FeatureRow,
    FrozenLogistic,
    LogisticConfig,
    Scaler,
    _fingerprint,
    _row,
)
from fivecast.models import MarketSnapshot, SnapshotQuality
from fivecast.replay import ReplaySnapshot
from fivecast.storage.migrations import iso
from fivecast.storage.sqlite import SnapshotStore


@dataclass(frozen=True, slots=True)
class FrozenPolicy:
    minimum_net_edge: Decimal = Decimal("0.02")
    max_spread: Decimal = Decimal("0.05")
    max_skew_ms: float = 5000
    fee: Decimal = Decimal("0.01")
    slippage: Decimal = Decimal("0.01")
    latency: Decimal = Decimal("0")


def load_artifact(record: dict) -> FrozenLogistic:
    value = json.loads(record["artifact_json"])
    required = {
        "intercept",
        "coefficients",
        "scaler_means",
        "scaler_scales",
        "feature_names",
        "config",
        "config_fingerprint",
        "dataset_fingerprint",
        "training_cutoff_utc",
    }
    if not required.issubset(value):
        raise ValueError("Frozen model artifact is incomplete")
    config = LogisticConfig(**value["config"])
    if (
        tuple(value["feature_names"]) != FEATURE_NAMES
        or value["dataset_fingerprint"] != record["dataset_fingerprint"]
        or value["training_cutoff_utc"] != record["training_cutoff_utc"]
        or value["config_fingerprint"] != _fingerprint(config)
        or config != LogisticConfig()
        or not (
            len(value["coefficients"]) == len(FEATURE_NAMES)
            and len(value["scaler_means"]) == len(FEATURE_NAMES)
            and len(value["scaler_scales"]) == len(FEATURE_NAMES)
        )
    ):
        raise ValueError("Frozen model artifact identity does not match its persisted record")
    return FrozenLogistic(
        value["intercept"],
        tuple(value["coefficients"]),
        Scaler(tuple(value["scaler_means"]), tuple(value["scaler_scales"])),
        tuple(value["feature_names"]),
        config,
        value["config_fingerprint"],
        value["dataset_fingerprint"],
        datetime.fromisoformat(value["training_cutoff_utc"]),
    )


def _feature_at(store: SnapshotStore, market_id: str, timestamp: str) -> FeatureRow:
    rows = store.connection.execute(
        "SELECT * FROM snapshots WHERE market_id=? AND timestamp_utc<=? ORDER BY timestamp_utc",
        (market_id, timestamp),
    ).fetchall()
    prefix = []
    for value in rows:
        snapshot = MarketSnapshot.model_validate(
            {key: value[key] for key in MarketSnapshot.model_fields}
        )
        quality = SnapshotQuality(
            btc_source_timestamp=value["btc_source_timestamp"],
            market_source_timestamp=value["market_source_timestamp"],
            source_skew_ms=value["source_skew_ms"],
            poll_latency_ms=value["poll_latency_ms"],
            is_stale=value["is_stale"],
        )
        prefix.append(ReplaySnapshot(snapshot, quality, None))
    row, reason = _row(prefix)
    if row is None:
        raise ValueError(f"Forward feature row unavailable: {reason}")
    return row


def predict_once(store: SnapshotStore, model_id: int, policy: FrozenPolicy) -> int:
    attach_known_settlements(store)
    record = store.get_model_version(model_id)
    if record is None:
        raise ValueError(f"Unknown frozen model {model_id}")
    model = load_artifact(record)
    latest = store.get_latest_snapshot()
    if latest is None:
        raise ValueError("No snapshot available for forward prediction")
    if latest.timestamp_utc <= model.trained_cutoff_utc:
        raise ValueError("Forward prediction timestamp must be after frozen training cutoff")
    row = _feature_at(store, latest.market_id, iso(latest.timestamp_utc))
    probability = Decimal(str(model.predict_proba(row)))
    up = fair_value(
        probability, row, "UP", FrictionConfig(policy.fee, policy.slippage, policy.latency)
    )
    down = fair_value(
        probability, row, "DOWN", FrictionConfig(policy.fee, policy.slippage, policy.latency)
    )
    selected = up if up.estimated_net_edge >= down.estimated_net_edge else down
    values = row.as_dict()
    reason = None
    if selected.estimated_net_edge < policy.minimum_net_edge:
        reason = "NET_EDGE_BELOW_MINIMUM"
    elif (values["up_spread"] if selected.side == "UP" else values["down_spread"]) > float(
        policy.max_spread
    ):
        reason = "SPREAD_TOO_WIDE"
    elif values["source_skew_ms"] > policy.max_skew_ms:
        reason = "SKEW_TOO_HIGH"
    elif values["is_stale"] != 0:
        reason = "STALE_SNAPSHOT"
    eligible = reason is None
    features_json = json.dumps(row.as_dict(), sort_keys=True, separators=(",", ":"))
    prediction_id = store.save_forward_prediction(
        {
            "model_version_id": model_id,
            "market_id": latest.market_id,
            "timestamp_utc": iso(latest.timestamp_utc),
            "features_json": features_json,
            "features_fingerprint": hashlib.sha256(features_json.encode()).hexdigest(),
            "predicted_up_probability": str(probability),
            "up_ask": str(latest.up_ask),
            "down_ask": str(latest.down_ask),
            "selected_side": selected.side,
            "selected_ask": str(selected.ask),
            "raw_edge": str(selected.raw_edge),
            "estimated_fees": str(policy.fee),
            "estimated_slippage": str(policy.slippage),
            "estimated_latency": str(policy.latency),
            "net_edge": str(selected.estimated_net_edge),
            "eligible": int(eligible),
            "rejection_reason": reason,
            "official_outcome": None,
            "paper_pnl": None,
            "created_at_utc": iso(datetime.now(UTC)),
        }
    )
    if eligible:
        store.save_forward_paper_trade(
            {
                "prediction_id": prediction_id,
                "model_version_id": model_id,
                "market_id": latest.market_id,
                "timestamp_utc": iso(latest.timestamp_utc),
                "side": selected.side,
                "entry_price": str(selected.ask),
                "gross_pnl": None,
                "estimated_fees": str(policy.fee),
                "estimated_slippage": str(policy.slippage),
                "estimated_latency": str(policy.latency),
                "net_pnl": None,
                "official_outcome": None,
                "created_at_utc": iso(datetime.now(UTC)),
            }
        )
    print(
        f"FORWARD model={record['version']} market={latest.market_id} "
        f"probability_up={probability:.6f} side={selected.side} "
        f"net_edge={selected.estimated_net_edge} eligible={eligible} reason={reason}"
    )
    return prediction_id


def attach_known_settlements(store: SnapshotStore) -> int:
    """Attach only independently persisted official outcomes to isolated forward rows."""
    rows = store.connection.execute(
        "SELECT p.market_id, p.selected_side, p.selected_ask, p.estimated_fees, "
        "p.estimated_slippage, p.estimated_latency, m.outcome "
        "FROM forward_predictions p JOIN markets m ON m.market_id=p.market_id "
        "WHERE p.official_outcome IS NULL AND m.resolved=1 AND m.outcome IN ('UP', 'DOWN')"
    ).fetchall()
    for row in rows:
        payout = Decimal(1) if row["selected_side"] == row["outcome"] else Decimal(0)
        entry = Decimal(row["selected_ask"])
        gross = payout - entry
        net = (
            gross
            - Decimal(row["estimated_fees"])
            - Decimal(row["estimated_slippage"])
            - Decimal(row["estimated_latency"])
        )
        store.attach_forward_settlement(row["market_id"], row["outcome"], str(net))
    return len(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Frozen-model forward research records only")
    parser.add_argument("--model-version", required=True, type=int)
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    with SnapshotStore(args.db_path) as store:
        while True:
            predict_once(store, args.model_version, FrozenPolicy())
            if args.once:
                break
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
