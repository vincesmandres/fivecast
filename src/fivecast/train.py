"""Freeze, train, validate, and persist one interpretable historical model."""

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fivecast.model import Dataset, FrozenLogistic, LogisticConfig, build_dataset, evaluate, train
from fivecast.storage.migrations import iso
from fivecast.storage.sqlite import SnapshotStore


def artifact(model: FrozenLogistic, dataset: Dataset) -> dict:
    return {
        "model": "regularized_logistic_regression",
        "model_version": "m5-logistic-v1",
        "intercept": model.intercept,
        "coefficients": model.coefficients,
        "scaler_means": model.scaler.means,
        "scaler_scales": model.scaler.scales,
        "feature_names": model.feature_names,
        "config": asdict(model.config),
        "config_fingerprint": model.config_fingerprint,
        "dataset_fingerprint": model.freeze_fingerprint,
        "training_cutoff_utc": iso(model.trained_cutoff_utc),
        "dataset_freeze": {
            "cutoff_utc": iso(dataset.freeze.cutoff_utc),
            "eligible_market_ids": dataset.freeze.eligible_market_ids,
            "rejected_market_ids": dataset.freeze.rejected_market_ids,
            "rejection_reasons": dataset.freeze.rejection_reasons,
            "feature_names": dataset.freeze.feature_names,
            "provenance": dataset.freeze.provenance,
        },
    }


def baseline_probabilities(dataset: Dataset) -> tuple[float, ...]:
    return tuple(row.as_dict()["up_ask"] for row in dataset.validation)


def evaluate_probabilities(
    probabilities: Sequence[float], labels: Sequence[int]
) -> dict[str, float]:
    import math

    if not probabilities or len(probabilities) != len(labels):
        raise ValueError("Baseline requires non-empty aligned probabilities and labels")
    epsilon = 1e-15
    return {
        "brier": sum(
            (value - label) ** 2 for value, label in zip(probabilities, labels, strict=True)
        )
        / len(labels),
        "logloss": -sum(
            label * math.log(max(epsilon, value)) + (1 - label) * math.log(max(epsilon, 1 - value))
            for value, label in zip(probabilities, labels, strict=True)
        )
        / len(labels),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze and train one research-only M5 logistic model"
    )
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument("--version", default="m5-logistic-v1")
    args = parser.parse_args(argv)
    dataset = build_dataset(args.db_path)
    model = train(dataset, LogisticConfig())
    validation = evaluate(model, dataset.validation, dataset.validation_labels)
    baseline = evaluate_probabilities(baseline_probabilities(dataset), dataset.validation_labels)
    payload = artifact(model, dataset)
    fingerprint = (
        __import__("hashlib")
        .sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    with SnapshotStore(args.db_path) as store:
        model_id = store.persist_model_version(
            {
                "version": args.version,
                "artifact_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "fingerprint": fingerprint,
                "dataset_fingerprint": dataset.freeze.fingerprint,
                "training_cutoff_utc": iso(dataset.freeze.cutoff_utc),
                "created_at_utc": iso(datetime.now(UTC)),
            }
        )
    print(f"model_version_id={model_id} version={args.version}")
    print(f"dataset_fingerprint={dataset.freeze.fingerprint}")
    print(
        "markets",
        f"train={len(set(row.market_id for row in dataset.train))}",
        f"validation={len(set(row.market_id for row in dataset.validation))}",
    )
    print(
        "rows",
        f"train={len(dataset.train)}",
        f"validation={len(dataset.validation)}",
        f"rejected_rows={len(dataset.row_rejections)}",
    )
    print(f"market_baseline brier={baseline['brier']:.6f} logloss={baseline['logloss']:.6f}")
    print(
        "logistic",
        f"brier={validation.brier:.6f}",
        f"logloss={validation.logloss:.6f}",
        f"auc={validation.roc_auc}",
        f"accuracy={validation.accuracy:.6f}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
