import json
from datetime import UTC, datetime

import pytest

from fivecast.m6 import collect_once, manifest
from fivecast.model import FEATURE_NAMES, LogisticConfig, _fingerprint
from fivecast.storage.sqlite import SnapshotStore


def test_manifest_is_deterministic_and_carries_frozen_protocol():
    config = LogisticConfig()
    model = {
        "version": "m5",
        "fingerprint": "model",
        "training_cutoff_utc": "2026-01-01T00:00:00+00:00",
        "dataset_fingerprint": "data",
        "artifact_json": json.dumps(
            {
                "config": {
                    "learning_rate": config.learning_rate,
                    "iterations": config.iterations,
                    "l2": config.l2,
                },
                "config_fingerprint": _fingerprint(config),
                "dataset_fingerprint": "data",
                "feature_names": FEATURE_NAMES,
                "training_cutoff_utc": "2026-01-01T00:00:00+00:00",
            }
        ),
    }
    value = manifest(model, datetime(2026, 1, 2, tzinfo=UTC))
    assert value["minimum_resolved_markets"] == 200
    assert value["minimum_eligible_records"] == 50
    assert value["edge_buckets"] == ["<=0", "0-0.02", "0.02-0.05", "0.05-0.08", ">0.08"]


def _model_record(version="m5-logistic-v1", dataset="dataset"):
    config = LogisticConfig()
    artifact = {
        "config": {
            "learning_rate": config.learning_rate,
            "iterations": config.iterations,
            "l2": config.l2,
        },
        "config_fingerprint": _fingerprint(config),
        "dataset_fingerprint": dataset,
        "feature_names": FEATURE_NAMES,
        "training_cutoff_utc": "2026-09-06T09:59:55.620000+00:00",
        "intercept": 0.0,
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "scaler_means": [0.0] * len(FEATURE_NAMES),
        "scaler_scales": [1.0] * len(FEATURE_NAMES),
    }
    encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    return {
        "version": version,
        "artifact_json": encoded,
        "fingerprint": __import__("hashlib").sha256(encoded.encode()).hexdigest(),
        "dataset_fingerprint": dataset,
        "training_cutoff_utc": artifact["training_cutoff_utc"],
        "created_at_utc": "2026-09-06T10:00:00.000000+00:00",
    }


def test_m6_start_resume_is_idempotent_and_model_binding_is_immutable(tmp_path):
    path = tmp_path / "m6.db"
    with SnapshotStore(path) as store:
        model_id = store.persist_model_version(_model_record())
        other_id = store.persist_model_version(_model_record("m5-logistic-v2", "dataset-2"))

    first = __import__("fivecast.m6", fromlist=["start"]).start(path, model_id, "fixed")
    second = __import__("fivecast.m6", fromlist=["start"]).start(path, model_id, "fixed")
    assert second == first
    with SnapshotStore(path) as store:
        row = store.get_m6_experiment(first)
        assert store.connection.execute("SELECT COUNT(*) FROM m6_experiments").fetchone()[0] == 1
        original = (row["manifest_json"], row["manifest_hash"], row["experiment_start_utc"])
    with SnapshotStore(path) as store:
        row = store.get_m6_experiment("fixed")
        assert (row["manifest_json"], row["manifest_hash"], row["experiment_start_utc"]) == original
    with pytest.raises(ValueError, match="another model"):
        __import__("fivecast.m6", fromlist=["start"]).start(path, other_id, "fixed")


def test_m6_storage_rejects_changed_manifest(tmp_path):
    path = tmp_path / "m6.db"
    with SnapshotStore(path) as store:
        model_id = store.persist_model_version(_model_record())
        record = {
            "experiment_id": "fixed",
            "model_version_id": model_id,
            "experiment_start_utc": "2026-09-06T10:00:00.000000+00:00",
            "manifest_json": "{}",
            "manifest_hash": "one",
            "status": "active",
            "created_at_utc": "2026-09-06T10:00:00.000000+00:00",
        }
        store.save_m6_experiment(record)
        with pytest.raises(ValueError, match="immutable"):
            store.save_m6_experiment({**record, "manifest_hash": "two"})


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-09-06T09:59:55.619999+00:00", "ignored"),
        ("2026-09-06T10:00:00.000000+00:00", "rejected"),
    ],
)
def test_m6_manifest_start_boundary_is_strict(tmp_path, monkeypatch, timestamp, expected):
    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_m6_experiment(self, value):
            return {
                "id": 1,
                "model_version_id": 1,
                "experiment_start_utc": "2026-09-06T10:00:00.000000+00:00",
            }

        def get_model_version(self, value):
            return {"training_cutoff_utc": "2026-09-06T09:00:00.000000+00:00"}

        def get_latest_snapshot(self):
            return type("S", (), {"timestamp_utc": datetime.fromisoformat(timestamp)})()

    monkeypatch.setattr("fivecast.m6.SnapshotStore", lambda _: Store())
    monkeypatch.setattr("fivecast.m6.predict_once", lambda *_: 1)
    if expected == "ignored":
        assert collect_once(tmp_path / "data.db", 1) is None
    else:
        with pytest.raises(ValueError, match="manifest start"):
            collect_once(tmp_path / "data.db", 1)


def test_m6_cutoff_boundary_is_rejected(tmp_path, monkeypatch):
    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_m6_experiment(self, value):
            return {
                "id": 1,
                "model_version_id": 1,
                "experiment_start_utc": "2026-09-06T09:00:00+00:00",
            }

        def get_model_version(self, value):
            return {"training_cutoff_utc": "2026-09-06T10:00:00+00:00"}

        def get_latest_snapshot(self):
            return type(
                "S", (), {"timestamp_utc": datetime.fromisoformat("2026-09-06T10:00:00+00:00")}
            )()

    monkeypatch.setattr("fivecast.m6.SnapshotStore", lambda _: Store())
    with pytest.raises(ValueError, match="pre-cutoff"):
        collect_once(tmp_path / "data.db", 1)


def test_m6_rejects_pre_manifest_snapshot_without_predicting(tmp_path, monkeypatch):
    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_m6_experiment(self, value):
            return {
                "id": 1,
                "model_version_id": 1,
                "experiment_start_utc": "2026-01-02T00:00:00+00:00",
            }

        def get_model_version(self, value):
            return {"training_cutoff_utc": "2026-01-01T00:00:00+00:00"}

        def get_latest_snapshot(self):
            return type("S", (), {"timestamp_utc": datetime(2026, 1, 1, 12, tzinfo=UTC)})()

    monkeypatch.setattr("fivecast.m6.SnapshotStore", lambda _: Store())
    assert collect_once(tmp_path / "data.db", 1) is None
