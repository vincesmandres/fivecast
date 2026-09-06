from datetime import timedelta
from decimal import Decimal

import pytest

from fivecast.config import Settings
from fivecast.market.snapshot import SnapshotUnavailable, build_snapshot
from fivecast.models import BTCQuote, OutcomeQuote


def test_delta_spread_and_remaining(snapshot):
    assert snapshot.btc_delta_usd == Decimal("100")
    assert snapshot.btc_delta_pct == Decimal("0.001")
    assert snapshot.seconds_remaining == 180
    assert snapshot.up_spread == Decimal("0.02")
    assert snapshot.down_spread == Decimal("0.03")


def test_negative_delta(inputs):
    inputs["btc"] = BTCQuote(timestamp_utc=inputs["btc"].timestamp_utc, price="99900")
    result = build_snapshot(**inputs)
    assert result.btc_delta_usd == Decimal("-100")
    assert result.btc_delta_pct == Decimal("-0.001")


@pytest.mark.parametrize("offset", [-1, 300, 301])
def test_outside_active_window_rejected(inputs, start, offset):
    inputs["timestamp"] = start + timedelta(seconds=offset)
    with pytest.raises(SnapshotUnavailable):
        build_snapshot(**inputs)


@pytest.mark.parametrize("offset", [-1, 80, 121])
def test_prewindow_stale_and_future_quotes_rejected(inputs, start, offset):
    inputs["btc"] = BTCQuote(timestamp_utc=start + timedelta(seconds=offset), price="100100")
    with pytest.raises(SnapshotUnavailable, match="pre-window, future-dated, or stale"):
        build_snapshot(**inputs)


def test_excessive_quote_skew_rejected(inputs, start):
    inputs["btc"] = BTCQuote(timestamp_utc=start + timedelta(seconds=100), price="100100")
    with pytest.raises(SnapshotUnavailable, match="skew"):
        build_snapshot(**inputs)


def test_freshness_and_skew_limits_are_configurable(inputs, start):
    inputs["btc"] = BTCQuote(timestamp_utc=start + timedelta(seconds=100), price="100100")
    inputs["settings"] = Settings(max_quote_skew_seconds=20)
    assert build_snapshot(**inputs).seconds_remaining == 180


def test_wrong_quote_token_is_rejected(inputs):
    inputs["up"] = OutcomeQuote.model_validate({**inputs["up"].model_dump(), "token_id": "999"})
    with pytest.raises(ValueError, match="UP quote"):
        build_snapshot(**inputs)


def test_missing_book_side_is_not_invented(inputs):
    inputs["up"] = OutcomeQuote.model_validate({**inputs["up"].model_dump(), "best_ask": None})
    result = build_snapshot(**inputs)
    assert result.up_ask is None
    assert result.up_spread is None


def test_wrong_window_open_rejected(inputs):
    inputs["opening"] = inputs["opening"].model_copy(
        update={"window_start_utc": inputs["market"].start_time_utc - timedelta(minutes=5)}
    )
    with pytest.raises(ValueError, match="different window"):
        build_snapshot(**inputs)
