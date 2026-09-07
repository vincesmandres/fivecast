from datetime import UTC, datetime
from decimal import Decimal

from fivecast.fair_value import FrictionConfig, baseline_direction, fair_value, market_baseline
from fivecast.model import (
    FEATURE_NAMES,
    Dataset,
    DatasetFreeze,
    FeatureRow,
    LogisticConfig,
    ReplayQuality,
    evaluate,
    train,
)


def row(market: str, value: float, up: float = 0.4, down: float = 0.6) -> FeatureRow:
    up_value = Decimal(str(up))
    down_value = Decimal(str(down))
    values = (
        value,
        value / 100,
        value,
        value,
        value,
        0.1,
        0.2,
        30,
        up_value - Decimal(".01"),
        up_value,
        down_value - Decimal(".01"),
        down_value,
        0.01,
        0.01,
        1,
        0,
    )
    return FeatureRow(market, datetime(2026, 1, 1, tzinfo=UTC), values)


def dataset() -> Dataset:
    rows = tuple(row(f"m{i}", float(i), 0.4, 0.6) for i in range(4))
    freeze = DatasetFreeze(
        datetime(2026, 1, 1, tzinfo=UTC),
        tuple(f"m{i}" for i in range(4)),
        (),
        (),
        ReplayQuality(),
        FEATURE_NAMES,
        "unit-test",
    )
    return Dataset(freeze, rows[:3], rows[3:], (0, 0, 1), (1,))


def test_feature_row_has_no_label_and_model_scaler_is_train_only():
    data = dataset()
    model = train(data, LogisticConfig(iterations=20))
    assert not hasattr(data.train[0], "label")
    assert model.scaler.means[0] == sum(row.values[0] for row in data.train) / 3
    assert model.scaler.means[0] != data.validation[0].values[0]


def test_model_training_and_freeze_fingerprint_are_deterministic():
    first = train(dataset(), LogisticConfig(iterations=30))
    second = train(dataset(), LogisticConfig(iterations=30))
    assert first == second
    assert len(first.freeze_fingerprint) == 64
    assert dataset().freeze.fingerprint == dataset().freeze.fingerprint


def test_market_baseline_and_fair_value_use_side_specific_asks():
    item = row("m", 1, up=Decimal("0.30"), down=Decimal("0.70"))
    assert market_baseline(item, "UP") == Decimal("0.3")
    assert market_baseline(item, "DOWN") == Decimal("0.7")
    assert baseline_direction(item) == "UP"
    result = fair_value(
        Decimal("0.8"), item, "DOWN", FrictionConfig(Decimal(".01"), Decimal(".02"), Decimal(".03"))
    )
    assert result.probability == Decimal("0.2")
    assert result.raw_edge == Decimal("-0.5")
    assert result.estimated_net_edge == Decimal("-0.56")


def test_evaluation_reports_metrics_and_deterministic_calibration():
    data = dataset()
    model = train(data, LogisticConfig(iterations=30))
    result = evaluate(model, data.validation, data.validation_labels, bins=2)
    assert 0 <= result.brier <= 1
    assert result.logloss >= 0
    assert 0 <= result.accuracy <= 1
    assert result.calibration
