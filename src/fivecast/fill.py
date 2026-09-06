"""Offline paper fills and settlement accounting."""

from dataclasses import dataclass
from decimal import Decimal

from fivecast.models import MarketSnapshot
from fivecast.strategy import Decision


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    market_id: str
    decision: Decision
    entry_price: Decimal
    slippage: Decimal
    fees: Decimal
    gross_payout: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    roi: Decimal

    @property
    def fee(self) -> Decimal:
        return self.fees

    @property
    def net(self) -> Decimal:
        return self.net_pnl


class PaperFill:
    def __init__(
        self, slippage_bps: Decimal = Decimal("0"), fee_bps: Decimal = Decimal("0")
    ) -> None:
        if slippage_bps < 0 or fee_bps < 0:
            raise ValueError("Invalid slippage or fee rate")
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps

    def fill(
        self, snapshot: MarketSnapshot, decision: Decision, outcome: str
    ) -> ShadowTrade | None:
        if decision is Decision.NO_SIGNAL:
            return None
        if outcome not in {"UP", "DOWN"}:
            raise ValueError("Outcome must be UP or DOWN")
        ask = snapshot.up_ask if decision is Decision.BUY_UP else snapshot.down_ask
        if ask is None:
            return None
        entry = ask * (Decimal(1) + self.slippage_bps / Decimal(10000))
        if entry > 1:
            raise ValueError("Slippage-adjusted buy price exceeds binary payout")
        slippage = entry - ask
        fees = entry * self.fee_bps / Decimal(10000)
        payout = Decimal(1) if (decision is Decision.BUY_UP) == (outcome == "UP") else Decimal(0)
        gross_pnl = payout - entry
        net_pnl = gross_pnl - fees
        roi = net_pnl / (entry + fees)
        return ShadowTrade(
            snapshot.market_id, decision, entry, slippage, fees, payout, gross_pnl, net_pnl, roi
        )
