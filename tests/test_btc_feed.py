from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from fivecast.feeds.btc import BTCFeed, OpenPriceUnavailable


def client_for(payload, status_code=200):
    def handler(request):
        return httpx.Response(status_code, json=payload, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_quote_preserves_decimal_and_normalizes_timestamp():
    client = client_for({"price": "65000.123456789012345678", "time": "2024-01-01T12:00:00-05:00"})
    with client:
        quote = BTCFeed(client).get_quote()

    assert quote.price == Decimal("65000.123456789012345678")
    assert quote.timestamp_utc == datetime(2024, 1, 1, 17, tzinfo=UTC)


def test_get_quote_raises_for_http_error():
    client = client_for({"error": "unavailable"}, status_code=503)
    with client, pytest.raises(httpx.HTTPStatusError):
        BTCFeed(client).get_quote()


@pytest.mark.parametrize(
    "payload",
    [
        {"price": "not-a-price", "time": "2024-01-01T00:00:00Z"},
        {"price": "1", "time": "not-a-timestamp"},
    ],
)
def test_get_quote_rejects_malformed_payload(payload):
    client = client_for(payload)
    with client, pytest.raises(ValueError):
        BTCFeed(client).get_quote()


def test_get_window_open_selects_exact_minute_and_preserves_json_precision():
    start = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    payload = [
        [1704067560, "1", "2", "1.5", "1.8", "10"],
        [
            1704067500,
            "64999.123456789012345678",
            "65001",
            "65000.123456789012345678",
            "65000.5",
            "3",
        ],
    ]
    client = client_for(payload)
    with client:
        result = BTCFeed(client).get_window_open(start)

    assert result.price == Decimal("65000.123456789012345678")
    assert result.window_start_utc == start


def test_get_window_open_bounds_query_to_the_five_minute_window():
    start = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=[[1704067500, "1", "2", "1.5", "1.8", "10"]],
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with client:
        BTCFeed(client).get_window_open(start)

    assert seen["granularity"] == "60"
    assert seen["start"] == start.isoformat()
    assert seen["end"] == "2024-01-01T00:10:00+00:00"


def test_get_window_open_rejects_missing_exact_starting_candle():
    start = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    client = client_for([[1704067560, "1", "2", "1.5", "1.8", "10"]])
    with client, pytest.raises(OpenPriceUnavailable):
        BTCFeed(client).get_window_open(start)


def test_get_window_open_rejects_duplicate_starting_candles():
    start = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    candle = [1704067500, "1", "2", "1.5", "1.8", "10"]
    client = client_for([candle, candle])
    with client, pytest.raises(ValueError, match="Duplicate"):
        BTCFeed(client).get_window_open(start)


@pytest.mark.parametrize(
    "candle",
    [
        [1704067501, "1", "2", "1.5", "1.8", "10"],
        [1704067500, "2", "1", "1.5", "1.8", "10"],
        [1704067500, "1", "2", "2.1", "1.8", "10"],
        [1704067500, "1", "2", "1.5", "1.8", "-1"],
    ],
)
def test_get_window_open_rejects_malformed_candle(candle):
    start = datetime(2024, 1, 1, 0, 5, tzinfo=UTC)
    client = client_for([candle])
    with client, pytest.raises(ValueError):
        BTCFeed(client).get_window_open(start)


@pytest.mark.parametrize(
    "start",
    [datetime(2024, 1, 1, 0, 5), datetime(2024, 1, 1, 0, 6, tzinfo=UTC)],
)
def test_get_window_open_requires_aware_five_minute_aligned_start(start):
    client = client_for([])
    with client, pytest.raises(ValueError, match="aware and five-minute"):
        BTCFeed(client).get_window_open(start)


def test_empty_candle_cache_does_not_poison_the_whole_next_window(monkeypatch):
    start = datetime(2026, 9, 6, 6, 20, tzinfo=UTC)
    now = start + timedelta(seconds=1)
    monkeypatch.setattr("fivecast.feeds.btc.utc_now", lambda: now)
    cache = {}
    seen = []

    def respond(request):
        url = str(request.url)
        seen.append(dict(request.url.params))
        if url not in cache:
            cache[url] = (
                []
                if now < start + timedelta(seconds=60)
                else [[int(start.timestamp()), "99", "101", "100", "100.5", "2"]]
            )
        return httpx.Response(
            200, json=cache[url], headers={"Cache-Control": "public, max-age=300"}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        feed = BTCFeed(client)
        with pytest.raises(OpenPriceUnavailable):
            feed.get_window_open(start)
        now += timedelta(seconds=60)
        result = feed.get_window_open(start)
    assert seen[0]["start"] == seen[1]["start"] == start.isoformat()
    assert seen[0]["end"] != seen[1]["end"]
    assert result.price == Decimal("100")
    assert result.window_start_utc == start
