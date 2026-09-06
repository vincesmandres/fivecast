"""Immutable normalized models; all timestamps are aware and normalized to UTC."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

UTCDateTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Probability = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
TokenID = Annotated[str, Field(pattern=r"^[0-9]+$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BTCQuote(Model):
    timestamp_utc: UTCDateTime
    symbol: Literal["BTC-USD"] = "BTC-USD"
    price: PositiveDecimal
    source: Literal["coinbase_exchange"] = "coinbase_exchange"


class WindowOpen(Model):
    window_start_utc: UTCDateTime
    observed_at_utc: UTCDateTime
    price: PositiveDecimal
    source: Literal["coinbase_exchange"] = "coinbase_exchange"
    method: Literal["coinbase_1m_candle_open"] = "coinbase_1m_candle_open"

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        if self.observed_at_utc < self.window_start_utc:
            raise ValueError("Window open cannot be observed before the window starts")
        return self


class PredictionMarket(Model):
    market_id: Identifier
    condition_id: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    question: Annotated[str, Field(min_length=1)]
    slug: Annotated[str, Field(pattern=r"^btc-updown-5m-[0-9]+$")]
    start_time_utc: UTCDateTime
    end_time_utc: UTCDateTime
    up_token_id: TokenID
    down_token_id: TokenID
    status: Literal["active", "closed"] = "active"

    @model_validator(mode="after")
    def validate_market(self) -> Self:
        if (self.end_time_utc - self.start_time_utc).total_seconds() != 300:
            raise ValueError("Expected an exact five-minute market window")
        epoch = int(self.slug.rsplit("-", 1)[1])
        if epoch % 300 or self.start_time_utc.timestamp() != epoch:
            raise ValueError("Market window does not match the aligned slug timestamp")
        if self.up_token_id == self.down_token_id:
            raise ValueError("UP and DOWN must have distinct token IDs")
        return self


class OutcomeQuote(Model):
    outcome: Literal["UP", "DOWN"]
    token_id: TokenID
    best_bid: Probability | None
    best_ask: Probability | None
    timestamp_utc: UTCDateTime
    source: Literal["polymarket_clob"] = "polymarket_clob"

    @model_validator(mode="after")
    def validate_spread(self) -> Self:
        if (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > self.best_ask
        ):
            raise ValueError("Best bid exceeds best ask")
        return self


def spread(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    return None if bid is None or ask is None else ask - bid


class MarketSnapshot(Model):
    timestamp_utc: UTCDateTime
    market_id: Identifier
    market_start_utc: UTCDateTime
    market_end_utc: UTCDateTime
    seconds_remaining: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)]
    btc_price: PositiveDecimal
    btc_window_open_price: PositiveDecimal
    btc_delta_usd: FiniteDecimal
    btc_delta_pct: FiniteDecimal
    up_bid: Probability | None
    up_ask: Probability | None
    down_bid: Probability | None
    down_ask: Probability | None
    up_spread: Probability | None
    down_spread: Probability | None
    source_btc: Literal["coinbase_exchange"]
    source_prediction_market: Literal["polymarket_clob"]
    btc_timestamp_utc: UTCDateTime
    up_timestamp_utc: UTCDateTime
    down_timestamp_utc: UTCDateTime
    up_token_id: TokenID
    down_token_id: TokenID
    btc_open_observed_at_utc: UTCDateTime
    btc_open_method: Literal["coinbase_1m_candle_open"]

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        if (self.market_end_utc - self.market_start_utc).total_seconds() != 300:
            raise ValueError("Snapshot window must be five minutes")
        if not self.market_start_utc <= self.timestamp_utc < self.market_end_utc:
            raise ValueError("Snapshot must fall within its active market window")
        if self.seconds_remaining != (self.market_end_utc - self.timestamp_utc).total_seconds():
            raise ValueError("Inconsistent seconds_remaining")
        delta = self.btc_price - self.btc_window_open_price
        if self.btc_delta_usd != delta or self.btc_delta_pct != delta / self.btc_window_open_price:
            raise ValueError("Inconsistent BTC deltas")
        for bid, ask, value in (
            (self.up_bid, self.up_ask, self.up_spread),
            (self.down_bid, self.down_ask, self.down_spread),
        ):
            if bid is not None and ask is not None and bid > ask:
                raise ValueError("Snapshot bid exceeds ask")
            if value != spread(bid, ask):
                raise ValueError("Inconsistent spread")
        for timestamp in (
            self.btc_timestamp_utc,
            self.up_timestamp_utc,
            self.down_timestamp_utc,
            self.btc_open_observed_at_utc,
        ):
            if not self.market_start_utc <= timestamp <= self.timestamp_utc:
                raise ValueError("Input timestamp lies outside the observation window")
        if self.up_token_id == self.down_token_id:
            raise ValueError("Snapshot token IDs must differ")
        return self


class SnapshotQuality(Model):
    btc_source_timestamp: UTCDateTime
    market_source_timestamp: UTCDateTime
    source_skew_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    poll_latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    is_stale: bool | None = None


class MarketResolution(Model):
    market_id: Identifier
    outcome: Literal["UP", "DOWN"]
    resolution_time_utc: UTCDateTime | None = None
    resolution_time_source: Literal["gamma_closedTime"] | None = None
    observed_at_utc: UTCDateTime
    source: Literal["polymarket_clob_winner+gamma_final"] = "polymarket_clob_winner+gamma_final"

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        if (self.resolution_time_utc is None) != (self.resolution_time_source is None):
            raise ValueError("Resolution time must include its provenance, or both must be absent")
        if self.resolution_time_utc is not None and self.resolution_time_utc > self.observed_at_utc:
            raise ValueError("Resolution time cannot be after the confirming observation")
        return self


def utc_now() -> datetime:
    return datetime.now(UTC)
