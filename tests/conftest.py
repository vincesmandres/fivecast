import os
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fivecast.config import Settings
from fivecast.market.snapshot import build_snapshot
from fivecast.models import BTCQuote, OutcomeQuote, PredictionMarket, WindowOpen


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Tests must not access the network; use httpx.MockTransport")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    for key in os.environ:
        if key.startswith("FIVECAST_"):
            monkeypatch.delenv(key)


@pytest.fixture
def start():
    return datetime(2026, 9, 6, 5, 30, tzinfo=UTC)


@pytest.fixture
def market(start):
    return PredictionMarket(
        market_id="1234",
        condition_id="0x" + "a" * 64,
        question="Bitcoin Up or Down?",
        slug=f"btc-updown-5m-{int(start.timestamp())}",
        start_time_utc=start,
        end_time_utc=start + timedelta(minutes=5),
        up_token_id="111",
        down_token_id="222",
    )


@pytest.fixture
def inputs(start, market):
    quote_time = start + timedelta(seconds=118)
    return {
        "market": market,
        "btc": BTCQuote(timestamp_utc=quote_time, price="100100"),
        "opening": WindowOpen(
            window_start_utc=start,
            observed_at_utc=start + timedelta(seconds=60),
            price="100000",
        ),
        "up": OutcomeQuote(
            outcome="UP", token_id="111", best_bid="0.6", best_ask="0.62", timestamp_utc=quote_time
        ),
        "down": OutcomeQuote(
            outcome="DOWN",
            token_id="222",
            best_bid="0.37",
            best_ask="0.4",
            timestamp_utc=quote_time,
        ),
        "timestamp": start + timedelta(seconds=120),
        "settings": Settings(),
    }


@pytest.fixture
def snapshot(inputs):
    result = build_snapshot(**inputs)
    assert result.btc_delta_pct == Decimal("0.001")
    return result
