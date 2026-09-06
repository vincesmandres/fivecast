from datetime import UTC, timedelta, timezone

import pytest
from pydantic import ValidationError

from fivecast.models import BTCQuote, MarketSnapshot, OutcomeQuote, PredictionMarket, WindowOpen


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "not-a-price"])
def test_btc_price_must_be_positive_finite(start, price):
    with pytest.raises(ValidationError):
        BTCQuote(timestamp_utc=start, price=price)


def test_timestamp_normalized_to_utc(start):
    quote = BTCQuote(timestamp_utc=start.astimezone(timezone(timedelta(hours=3))), price="100")
    assert quote.timestamp_utc == start
    assert quote.timestamp_utc.tzinfo == UTC


def test_naive_timestamp_rejected(start):
    with pytest.raises(ValidationError):
        BTCQuote(timestamp_utc=start.replace(tzinfo=None), price="100")


@pytest.mark.parametrize("field,value", [("symbol", "ETH-USD"), ("source", ""), ("extra", 1)])
def test_btc_identity_and_unknown_fields_rejected(start, field, value):
    with pytest.raises(ValidationError):
        BTCQuote.model_validate({"timestamp_utc": start, "price": "100", field: value})


@pytest.mark.parametrize("bid,ask", [("1.01", "1"), ("-0.1", "0.3"), ("0.8", "0.7")])
def test_invalid_outcome_quotes(start, bid, ask):
    with pytest.raises(ValidationError):
        OutcomeQuote(outcome="UP", token_id="111", timestamp_utc=start, best_bid=bid, best_ask=ask)


def test_market_window_and_tokens_are_validated(market):
    for change in (
        {"end_time_utc": market.start_time_utc},
        {"down_token_id": market.up_token_id},
        {"slug": "btc-updown-5m-1788672900"},
    ):
        with pytest.raises(ValidationError):
            PredictionMarket.model_validate({**market.model_dump(), **change})


def test_open_cannot_be_observed_before_start(start):
    with pytest.raises(ValidationError):
        WindowOpen(window_start_utc=start, observed_at_utc=start - timedelta(seconds=1), price="1")


@pytest.mark.parametrize(
    "change",
    [
        {"seconds_remaining": -1},
        {"seconds_remaining": 179},
        {"btc_delta_usd": "0"},
        {"btc_delta_pct": "0.1"},
        {"up_spread": "0.5"},
        {"up_bid": "0.99"},
        {"down_token_id": "111"},
    ],
)
def test_snapshot_cannot_contain_inconsistent_derived_values(snapshot, change):
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate({**snapshot.model_dump(), **change})


def test_models_are_immutable(snapshot):
    with pytest.raises(ValidationError):
        snapshot.btc_price = "1"
