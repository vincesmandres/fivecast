from datetime import UTC, datetime

import httpx
import pytest

from fivecast.feeds.polymarket import PolymarketFeed
from fivecast.models import PredictionMarket

SLUG = "btc-updown-5m-1704067200"
CONDITION = "0x" + "a" * 64
MARKET = PredictionMarket(
    market_id="market-1",
    condition_id=CONDITION,
    question="Will BTC go up?",
    slug=SLUG,
    start_time_utc=datetime(2024, 1, 1, tzinfo=UTC),
    end_time_utc=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
    up_token_id="111",
    down_token_id="222",
    status="closed",
)


@pytest.fixture(autouse=True)
def fixed_observation_time(monkeypatch):
    monkeypatch.setattr(
        "fivecast.feeds.polymarket.utc_now", lambda: datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    )


def gamma_payload(**overrides):
    payload = {
        "id": "market-1",
        "conditionId": CONDITION,
        "question": "Will BTC go up?",
        "slug": SLUG,
        "eventStartTime": "2024-01-01T00:00:00Z",
        "endDate": "2024-01-01T00:05:00Z",
        "outcomes": '["UP", "DOWN"]',
        "clobTokenIds": '["111", "222"]',
        "active": False,
        "closed": True,
        "archived": False,
        "enableOrderBook": True,
        "acceptingOrders": False,
        "umaResolutionStatus": "resolved",
        "outcomePrices": '["1", "0"]',
        "closedTime": "2026-09-06 05:41:25+00",
    }
    payload.update(overrides)
    return payload


def clob_payload(outcome="UP", **overrides):
    payload = {
        "condition_id": CONDITION,
        "market_slug": SLUG,
        "closed": True,
        "tokens": [
            {"token_id": "111", "outcome": "UP", "winner": outcome == "UP"},
            {"token_id": "222", "outcome": "DOWN", "winner": outcome == "DOWN"},
        ],
    }
    payload.update(overrides)
    return payload


def settlement_client(gamma=None, clob=None):
    def handler(request):
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(200, json=gamma or gamma_payload(), request=request)
        return httpx.Response(200, json=clob or clob_payload(), request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("outcome", ["UP", "DOWN"])
def test_get_resolution_supports_both_winners_and_reversed_gamma_mapping(outcome):
    gamma = gamma_payload(
        outcomes='["DOWN", "UP"]',
        clobTokenIds='["222", "111"]',
        outcomePrices='["0", "1"]' if outcome == "UP" else '["1", "0"]',
    )
    with settlement_client(gamma, clob_payload(outcome)) as client:
        result = PolymarketFeed(client).get_resolution(MARKET)
    assert result.outcome == outcome


def test_get_resolution_returns_none_until_gamma_is_final():
    with settlement_client(gamma_payload(umaResolutionStatus="pending")) as client:
        assert PolymarketFeed(client).get_resolution(MARKET) is None


def test_get_resolution_returns_none_for_closed_but_unresolved_gamma():
    with settlement_client(gamma_payload(umaResolutionStatus=None)) as client:
        assert PolymarketFeed(client).get_resolution(MARKET) is None


def test_get_resolution_returns_none_during_clob_publication_lag():
    with settlement_client(clob=clob_payload(closed=False)) as client:
        assert PolymarketFeed(client).get_resolution(MARKET) is None


@pytest.mark.parametrize(
    "gamma,clob",
    [
        (
            gamma_payload(),
            {
                **clob_payload(),
                "tokens": [
                    {"token_id": "111", "outcome": "UP", "winner": True},
                    {"token_id": "222", "outcome": "DOWN", "winner": True},
                ],
            },
        ),
        (gamma_payload(outcomePrices='["0.9", "0.1"]'), clob_payload()),
        (gamma_payload(conditionId="0x" + "b" * 64), clob_payload()),
        (
            gamma_payload(),
            {
                **clob_payload(),
                "tokens": [
                    {"token_id": "999", "outcome": "UP", "winner": True},
                    {"token_id": "222", "outcome": "DOWN", "winner": False},
                ],
            },
        ),
    ],
)
def test_get_resolution_rejects_inconsistent_final_data(gamma, clob):
    with settlement_client(gamma, clob) as client, pytest.raises(ValueError):
        PolymarketFeed(client).get_resolution(MARKET)


@pytest.mark.parametrize("closed_time", [None, "2026-09-06T05:41:25-04:00"])
def test_get_resolution_handles_missing_or_offset_closed_time(monkeypatch, closed_time):
    observed = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("fivecast.feeds.polymarket.utc_now", lambda: observed)
    with settlement_client(gamma_payload(closedTime=closed_time)) as client:
        result = PolymarketFeed(client).get_resolution(MARKET)
    if closed_time is None:
        assert result.resolution_time_utc is None
        assert result.resolution_time_source is None
    else:
        assert result.resolution_time_utc == datetime(2026, 9, 6, 9, 41, 25, tzinfo=UTC)


def test_get_resolution_rejects_naive_closed_time():
    with settlement_client(gamma_payload(closedTime="2026-09-06 05:41:25")) as client:
        with pytest.raises(ValueError):
            PolymarketFeed(client).get_resolution(MARKET)


@pytest.mark.parametrize("prices", ['["NaN", "0"]', '["sNaN", "0"]', '["1.1", "0"]'])
def test_nonfinite_or_invalid_final_prices_rejected(prices):
    with settlement_client(gamma_payload(outcomePrices=prices)) as client:
        with pytest.raises(ValueError):
            PolymarketFeed(client).get_resolution(MARKET)


def test_duplicate_nonwinning_token_rejected():
    payload = clob_payload()
    payload["tokens"].append(payload["tokens"][1].copy())
    with settlement_client(clob=payload) as client:
        with pytest.raises(ValueError, match="resolution tokens"):
            PolymarketFeed(client).get_resolution(MARKET)


def test_missing_official_close_timestamp_is_not_invented():
    payload = gamma_payload()
    del payload["closedTime"]
    with settlement_client(payload) as client:
        result = PolymarketFeed(client).get_resolution(MARKET)
        assert result.outcome == "UP"
        assert result.resolution_time_utc is None
        assert result.observed_at_utc == datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
