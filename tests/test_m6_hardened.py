import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fivecast.fair_value import FrictionConfig, fair_value
from fivecast.forward import FrozenPolicy, load_artifact, predict_once
from fivecast.m6 import main, manifest, start
from fivecast.m6_report import _robustness, _time_blocks, bootstrap, report
from fivecast.model import FEATURE_NAMES, FeatureRow, LogisticConfig, _fingerprint
from fivecast.storage.sqlite import SnapshotStore


def _valid_artifact(**changes):
    config = LogisticConfig()
    value = {
        "config": {"learning_rate": 0.1, "iterations": 1000, "l2": 0.01},
        "config_fingerprint": _fingerprint(config),
        "dataset_fingerprint": "dataset",
        "feature_names": list(FEATURE_NAMES),
        "training_cutoff_utc": "2026-09-06T09:59:55.620000+00:00",
        "intercept": 0.0,
        "coefficients": [0.0] * 16,
        "scaler_means": [0.0] * 16,
        "scaler_scales": [1.0] * 16,
    }
    value.update(changes)
    return value


def test_manifest_rejects_changed_frozen_configuration():
    artifact = _valid_artifact(config={"learning_rate": 0.2, "iterations": 1000, "l2": 0.01})
    model = {
        "version": "m5-logistic-v1",
        "fingerprint": "model",
        "training_cutoff_utc": artifact["training_cutoff_utc"],
        "dataset_fingerprint": "dataset",
        "artifact_json": json.dumps(artifact),
    }
    with pytest.raises(ValueError, match="frozen M5 policy"):
        manifest(model, datetime.now(UTC))


def test_load_artifact_rejects_cutoff_and_shape_tampering():
    artifact = _valid_artifact(training_cutoff_utc="2026-09-07T00:00:00+00:00")
    record = {
        "artifact_json": json.dumps(artifact),
        "dataset_fingerprint": "dataset",
        "training_cutoff_utc": "2026-09-06T09:59:55.620000+00:00",
    }
    with pytest.raises(ValueError, match="identity"):
        load_artifact(record)
    artifact = _valid_artifact(coefficients=[0.0])
    with pytest.raises(ValueError, match="identity"):
        load_artifact({**record, "artifact_json": json.dumps(artifact)})


def test_frozen_policy_uses_executable_side_asks_and_exact_costs():
    values = (
        10.0,
        0.001,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        100.0,
        0.50,
        0.60,
        0.05,
        0.65,
        0.10,
        0.10,
        100.0,
        0.0,
    )
    row = FeatureRow("m", datetime.now(UTC), values)
    policy = FrozenPolicy()
    up = fair_value(
        Decimal("0.80"), row, "UP", FrictionConfig(policy.fee, policy.slippage, policy.latency)
    )
    down = fair_value(
        Decimal("0.80"), row, "DOWN", FrictionConfig(policy.fee, policy.slippage, policy.latency)
    )
    assert up.ask == Decimal("0.60")
    assert down.ask == Decimal("0.65")
    assert up.raw_edge == Decimal("0.20")
    assert down.raw_edge == Decimal("-0.45")
    assert up.estimated_net_edge == Decimal("0.18")


@pytest.mark.parametrize(
    ("p", "up_ask", "down_ask", "up_spread", "skew", "stale", "reason"),
    [
        (0.55, 0.54, 0.46, 0.01, 100, 0, "NET_EDGE_BELOW_MINIMUM"),
        (0.90, 0.85, 0.10, 0.06, 100, 0, "SPREAD_TOO_WIDE"),
        (0.90, 0.70, 0.10, 0.01, 5001, 0, "SKEW_TOO_HIGH"),
        (0.90, 0.70, 0.10, 0.01, 100, 1, "STALE_SNAPSHOT"),
    ],
)
def test_policy_rejection_reasons_are_frozen(
    monkeypatch, p, up_ask, down_ask, up_spread, skew, stale, reason
):
    class Result:
        def fetchall(self):
            return []

    class Connection:
        def execute(self, *_args):
            return Result()

    class Store:
        connection = Connection()

        def get_model_version(self, _):
            return {"version": "m5-logistic-v1", "artifact_json": "{}"}

        def get_latest_snapshot(self):
            return type(
                "Snapshot",
                (),
                {
                    "market_id": "m",
                    "timestamp_utc": datetime(2026, 9, 6, 10, 1, tzinfo=UTC),
                    "up_ask": Decimal(str(up_ask)),
                    "down_ask": Decimal(str(down_ask)),
                },
            )()

        def save_forward_prediction(self, record):
            self.record = record
            return 1

        def save_forward_paper_trade(self, record):
            self.trade = record

    row_values = [
        10.0,
        0.001,
        1.0,
        1.0,
        1.0,
        0.0,
        0.0,
        100.0,
        0.50,
        up_ask,
        0.09,
        down_ask,
        up_spread,
        0.01,
        skew,
        stale,
    ]
    store = Store()
    monkeypatch.setattr(
        "fivecast.forward.load_artifact",
        lambda _: type(
            "Model",
            (),
            {
                "trained_cutoff_utc": datetime(2026, 9, 6, 9, tzinfo=UTC),
                "predict_proba": lambda *_: p,
            },
        )(),
    )
    monkeypatch.setattr(
        "fivecast.forward._feature_at",
        lambda *_: FeatureRow("m", datetime(2026, 9, 6, 10, 1, tzinfo=UTC), tuple(row_values)),
    )
    prediction = predict_once(store, 1, FrozenPolicy())
    assert prediction == 1
    assert store.record["eligible"] == 0
    assert store.record["rejection_reason"] == reason


def test_bootstrap_is_market_level_deterministic_and_n_a_for_one_market():
    assert bootstrap([1.0], 20260906) is None
    first = bootstrap([0.0, 1.0, 0.5], 20260906, 1000)
    second = bootstrap([0.0, 1.0, 0.5], 20260906, 1000)
    assert first == second
    assert first is not None
    assert 0 <= first[0] <= first[1] <= 1


def test_m6_report_is_read_only_and_inconclusive_without_minimums(tmp_path):
    path = tmp_path / "report.db"
    artifact = _valid_artifact()
    encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    record = {
        "version": "m5-logistic-v1",
        "artifact_json": encoded,
        "fingerprint": "model-report",
        "dataset_fingerprint": "dataset",
        "training_cutoff_utc": artifact["training_cutoff_utc"],
        "created_at_utc": "2026-09-06T10:00:00.000000+00:00",
    }
    with SnapshotStore(path) as store:
        model_id = store.persist_model_version(record)
    experiment_id = start(path, model_id, "m6-report")
    before = path.read_bytes()
    value = report(path, experiment_id, detailed=True)
    assert value["status"] == "INCONCLUSIVE"
    assert value["net_pnl_ci_95"] is None
    assert value["calendar_days"] == {}
    assert all(block["low_n"] for block in value["time_of_day_blocks_utc"].values())
    assert value["robustness"]["pnl_by_calendar_day"] == {}
    assert value["data_quality"]["missing_settlements"] == 0
    assert path.read_bytes() == before


def test_m6_cli_stops_cleanly_on_ctrl_c(monkeypatch, tmp_path, capsys):
    def stop(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("fivecast.m6.collect_once", stop)
    assert main(["collect", "--experiment", "1", "--once", "--db-path", str(tmp_path / "db")]) == 0
    assert "resumable" in capsys.readouterr().out


def test_robustness_reports_absolute_and_exclusion_diagnostics_when_pnl_negative():
    rows = [
        {"timestamp_utc": "2026-09-07T00:00:00+00:00", "paper_pnl": "2", "eligible": 1},
        {"timestamp_utc": "2026-09-07T06:00:00+00:00", "paper_pnl": "-5", "eligible": 1},
        {"timestamp_utc": "2026-09-08T12:00:00+00:00", "paper_pnl": "-1", "eligible": 1},
    ]
    metrics = _robustness(rows, rows)
    assert metrics["pnl_excluding_best_trade"] == -6
    assert metrics["pnl_excluding_top_five_trades"] == 0
    assert metrics["best_day_pnl_share"] is None
    assert metrics["best_day_pnl"] == -1
    assert metrics["pnl_by_calendar_day"] == {"2026-09-07": -3.0, "2026-09-08": -1.0}


def test_time_blocks_are_fixed_and_mark_low_sample_sizes():
    row = {
        "timestamp_utc": "2026-09-07T07:00:00+00:00",
        "market_id": "m",
        "outcome": "UP",
        "eligible": 0,
        "paper_pnl": None,
        "predicted_up_probability": "0.6",
        "up_ask": "0.5",
    }
    blocks = _time_blocks([row])
    assert tuple(blocks) == ("00:00-05:59", "06:00-11:59", "12:00-17:59", "18:00-23:59")
    assert blocks["06:00-11:59"]["resolved_markets"] == 1
    assert blocks["06:00-11:59"]["low_n"] is True
