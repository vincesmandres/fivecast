"""Coinbase public BTC-USD HTTP data, avoiding stream/reconnect state at 5s cadence."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from fivecast.models import BTCQuote, PositiveDecimal, UTCDateTime, WindowOpen, utc_now

BASE_URL = "https://api.exchange.coinbase.com"


class OpenPriceUnavailable(RuntimeError):
    """The exact starting-minute candle is not published yet; never substitute another."""


class TickerPayload(BaseModel):
    # Public APIs may add fields. Validate consumed fields, ignore unrelated metadata.
    model_config = ConfigDict(extra="ignore")
    price: PositiveDecimal
    time: UTCDateTime


class Candle(RootModel):
    root: tuple[
        Annotated[int, Field(ge=0, strict=True)],
        PositiveDecimal,
        PositiveDecimal,
        PositiveDecimal,
        PositiveDecimal,
        Annotated[Decimal, Field(ge=0, allow_inf_nan=False)],
    ]

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        timestamp, low, high, opening, close, _volume = self.root
        if timestamp % 60 or not low <= opening <= high or not low <= close <= high:
            raise ValueError("Malformed candle range or minute alignment")
        return self


class CandlesPayload(RootModel[list[Candle]]):
    pass


class BTCFeed:
    def __init__(self, client: httpx.Client):
        self.client = client

    def get_quote(self) -> BTCQuote:
        response = self.client.get(f"{BASE_URL}/products/BTC-USD/ticker")
        response.raise_for_status()
        payload = TickerPayload.model_validate_json(response.content)
        return BTCQuote(timestamp_utc=payload.time, price=payload.price)

    def get_window_open(self, start: datetime) -> WindowOpen:
        if start.utcoffset() is None or start.timestamp() % 300:
            raise ValueError("Window start must be aware and five-minute aligned")
        now = utc_now()
        if now <= start:
            raise OpenPriceUnavailable("The opening observation is not available before the window")
        # Coinbase caches even empty candle responses for 300s. Advance the real
        # query end while the window is active, rather than repeatedly reading a
        # cached initial miss. The selected opening bucket remains exactly start.
        end = min(now, start + timedelta(minutes=5))
        response = self.client.get(
            f"{BASE_URL}/products/BTC-USD/candles",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "granularity": "60",
            },
        )
        response.raise_for_status()
        # Preserve numeric JSON prices as Decimals rather than passing through float.
        payload = CandlesPayload.model_validate(json.loads(response.content, parse_float=Decimal))
        matches = [candle for candle in payload.root if candle.root[0] == int(start.timestamp())]
        if not matches:
            raise OpenPriceUnavailable(
                f"No Coinbase starting-minute candle for {start.isoformat()}"
            )
        if len(matches) != 1:
            raise ValueError("Duplicate starting-minute candles")
        # Coinbase defines open as the bucket's first trade, not a quote at startup.
        return WindowOpen(
            window_start_utc=start, observed_at_utc=utc_now(), price=matches[0].root[3]
        )
