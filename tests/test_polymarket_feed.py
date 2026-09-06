from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from fivecast.feeds.polymarket import NoActiveMarket, PolymarketFeed

NOW = datetime(2024, 1, 1, 0, 2, tzinfo=UTC)
EPOCH = 1704067200
CONDITION = "0x" + "a" * 64


def market_payload(**overrides):
    payload = {
        "id": "market-1",
        "conditionId": CONDITION,
        "question": "Will BTC go up?",
        "slug": f"btc-updown-5m-{EPOCH}",
        "eventStartTime": "2023-12-31T19:00:00-05:00",
        "endDate": "2024-01-01T00:05:00Z",
        "outcomes": '["UP", "DOWN"]',
        "clobTokenIds": '["111", "222"]',
        "active": True,
        "closed": False,
        "archived": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
    }
    payload.update(overrides)
    return payload


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_market_reverses_positional_outcome_mapping_and_normalizes_times():
    payload = market_payload(outcomes='[" DOWN ", "up"]', clobTokenIds='["222", "111"]')
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client:
        market = PolymarketFeed(client).discover_market(NOW)

    assert market.up_token_id == "111"
    assert market.down_token_id == "222"
    assert market.start_time_utc == datetime(2024, 1, 1, tzinfo=UTC)
    assert market.end_time_utc == datetime(2024, 1, 1, 0, 5, tzinfo=UTC)


def test_discover_market_raises_for_missing_market():
    client = client_for(lambda request: httpx.Response(404, request=request))
    with client, pytest.raises(NoActiveMarket):
        PolymarketFeed(client).discover_market(NOW)


@pytest.mark.parametrize("flag", ["active", "enableOrderBook", "acceptingOrders"])
def test_discover_market_rejects_disabled_flags(flag):
    client = client_for(
        lambda request: httpx.Response(200, json=market_payload(**{flag: False}), request=request)
    )
    with client, pytest.raises(NoActiveMarket):
        PolymarketFeed(client).discover_market(NOW)


@pytest.mark.parametrize("flag", ["closed", "archived"])
def test_discover_market_rejects_closed_or_archived_flags(flag):
    client = client_for(
        lambda request: httpx.Response(200, json=market_payload(**{flag: True}), request=request)
    )
    with client, pytest.raises(NoActiveMarket):
        PolymarketFeed(client).discover_market(NOW)


@pytest.mark.parametrize(
    "outcomes,tokens",
    [('["UP"]', '["111"]'), ('["UP", "UP"]', '["111", "222"]'), ('["UP", "DOWN"]', '["111"]')],
)
def test_discover_market_rejects_missing_or_invalid_mapping(outcomes, tokens):
    payload = market_payload(outcomes=outcomes, clobTokenIds=tokens)
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client, pytest.raises(ValueError, match="outcome/token mapping"):
        PolymarketFeed(client).discover_market(NOW)


def test_discover_market_rejects_wrong_returned_slug():
    payload = market_payload(
        slug="btc-updown-5m-1704067500",
        eventStartTime="2023-12-31T19:05:00-05:00",
        endDate="2024-01-01T00:10:00Z",
    )
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client, pytest.raises(ValueError, match="different market"):
        PolymarketFeed(client).discover_market(NOW)


def test_discover_market_rejects_wrong_window_range():
    payload = market_payload(endDate="2024-01-01T00:06:00Z")
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client, pytest.raises(ValueError, match="five-minute"):
        PolymarketFeed(client).discover_market(NOW)


def test_discover_market_raises_for_http_error():
    client = client_for(lambda request: httpx.Response(500, request=request))
    with client, pytest.raises(httpx.HTTPStatusError):
        PolymarketFeed(client).discover_market(NOW)


def make_book(
    token="111",
    market=CONDITION,
    bids=None,
    asks=None,
    timestamp="1704067320123",
    asset_id=None,
):
    return {
        "market": market,
        "asset_id": token if asset_id is None else asset_id,
        "timestamp": timestamp,
        "bids": bids if bids is not None else [{"price": "0.41", "size": "2"}],
        "asks": asks if asks is not None else [{"price": "0.59", "size": "3"}],
    }


def test_get_quote_uses_bid_max_and_ask_min_for_requested_outcome():
    market = PolymarketFeed(
        client_for(lambda request: httpx.Response(200, json=market_payload(), request=request))
    ).discover_market(NOW)
    payload = make_book(
        bids=[{"price": "0.4", "size": "1"}, {"price": "0.55", "size": "1"}],
        asks=[{"price": "0.7", "size": "1"}, {"price": "0.6", "size": "1"}],
    )
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client:
        quote = PolymarketFeed(client).get_quote(market, "UP")

    assert quote.best_bid == Decimal("0.55")
    assert quote.best_ask == Decimal("0.6")
    assert quote.timestamp_utc == datetime(2024, 1, 1, 0, 2, 0, 123000, tzinfo=UTC)


def test_get_quote_uses_down_token_for_down_request():
    market_client = client_for(
        lambda request: httpx.Response(200, json=market_payload(), request=request)
    )
    with market_client:
        market = PolymarketFeed(market_client).discover_market(NOW)
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=make_book(token="222"), request=request)

    client = client_for(handler)
    with client:
        quote = PolymarketFeed(client).get_quote(market, "DOWN")
    assert seen["token_id"] == "222"
    assert quote.outcome == "DOWN"


@pytest.mark.parametrize("field", ["asset_id", "market"])
def test_get_quote_rejects_wrong_token_or_condition(field):
    market_client = client_for(
        lambda request: httpx.Response(200, json=market_payload(), request=request)
    )
    with market_client:
        market = PolymarketFeed(market_client).discover_market(NOW)
    wrong = "999" if field == "asset_id" else "0x" + "b" * 64
    payload = make_book(**{field: wrong})
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client, pytest.raises(ValueError, match="token or condition"):
        PolymarketFeed(client).get_quote(market, "UP")


def test_get_quote_allows_empty_book_sides():
    market_client = client_for(
        lambda request: httpx.Response(200, json=market_payload(), request=request)
    )
    with market_client:
        market = PolymarketFeed(market_client).discover_market(NOW)
    client = client_for(
        lambda request: httpx.Response(200, json=make_book(bids=[], asks=[]), request=request)
    )
    with client:
        quote = PolymarketFeed(client).get_quote(market, "UP")
    assert quote.best_bid is None
    assert quote.best_ask is None


def test_get_quote_rejects_crossed_book():
    market_client = client_for(
        lambda request: httpx.Response(200, json=market_payload(), request=request)
    )
    with market_client:
        market = PolymarketFeed(market_client).discover_market(NOW)
    payload = make_book(bids=[{"price": "0.8", "size": "1"}], asks=[{"price": "0.2", "size": "1"}])
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client, pytest.raises(ValueError, match="Best bid exceeds best ask"):
        PolymarketFeed(client).get_quote(market, "UP")


@pytest.mark.parametrize("price", ["-0.1", "1.1", "NaN"])
def test_get_quote_rejects_invalid_probability(price):
    market_client = client_for(
        lambda request: httpx.Response(200, json=market_payload(), request=request)
    )
    with market_client:
        market = PolymarketFeed(market_client).discover_market(NOW)
    payload = make_book(bids=[{"price": price, "size": "1"}])
    client = client_for(lambda request: httpx.Response(200, json=payload, request=request))
    with client, pytest.raises(ValueError):
        PolymarketFeed(client).get_quote(market, "UP")


def test_get_quote_rejects_invalid_outcome():
    market_client = client_for(
        lambda request: httpx.Response(200, json=market_payload(), request=request)
    )
    with market_client:
        market = PolymarketFeed(market_client).discover_market(NOW)
    client = client_for(lambda request: httpx.Response(200, json=make_book(), request=request))
    with client, pytest.raises(ValueError, match="Expected UP or DOWN"):
        PolymarketFeed(client).get_quote(market, "MAYBE")


def test_condition_hex_casing_does_not_change_identity(market):
    payload = make_book(market="0x" + "A" * 64)
    with client_for(lambda request: httpx.Response(200, json=payload)) as client:
        assert PolymarketFeed(client).get_quote(market, "UP").token_id == "111"
