"""Offline fair-value and executable market-baseline calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from fivecast.model import FeatureRow

Side = Literal["UP", "DOWN"]


@dataclass(frozen=True, slots=True)
class FrictionConfig:
    fee: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    latency: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if min(self.fee, self.slippage, self.latency) < 0:
            raise ValueError("Friction estimates cannot be negative")

    @property
    def total(self) -> Decimal:
        return self.fee + self.slippage + self.latency


@dataclass(frozen=True, slots=True)
class FairValue:
    side: Side
    probability: Decimal
    ask: Decimal
    raw_edge: Decimal
    estimated_net_edge: Decimal
    friction: FrictionConfig


def executable_ask(row: FeatureRow, side: Side) -> Decimal:
    values = row.as_dict()
    key = "up_ask" if side == "UP" else "down_ask"
    return Decimal(str(values[key]))


def market_baseline(row: FeatureRow, side: Side) -> Decimal:
    """Model0: the executable probability for a side is its own best ask."""
    return executable_ask(row, side)


def baseline_direction(row: FeatureRow) -> Side:
    """Choose the executable side with the lower ask."""
    up = executable_ask(row, "UP")
    down = executable_ask(row, "DOWN")
    return "UP" if up <= down else "DOWN"


def fair_value(
    model_probability: float | Decimal,
    row: FeatureRow,
    side: Side,
    friction: FrictionConfig | None = None,
) -> FairValue:
    """Calculate model-minus-ask edge, then subtract explicit estimated costs."""
    costs = friction or FrictionConfig()
    probability = Decimal(str(model_probability))
    if not 0 <= probability <= 1:
        raise ValueError("Model probability must be between zero and one")
    probability = probability if side == "UP" else Decimal(1) - probability
    ask = executable_ask(row, side)
    raw = probability - ask
    return FairValue(side, probability, ask, raw, raw - costs.total, costs)


def estimated_net_edge(
    model_probability: float | Decimal,
    row: FeatureRow,
    side: Side,
    friction: FrictionConfig | None = None,
) -> Decimal:
    return fair_value(model_probability, row, side, friction).estimated_net_edge
