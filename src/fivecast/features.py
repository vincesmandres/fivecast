"""Historical-only, deterministic features for replay."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from fivecast.models import MarketSnapshot


@dataclass(frozen=True, slots=True)
class Features:
    btc_velocity_15s: Decimal | None
    btc_velocity_30s: Decimal | None
    btc_volatility_30s: Decimal | None
    btc_volatility_60s: Decimal | None

    @property
    def velocity_15s(self) -> Decimal | None:
        return self.btc_velocity_15s

    @property
    def velocity_30s(self) -> Decimal | None:
        return self.btc_velocity_30s

    @property
    def volatility_30s(self) -> Decimal | None:
        return self.btc_volatility_30s

    @property
    def volatility_60s(self) -> Decimal | None:
        return self.btc_volatility_60s


def _prior_price(history: Sequence[MarketSnapshot], timestamp, seconds: int) -> Decimal | None:
    cutoff = timestamp - timedelta(seconds=seconds)
    candidates = [item for item in history if item.timestamp_utc <= cutoff]
    return None if not candidates else candidates[-1].btc_price


def _volatility(history: Sequence[MarketSnapshot], timestamp, seconds: int) -> Decimal | None:
    cutoff = timestamp - timedelta(seconds=seconds)
    values = [item.btc_delta_pct for item in history if cutoff <= item.timestamp_utc <= timestamp]
    if not values:
        return None
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values))
    return variance.sqrt() if variance else Decimal(0)


def calculate_features(history: Sequence[MarketSnapshot]) -> Features:
    """Calculate features for the last observation using no future observations."""
    if not history:
        raise ValueError("At least one snapshot is required")
    ordered = tuple(history)
    current = ordered[-1]

    def velocity(seconds: int) -> Decimal | None:
        prior = _prior_price(ordered, current.timestamp_utc, seconds)
        return None if prior is None else current.btc_price - prior

    return Features(
        btc_velocity_15s=velocity(15),
        btc_velocity_30s=velocity(30),
        btc_volatility_30s=_volatility(ordered, current.timestamp_utc, 30),
        btc_volatility_60s=_volatility(ordered, current.timestamp_utc, 60),
    )
