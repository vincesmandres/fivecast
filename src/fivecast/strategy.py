"""Pure replay strategies; settlement is deliberately absent from their context."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from fivecast.features import Features
from fivecast.models import MarketSnapshot, SnapshotQuality


class Decision(StrEnum):
    NO_SIGNAL = "NO_SIGNAL"
    BUY_UP = "BUY_UP"
    BUY_DOWN = "BUY_DOWN"


@dataclass(frozen=True, slots=True)
class StrategyContext:
    current: MarketSnapshot
    history: tuple[MarketSnapshot, ...]
    quality: SnapshotQuality
    features: Features


@dataclass(frozen=True, slots=True)
class LateMomentumParams:
    delta_threshold_usd: Decimal = Decimal("80")
    max_seconds_remaining: float = 60.0
    max_entry_price: Decimal = Decimal("0.85")
    max_spread: Decimal = Decimal("0.05")
    max_skew_ms: float = 5000.0

    def __post_init__(self) -> None:
        if self.delta_threshold_usd < 0 or not 0 <= self.max_spread <= 1:
            raise ValueError("Delta threshold must be nonnegative and spread must be in [0, 1]")
        if not 0 < self.max_seconds_remaining <= 300 or not 0 <= self.max_entry_price <= 1:
            raise ValueError("Invalid entry time or price limit")
        if self.max_skew_ms < 0:
            raise ValueError("Maximum source skew must be nonnegative")


class Strategy(Protocol):
    def decide(
        self, context: StrategyContext, params: LateMomentumParams | None = None
    ) -> Decision: ...


class LateMomentumStrategy:
    def __init__(self, params: LateMomentumParams | None = None) -> None:
        self.params = params or LateMomentumParams()

    def decide(
        self, context: StrategyContext, params: LateMomentumParams | None = None
    ) -> Decision:
        chosen = params or self.params
        current = context.current
        if (
            context.quality.is_stale is not False
            or current.seconds_remaining > chosen.max_seconds_remaining
            or context.quality.source_skew_ms > chosen.max_skew_ms
        ):
            return Decision.NO_SIGNAL
        if current.btc_delta_usd >= chosen.delta_threshold_usd:
            if (
                current.up_ask is not None
                and current.up_spread is not None
                and current.up_ask <= chosen.max_entry_price
                and current.up_spread <= chosen.max_spread
            ):
                return Decision.BUY_UP
        if current.btc_delta_usd <= -chosen.delta_threshold_usd:
            if (
                current.down_ask is not None
                and current.down_spread is not None
                and current.down_ask <= chosen.max_entry_price
                and current.down_spread <= chosen.max_spread
            ):
                return Decision.BUY_DOWN
        return Decision.NO_SIGNAL
