from datetime import timedelta
from decimal import Decimal

from fivecast.features import calculate_features
from fivecast.leadlag import (
    analyze,
    cross_correlation,
    detect_events,
    event_responses,
    repricing_time,
)
from fivecast.models import SnapshotQuality
from fivecast.replay import MarketReplay, ReplaySnapshot


def _replay(snapshot, prices):
    rows = []
    for offset, price, up, down in prices:
        moment = snapshot.market_start_utc + timedelta(seconds=offset)
        delta = Decimal(price) - snapshot.btc_window_open_price
        current = snapshot.model_validate(
            {
                **snapshot.model_dump(),
                "timestamp_utc": moment,
                "btc_timestamp_utc": moment,
                "up_timestamp_utc": moment,
                "down_timestamp_utc": moment,
                "btc_price": price,
                "btc_delta_usd": delta,
                "btc_delta_pct": delta / snapshot.btc_window_open_price,
                "seconds_remaining": 300 - offset,
                "up_ask": up,
                "down_ask": down,
                "up_bid": Decimal(up) - Decimal("0.01"),
                "down_bid": Decimal(down) - Decimal("0.01"),
                "up_spread": "0.01",
                "down_spread": "0.01",
                "btc_open_observed_at_utc": snapshot.market_start_utc,
            }
        )
        quality = SnapshotQuality(
            btc_source_timestamp=moment,
            market_source_timestamp=moment,
            source_skew_ms=0,
            is_stale=False,
        )
        rows.append(ReplaySnapshot(current, quality, calculate_features((current,))))
    return MarketReplay("m", tuple(rows), "UP")


def test_events_use_fixed_buckets_cooldown_and_directional_quote(snapshot):
    replay = _replay(
        snapshot,
        [
            (0, "100000", "0.5", "0.5"),
            (5, "100006", "0.6", "0.4"),
            (10, "100013", "0.7", "0.3"),
            (40, "100005", "0.8", "0.2"),
        ],
    )
    events = detect_events(replay)
    assert len(events) == 2
    assert (
        events[0].bucket == "5-10"
        and events[0].direction == "positive"
        and events[0].selected_ask == Decimal("0.6")
    )
    assert events[1].direction == "negative" and events[1].selected_ask == Decimal("0.2")


def test_future_quotes_are_labeled_response_only_and_tolerant_to_sampling(snapshot):
    replay = _replay(
        snapshot,
        [
            (0, "100000", "0.5", "0.5"),
            (5, "100006", "0.6", "0.4"),
            (10, "100006", "0.8", "0.2"),
            (15, "100006", "0.9", "0.1"),
        ],
    )
    rows = event_responses(replay)
    plus_five = next(row for row in rows if row["response_lag_seconds"] == 5)
    assert plus_five["ask_change"] == Decimal("0.2")
    assert "response_lag_seconds" in plus_five


def test_cross_correlation_and_reprice_time_are_descriptive(snapshot):
    replay = _replay(
        snapshot,
        [
            (0, "100000", "0.5", "0.5"),
            (5, "100050", "0.6", "0.4"),
            (10, "100050", "0.7", "0.3"),
            (15, "100050", "0.8", "0.2"),
            (20, "100050", "0.9", "0.1"),
            (35, "100050", "1.0", "0.01"),
        ],
    )
    correlations = cross_correlation((replay,))
    assert correlations["5"]["observations"] > 0
    result = repricing_time((replay,))["40+"]
    assert result["count"] == 1
    assert result["median"] == Decimal("30.0")


def test_analysis_calculates_each_declared_historical_return_window(snapshot):
    replay = _replay(
        snapshot,
        [(0, "100000", "0.5", "0.5"), (60, "100050", "0.7", "0.3"), (90, "100060", "0.8", "0.2")],
    )
    assert set(analyze((replay,))["cross_correlation"]) == {"5", "10", "15", "30", "60"}
