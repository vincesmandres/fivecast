"""Deterministic, research-only logistic model and dataset construction."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev

from fivecast.replay import MarketReplay, ReplayQuality, ReplaySnapshot, ReplayStore

FEATURE_NAMES = (
    "btc_delta_usd",
    "btc_delta_pct",
    "btc_velocity_5s",
    "btc_velocity_15s",
    "btc_velocity_30s",
    "btc_volatility_30s",
    "btc_volatility_60s",
    "seconds_remaining",
    "up_bid",
    "up_ask",
    "down_bid",
    "down_ask",
    "up_spread",
    "down_spread",
    "source_skew_ms",
    "is_stale",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """A point-in-time feature vector. It intentionally has no outcome field."""

    market_id: str
    timestamp_utc: datetime
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.timestamp_utc.tzinfo is None:
            raise ValueError("Feature timestamp must be timezone-aware")
        values = tuple(float(value) for value in self.values)
        if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(v) for v in values):
            raise ValueError("Feature vector must contain finite values for every feature")
        object.__setattr__(self, "values", values)

    def as_dict(self) -> dict[str, float]:
        return dict(zip(FEATURE_NAMES, self.values, strict=True))

    def as_vector(self) -> tuple[float, ...]:
        return self.values


@dataclass(frozen=True, slots=True)
class DatasetFreeze:
    cutoff_utc: datetime
    eligible_market_ids: tuple[str, ...]
    rejected_market_ids: tuple[str, ...]
    rejection_reasons: tuple[tuple[str, str], ...]
    replay_quality: ReplayQuality
    feature_names: tuple[str, ...]
    provenance: str
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.cutoff_utc.tzinfo is None or self.cutoff_utc.utcoffset() is None:
            raise ValueError("Dataset cutoff must be timezone-aware UTC")
        cutoff = self.cutoff_utc.astimezone(UTC)
        object.__setattr__(self, "cutoff_utc", cutoff)
        body = {
            "cutoff_utc": cutoff.isoformat(),
            "eligible_market_ids": self.eligible_market_ids,
            "rejected_market_ids": self.rejected_market_ids,
            "rejection_reasons": self.rejection_reasons,
            "replay_quality": self.replay_quality,
            "feature_names": self.feature_names,
            "provenance": self.provenance,
        }
        object.__setattr__(self, "fingerprint", _fingerprint(body))


@dataclass(frozen=True, slots=True)
class Dataset:
    freeze: DatasetFreeze
    train: tuple[FeatureRow, ...]
    validation: tuple[FeatureRow, ...]
    train_labels: tuple[int, ...]
    validation_labels: tuple[int, ...]
    row_rejections: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if len(self.train) != len(self.train_labels) or len(self.validation) != len(
            self.validation_labels
        ):
            raise ValueError("Each dataset row must have one official label")


def _velocity(prefix: Sequence[ReplaySnapshot], seconds: int) -> Decimal | None:
    current = prefix[-1].snapshot
    cutoff = current.timestamp_utc.timestamp() - seconds
    prior = [item.snapshot for item in prefix if item.snapshot.timestamp_utc.timestamp() <= cutoff]
    return None if not prior else current.btc_price - prior[-1].btc_price


def _volatility(prefix: Sequence[ReplaySnapshot], seconds: int) -> Decimal | None:
    current = prefix[-1].snapshot
    cutoff = current.timestamp_utc.timestamp() - seconds
    values = [
        item.snapshot.btc_delta_pct
        for item in prefix
        if cutoff <= item.snapshot.timestamp_utc.timestamp() <= current.timestamp_utc.timestamp()
    ]
    return None if not values else Decimal(str(pstdev(float(v) for v in values)))


def _row(prefix: Sequence[ReplaySnapshot]) -> tuple[FeatureRow | None, str | None]:
    current = prefix[-1]
    snapshot = current.snapshot
    quality = current.quality
    values: tuple[object, ...] = (
        snapshot.btc_delta_usd,
        snapshot.btc_delta_pct,
        _velocity(prefix, 5),
        _velocity(prefix, 15),
        _velocity(prefix, 30),
        _volatility(prefix, 30),
        _volatility(prefix, 60),
        snapshot.seconds_remaining,
        snapshot.up_bid,
        snapshot.up_ask,
        snapshot.down_bid,
        snapshot.down_ask,
        snapshot.up_spread,
        snapshot.down_spread,
        quality.source_skew_ms,
        None if quality.is_stale is None else int(quality.is_stale),
    )
    missing = tuple(
        name for name, value in zip(FEATURE_NAMES, values, strict=True) if value is None
    )
    if missing:
        return None, "missing:" + ",".join(missing)
    return FeatureRow(
        snapshot.market_id, snapshot.timestamp_utc, tuple(float(v) for v in values)
    ), None


def build_dataset(
    path: str | Path,
    replay_quality: ReplayQuality | None = None,
    *,
    provenance: str = "ReplayStore.load_eligible; M3 chronological research split",
) -> Dataset:
    """Load eligible replays and create only the first 70% research dataset."""
    quality = replay_quality or ReplayQuality()
    selection = ReplayStore(path).load_eligible(quality)
    ordered = tuple(
        sorted(selection.replays, key=lambda r: r.snapshots[0].snapshot.market_start_utc)
    )
    research = ordered[: int(len(ordered) * 0.7)]
    if not research:
        raise ValueError("Research subset is empty")
    cutoff = max(item.snapshot.timestamp_utc for replay in research for item in replay.snapshots)
    rejected = dict(selection.rejected)
    train_replays = research[: int(len(research) * 0.7)]
    validation_replays = research[int(len(research) * 0.7) :]
    train: list[FeatureRow] = []
    validation: list[FeatureRow] = []
    train_labels: list[int] = []
    validation_labels: list[int] = []
    row_rejections: list[tuple[str, str, str]] = []

    def consume(replays: Iterable[MarketReplay], rows: list[FeatureRow], labels: list[int]) -> None:
        for replay in replays:
            selected: FeatureRow | None = None
            for index in range(len(replay.snapshots)):
                feature, reason = _row(replay.snapshots[: index + 1])
                item = replay.snapshots[index]
                if feature is None:
                    row_rejections.append(
                        (
                            replay.market_id,
                            item.snapshot.timestamp_utc.isoformat(),
                            reason or "invalid",
                        )
                    )
                    continue
                if item.snapshot.seconds_remaining <= 60:
                    selected = feature
                    break
            if selected is not None:
                # One predeclared point-in-time anchor per market avoids treating
                # every five-second row as an independent outcome observation.
                rows.append(selected)
                labels.append(1 if replay.settle() == "UP" else 0)

    consume(train_replays, train, train_labels)
    consume(validation_replays, validation, validation_labels)
    for replay in research:
        if not any(row.market_id == replay.market_id for row in (*train, *validation)):
            rejected.setdefault(replay.market_id, "no_complete_feature_rows")
    eligible = tuple(sorted({row.market_id for row in (*train, *validation)}))
    freeze = DatasetFreeze(
        cutoff,
        eligible,
        tuple(sorted(rejected)),
        tuple(sorted(rejected.items())),
        quality,
        FEATURE_NAMES,
        provenance,
    )
    return Dataset(
        freeze,
        tuple(train),
        tuple(validation),
        tuple(train_labels),
        tuple(validation_labels),
        tuple(sorted(row_rejections)),
    )


@dataclass(frozen=True, slots=True)
class LogisticConfig:
    learning_rate: float = 0.1
    iterations: int = 1000
    l2: float = 0.01


@dataclass(frozen=True, slots=True)
class Scaler:
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def transform(self, row: FeatureRow) -> tuple[float, ...]:
        return tuple(
            (float(value) - mean) / scale
            for value, mean, scale in zip(row.values, self.means, self.scales, strict=True)
        )


@dataclass(frozen=True, slots=True)
class FrozenLogistic:
    intercept: float
    coefficients: tuple[float, ...]
    scaler: Scaler
    feature_names: tuple[str, ...]
    config: LogisticConfig
    config_fingerprint: str
    freeze_fingerprint: str
    trained_cutoff_utc: datetime

    def predict_proba(self, row: FeatureRow) -> float:
        if row.as_dict().keys() != dict.fromkeys(self.feature_names).keys():
            raise ValueError("Feature names do not match model artifact")
        z = self.intercept + sum(
            c * x for c, x in zip(self.coefficients, self.scaler.transform(row), strict=True)
        )
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        exp_z = math.exp(z)
        return exp_z / (1.0 + exp_z)


def train(dataset: Dataset, config: LogisticConfig | None = None) -> FrozenLogistic:
    config = config or LogisticConfig()
    if config.learning_rate <= 0 or config.iterations < 1 or config.l2 < 0:
        raise ValueError("Invalid logistic configuration")
    if not dataset.train:
        raise ValueError("Cannot train without training rows")
    width = len(FEATURE_NAMES)
    means = tuple(mean(float(row.values[i]) for row in dataset.train) for i in range(width))
    scales = tuple(
        max(
            math.sqrt(
                sum((float(row.values[i]) - means[i]) ** 2 for row in dataset.train)
                / len(dataset.train)
            ),
            1e-12,
        )
        for i in range(width)
    )
    scaler = Scaler(means, scales)
    x = tuple(scaler.transform(row) for row in dataset.train)
    weights = [0.0] * width
    intercept = 0.0
    for _ in range(config.iterations):
        errors = [
            (
                _sigmoid(
                    intercept + sum(w * value for w, value in zip(weights, values, strict=True))
                )
                - label
            )
            for values, label in zip(x, dataset.train_labels, strict=True)
        ]
        intercept -= config.learning_rate * sum(errors) / len(errors)
        for i in range(width):
            gradient = (
                sum(error * values[i] for error, values in zip(errors, x, strict=True))
                / len(errors)
                + config.l2 * weights[i]
            )
            weights[i] -= config.learning_rate * gradient
    return FrozenLogistic(
        intercept,
        tuple(weights),
        scaler,
        FEATURE_NAMES,
        config,
        _fingerprint(config),
        dataset.freeze.fingerprint,
        dataset.freeze.cutoff_utc,
    )


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


@dataclass(frozen=True, slots=True)
class Evaluation:
    brier: float
    logloss: float
    roc_auc: float | None
    accuracy: float
    calibration: tuple[tuple[float, int, float, float], ...]


def evaluate(
    model: FrozenLogistic, rows: Sequence[FeatureRow], labels: Sequence[int], bins: int = 10
) -> Evaluation:
    if len(rows) != len(labels) or not rows:
        raise ValueError("Evaluation requires equally sized non-empty rows and labels")
    probabilities = tuple(model.predict_proba(row) for row in rows)
    eps = 1e-15
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)) / len(labels)
    logloss = -sum(
        y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1 - p))
        for p, y in zip(probabilities, labels, strict=True)
    ) / len(labels)
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        auc = None
    else:
        order = sorted(range(len(labels)), key=lambda i: (probabilities[i], -i))
        auc = (
            sum(rank for rank, index in enumerate(order, 1) if labels[index])
            - positives * (positives + 1) / 2
        )
        auc /= positives * negatives
    calibration = []
    for bucket in range(bins):
        members = [
            i
            for i, p in enumerate(probabilities)
            if (bucket / bins <= p < (bucket + 1) / bins) or (bucket == bins - 1 and p == 1)
        ]
        if members:
            calibration.append(
                (
                    (bucket + 0.5) / bins,
                    len(members),
                    sum(probabilities[i] for i in members) / len(members),
                    sum(labels[i] for i in members) / len(members),
                )
            )
    return Evaluation(
        brier,
        logloss,
        auc,
        sum((p >= 0.5) == bool(y) for p, y in zip(probabilities, labels, strict=True))
        / len(labels),
        tuple(calibration),
    )
