"""Gamma discovery and CLOB public GET books only; no trading SDK dependency."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, Json, StrictBool, StrictStr, field_validator

from fivecast.models import (
    MarketResolution,
    OutcomeQuote,
    PositiveDecimal,
    PredictionMarket,
    Probability,
    TokenID,
    UTCDateTime,
    utc_now,
)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
logger = logging.getLogger(__name__)


class NoActiveMarket(RuntimeError):
    """No book-enabled BTC five-minute market exists for the current UTC window."""


class MarketPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Annotated[str, Field(min_length=1)]
    conditionId: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    question: Annotated[str, Field(min_length=1)]
    slug: str
    eventStartTime: UTCDateTime
    endDate: UTCDateTime
    outcomes: Json[list[str]]
    clobTokenIds: Json[list[TokenID]]
    active: StrictBool
    closed: StrictBool
    archived: StrictBool
    enableOrderBook: StrictBool
    acceptingOrders: StrictBool
    umaResolutionStatus: StrictStr | None = None
    outcomePrices: Json[list[Probability]] | None = None
    closedTime: UTCDateTime | None = None

    @field_validator("closedTime", mode="before")
    @classmethod
    def parse_closed_time(cls, value):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value


class ResolutionMarketPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    condition_id: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    market_slug: Annotated[str, Field(pattern=r"^btc-updown-5m-[0-9]+$")]
    closed: StrictBool
    tokens: list["ResolutionToken"]


class ResolutionToken(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token_id: TokenID
    outcome: StrictStr
    winner: StrictBool


class BookLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    price: Probability
    size: PositiveDecimal


class BookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    market: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    asset_id: TokenID
    timestamp: Annotated[str, Field(pattern=r"^[0-9]{13}$")]
    bids: list[BookLevel]
    asks: list[BookLevel]


class PolymarketFeed:
    def __init__(self, client: httpx.Client):
        self.client = client

    def discover_market(self, now: datetime) -> PredictionMarket:
        if now.utcoffset() is None:
            raise ValueError("Discovery time must be timezone-aware")
        epoch = int(now.timestamp()) // 300 * 300
        slug = f"btc-updown-5m-{epoch}"
        response = self.client.get(f"{GAMMA_URL}/markets/slug/{slug}")
        if response.status_code == 404:
            raise NoActiveMarket(f"Gamma has no market for {slug}")
        response.raise_for_status()
        payload = MarketPayload.model_validate_json(response.content)
        market = self._market_from_gamma(payload)
        if market.slug != slug:
            raise ValueError("Gamma returned a different market than the requested slug")
        if (
            not payload.active
            or payload.closed
            or payload.archived
            or not payload.enableOrderBook
            or not payload.acceptingOrders
            or not market.start_time_utc <= now < market.end_time_utc
        ):
            raise NoActiveMarket(f"Market {slug} is not active and book-enabled")
        return market

    def get_market_by_slug(self, slug: str) -> PredictionMarket:
        response = self.client.get(f"{GAMMA_URL}/markets/slug/{slug}")
        response.raise_for_status()
        market = self._market_from_gamma(MarketPayload.model_validate_json(response.content))
        if market.slug != slug:
            raise ValueError("Gamma returned a different market than the requested slug")
        return market

    @staticmethod
    def _market_from_gamma(payload: MarketPayload) -> PredictionMarket:
        labels = [value.strip().upper() for value in payload.outcomes]
        if len(labels) != 2 or set(labels) != {"UP", "DOWN"} or len(payload.clobTokenIds) != 2:
            raise ValueError("Expected exactly one UP and one DOWN outcome/token mapping")
        tokens = dict(zip(labels, payload.clobTokenIds, strict=True))
        return PredictionMarket(
            market_id=payload.id,
            condition_id=payload.conditionId,
            question=payload.question,
            slug=payload.slug,
            start_time_utc=payload.eventStartTime,
            end_time_utc=payload.endDate,
            up_token_id=tokens["UP"],
            down_token_id=tokens["DOWN"],
            status="closed" if payload.closed else "active",
        )

    def get_resolution(self, market: PredictionMarket) -> MarketResolution | None:
        gamma_response = self.client.get(f"{GAMMA_URL}/markets/slug/{market.slug}")
        gamma_response.raise_for_status()
        gamma_payload = MarketPayload.model_validate_json(gamma_response.content)
        gamma = self._market_from_gamma(gamma_payload)
        if (
            gamma.market_id != market.market_id
            or gamma.slug != market.slug
            or gamma.condition_id.lower() != market.condition_id.lower()
            or gamma.start_time_utc != market.start_time_utc
            or gamma.end_time_utc != market.end_time_utc
            or gamma.up_token_id != market.up_token_id
            or gamma.down_token_id != market.down_token_id
        ):
            raise ValueError("Gamma market identity does not match supplied market")
        if gamma.status != "closed":
            return None

        if (
            gamma_payload.umaResolutionStatus is None
            or gamma_payload.umaResolutionStatus != "resolved"
        ):
            return None
        if gamma_payload.outcomePrices is None:
            raise ValueError("Final Gamma market is missing outcome prices")
        if gamma_payload.closedTime is not None and gamma_payload.closedTime < market.end_time_utc:
            raise ValueError("Gamma official close precedes market end")
        prices = gamma_payload.outcomePrices
        labels = [value.strip().upper() for value in gamma_payload.outcomes]
        if len(prices) != 2 or len(labels) != 2 or set(labels) != {"UP", "DOWN"}:
            raise ValueError("Final Gamma outcome prices are not a UP/DOWN pair")
        price_by_outcome = dict(zip(labels, prices, strict=True))
        if set(price_by_outcome.values()) != {Decimal("0"), Decimal("1")}:
            raise ValueError("Final Gamma outcome prices must be exactly 1/0")

        response = self.client.get(f"{CLOB_URL}/markets/{market.condition_id}")
        response.raise_for_status()
        clob = ResolutionMarketPayload.model_validate_json(response.content)
        if (
            clob.market_slug != market.slug
            or clob.condition_id.lower() != market.condition_id.lower()
        ):
            raise ValueError("CLOB market identity does not match supplied market")
        expected_tokens = {market.up_token_id, market.down_token_id}
        if len(clob.tokens) != 2 or {token.token_id for token in clob.tokens} != expected_tokens:
            raise ValueError("CLOB resolution tokens do not match supplied market")
        token_by_outcome = {token.outcome.strip().upper(): token.token_id for token in clob.tokens}
        if set(token_by_outcome) != {"UP", "DOWN"} or token_by_outcome != {
            "UP": market.up_token_id,
            "DOWN": market.down_token_id,
        }:
            raise ValueError("CLOB resolution outcome mapping does not match supplied market")
        winners = [token for token in clob.tokens if token.winner]
        if len(winners) > 1:
            raise ValueError("CLOB resolution has multiple winners")
        if not clob.closed or not winners:
            logger.warning("Final Gamma market is awaiting CLOB resolution publication")
            return None
        winner = winners[0].outcome.strip().upper()
        if winner not in ("UP", "DOWN") or price_by_outcome[winner] != Decimal("1"):
            raise ValueError("CLOB winner does not match final Gamma prices")
        if price_by_outcome["UP" if winner == "DOWN" else "DOWN"] != Decimal("0"):
            raise ValueError("Final Gamma prices do not match CLOB winner")
        return MarketResolution(
            market_id=market.market_id,
            outcome=winner,
            resolution_time_utc=gamma_payload.closedTime,
            resolution_time_source=(
                "gamma_closedTime" if gamma_payload.closedTime is not None else None
            ),
            observed_at_utc=utc_now(),
        )

    def get_quote(self, market: PredictionMarket, outcome: Literal["UP", "DOWN"]) -> OutcomeQuote:
        if outcome not in ("UP", "DOWN"):
            raise ValueError("Expected UP or DOWN")
        token = market.up_token_id if outcome == "UP" else market.down_token_id
        response = self.client.get(f"{CLOB_URL}/book", params={"token_id": token})
        response.raise_for_status()
        payload = BookPayload.model_validate_json(response.content)
        if payload.asset_id != token or payload.market.lower() != market.condition_id.lower():
            raise ValueError("CLOB book token or condition does not match discovered market")
        # Do not assume book sorting; validate every level, then take the extrema.
        return OutcomeQuote(
            outcome=outcome,
            token_id=token,
            best_bid=max((level.price for level in payload.bids), default=None),
            best_ask=min((level.price for level in payload.asks), default=None),
            timestamp_utc=datetime(1970, 1, 1, tzinfo=UTC)
            + timedelta(milliseconds=int(payload.timestamp)),
        )
