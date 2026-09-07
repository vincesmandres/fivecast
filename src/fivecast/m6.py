"""Immutable prospective manifest and frozen-model forward evidence collection."""

import argparse
import hashlib
import json
import os
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fivecast.config import Settings
from fivecast.feeds.http import RetryingClient
from fivecast.feeds.polymarket import PolymarketFeed
from fivecast.forward import FrozenPolicy, attach_known_settlements, predict_once
from fivecast.model import FEATURE_NAMES, LogisticConfig, _fingerprint
from fivecast.storage.migrations import iso
from fivecast.storage.sqlite import SnapshotStore


def _default_db_path() -> Path:
    return Path(os.environ.get("FIVECAST_DB_PATH", "data/fivecast.db"))


def manifest(model: dict, started: datetime) -> dict:
    artifact = json.loads(model["artifact_json"])
    config = LogisticConfig(**artifact["config"])
    if (
        tuple(artifact["feature_names"]) != FEATURE_NAMES
        or config != LogisticConfig()
        or artifact["config_fingerprint"] != _fingerprint(config)
        or artifact["dataset_fingerprint"] != model["dataset_fingerprint"]
        or artifact["training_cutoff_utc"] != model["training_cutoff_utc"]
    ):
        raise ValueError("M6 manifest does not match the frozen M5 policy")
    return {
        "schema": "fivecast-m6-forward-v1",
        "model_version": model["version"],
        "model_fingerprint": model["fingerprint"],
        "dataset_cutoff_utc": model["training_cutoff_utc"],
        "dataset_fingerprint": model["dataset_fingerprint"],
        "configuration_fingerprint": artifact["config_fingerprint"],
        "feature_names": artifact["feature_names"],
        "quality_filters": {"max_spread": "0.05", "max_skew_ms": 5000, "is_stale": False},
        "friction": {"fees": "0.01", "slippage": "0.01", "latency": "0"},
        "minimum_net_edge": "0.02",
        "one_paper_record_per_market": True,
        "preregistration": "docs/M5_PREREGISTRATION.md",
        "start_utc": iso(started),
        "minimum_resolved_markets": 200,
        "minimum_eligible_records": 50,
        "bootstrap_seed": 20260906,
        "bootstrap_replicates": 1000,
        "edge_buckets": ["<=0", "0-0.02", "0.02-0.05", "0.05-0.08", ">0.08"],
    }


def start(path: Path, model_id: int, experiment_id: str | None = None) -> int:
    with SnapshotStore(path) as store:
        model = store.get_model_version(model_id)
        if model is None:
            raise ValueError(f"Unknown frozen model {model_id}")
        if experiment_id is not None:
            existing = store.get_m6_experiment(experiment_id)
            if existing is not None:
                if existing["model_version_id"] != model_id:
                    raise ValueError("Existing M6 experiment is bound to another model version")
                return existing["id"]
        now = datetime.now(UTC)
        body = manifest(model, now)
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        identity = experiment_id or f"m6-{now:%Y%m%d-%H%M%S}-{digest[:8]}"
        return store.save_m6_experiment(
            {
                "experiment_id": identity,
                "model_version_id": model_id,
                "experiment_start_utc": iso(now),
                "manifest_json": encoded,
                "manifest_hash": digest,
                "status": "active",
                "created_at_utc": iso(now),
            }
        )


def collect_once(path: Path, experiment: int) -> int | None:
    with SnapshotStore(path) as store:
        current = store.get_m6_experiment(experiment)
        if current is None:
            raise ValueError(f"Unknown M6 experiment {experiment}")
        model = store.get_model_version(current["model_version_id"])
        if model is None:
            raise ValueError("Manifest references missing frozen model")
        latest = store.get_latest_snapshot()
        experiment_start = datetime.fromisoformat(current["experiment_start_utc"])
        if latest is None or latest.timestamp_utc < experiment_start:
            return None
        if latest.timestamp_utc <= datetime.fromisoformat(model["training_cutoff_utc"]):
            raise ValueError("M6 rejects pre-cutoff evidence")
        if latest.timestamp_utc == experiment_start:
            raise ValueError("M6 rejects evidence at manifest start")
        try:
            prediction = predict_once(store, current["model_version_id"], FrozenPolicy())
        except ValueError as error:
            if not str(error).startswith("Forward feature row unavailable:"):
                raise
            return None
        store.link_m6_prediction(current["id"], prediction, datetime.now(UTC))
        return prediction


def settle_once(path: Path) -> int:
    """Use the approved public settlement path, then attach stored official outcomes."""
    from fivecast.collector import settle_pending

    stop = threading.Event()
    settings = Settings(db_path=path)
    with RetryingClient(settings, stop) as client, SnapshotStore(path) as store:
        settle_pending(store, PolymarketFeed(client), settings, stop)
        return attach_known_settlements(store)


def _print_status(path: Path, experiment: int, detailed: bool = False) -> None:
    from fivecast.m6_report import format_report, report

    print(format_report(report(path, experiment, detailed), detailed))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FiveCast M6 prospective frozen forward experiment"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start")
    begin.add_argument("--model-version", required=True, type=int)
    begin.add_argument("--experiment-id")
    collect = commands.add_parser("collect")
    collect.add_argument("--experiment", required=True, type=int)
    collect.add_argument("--interval", type=float, default=5)
    collect.add_argument("--once", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--experiment", required=True, type=int)
    report = commands.add_parser("report")
    report.add_argument("--experiment", required=True, type=int)
    report.add_argument("--detailed", action="store_true")
    settle = commands.add_parser("settle")
    for command in (begin, collect, status, report, settle):
        command.add_argument("--db-path", type=Path, default=_default_db_path())
    args = parser.parse_args(argv)
    if args.command == "start":
        print(f"experiment_id={start(args.db_path, args.model_version, args.experiment_id)}")
        return 0
    if args.command == "status":
        _print_status(args.db_path, args.experiment)
        return 0
    if args.command == "report":
        _print_status(args.db_path, args.experiment, args.detailed)
        return 0
    if args.command == "settle":
        print(f"Attached settlements={settle_once(args.db_path)}")
        return 0
    if args.interval <= 0:
        parser.error("--interval must be positive")
    try:
        while True:
            value = collect_once(args.db_path, args.experiment)
            print("No new prospective snapshot." if value is None else f"Linked prediction={value}")
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("M6 collection stopped; persisted evidence is resumable.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
