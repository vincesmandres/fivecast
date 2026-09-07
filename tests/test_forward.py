import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fivecast.forward import FrozenPolicy, load_artifact
from fivecast.model import FrozenLogistic, LogisticConfig, Scaler


def test_frozen_policy_has_explicit_nonnegative_costs():
    policy = FrozenPolicy()
    assert policy.minimum_net_edge == Decimal("0.02")
    assert policy.fee == policy.slippage == Decimal("0.01")


def test_artifact_rejects_feature_or_dataset_identity_mismatch():
    artifact = {
        "intercept": 0,
        "coefficients": [0] * 16,
        "scaler_means": [0] * 16,
        "scaler_scales": [1] * 16,
        "feature_names": ["bad"],
        "config": {"learning_rate": 0.1, "iterations": 1, "l2": 0},
        "config_fingerprint": "x",
        "dataset_fingerprint": "expected",
        "training_cutoff_utc": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(ValueError, match="identity"):
        load_artifact({"artifact_json": json.dumps(artifact), "dataset_fingerprint": "expected"})


def test_frozen_model_has_no_training_method():
    model = FrozenLogistic(
        0,
        (0,) * 16,
        Scaler((0,) * 16, (1,) * 16),
        tuple(str(n) for n in range(16)),
        LogisticConfig(),
        "x",
        "y",
        datetime.now(UTC),
    )
    assert not hasattr(model, "train")
