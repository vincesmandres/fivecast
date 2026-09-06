from datetime import timedelta
from decimal import Decimal

from fivecast.audit import _blockers, audit, first_crossing, research_replays
from fivecast.features import calculate_features
from fivecast.models import MarketResolution, SnapshotQuality
from fivecast.replay import MarketReplay, ReplaySnapshot
from fivecast.storage.sqlite import SnapshotStore


def item(snapshot, delta, seconds, ask="0.7", skew=0, stale=False):
    timestamp = snapshot.market_start_utc + timedelta(seconds=300 - seconds)
    ask_value = Decimal(ask)
    bid = ask_value - Decimal("0.01")
    current = snapshot.model_validate(
        {
            **snapshot.model_dump(),
            "timestamp_utc": timestamp,
            "btc_timestamp_utc": timestamp,
            "up_timestamp_utc": timestamp,
            "down_timestamp_utc": timestamp,
            "btc_price": snapshot.btc_window_open_price + Decimal(delta),
            "btc_delta_usd": Decimal(delta),
            "btc_delta_pct": Decimal(delta) / snapshot.btc_window_open_price,
            "seconds_remaining": seconds,
            "up_bid": bid,
            "down_bid": bid,
            "up_ask": ask_value,
            "down_ask": ask_value,
            "up_spread": Decimal("0.01"),
            "down_spread": Decimal("0.01"),
            "btc_open_observed_at_utc": snapshot.market_start_utc,
        }
    )
    quality = SnapshotQuality(
        btc_source_timestamp=timestamp,
        market_source_timestamp=timestamp,
        source_skew_ms=skew,
        is_stale=stale,
    )
    return ReplaySnapshot(current, quality, calculate_features((current,)))


def replay(snapshot, values, market_id="m"):
    rows = tuple(item(snapshot, *value) for value in values)
    return MarketReplay(market_id, rows, "UP")


def test_first_crossing_is_earliest_in_time_bucket_and_maps_sides(snapshot):
    value = replay(snapshot, [("30", 150, "0.4"), ("45", 90, "0.6"), ("-50", 60, "0.7")])
    positive = first_crossing(value, 40, 120)
    negative = first_crossing(value, 50, 60)
    assert positive.side == "UP"
    assert positive.seconds_remaining == 90
    assert positive.ask == Decimal("0.6")
    assert negative.side == "DOWN"
    assert negative.ask == Decimal("0.7")


def test_blockers_allow_later_valid_snapshot_not_future_quote_selection(snapshot):
    value = replay(snapshot, [("50", 80, "0.95"), ("50", 60, "0.8")])
    first, all_values = _blockers(value, Decimal("40"), 90, Decimal("0.85"), Decimal("0.05"), 5000)
    assert first == "SIGNAL"
    assert all_values == ("SIGNAL",)


def test_audit_counts_market_once_per_threshold_time_and_price_buckets(snapshot):
    up = replay(snapshot, [("45", 100, "0.7"), ("90", 40, "0.99")], "up")
    down = replay(snapshot, [("-45", 80, "0.8")], "down")
    result = audit((up, down))
    assert result["threshold_matrix"]["40"]["120"] == {"markets": 2, "pct": Decimal("100")}
    assert result["threshold_matrix"]["80"]["60"]["markets"] == 1
    assert result["price_response"]["40"]["120"]["mean"] == Decimal("0.75")
    assert result["price_buckets"]["40"]["<=0.60"] == 0
    assert result["price_buckets"]["40"]["<=0.70"] == 1
    assert result["price_buckets"]["40"]["<=0.80"] == 2


def test_funnel_distribution_repricing_and_blocker_denominator(snapshot):
    value = replay(snapshot, [("10", 150, "0.5"), ("50", 90, "0.8"), ("-120", 30, "0.9")])
    result = audit((value,))
    assert result["funnels"]["D40_T90"]["delta_reached"] == 1
    assert result["funnels"]["D40_T90"]["signals"] == 1
    assert result["momentum_distribution"]["max_positive"]["max"] == Decimal("50")
    assert result["momentum_distribution"]["max_negative"]["max"] == Decimal("120")
    assert result["repricing"]["100+"]["observations"] == 1
    assert result["blocker_denominator"] == 100
    assert sum(result["first_blockers"].values()) == 100


def test_research_helper_excludes_holdout_without_evaluating_it(tmp_path, snapshot, market):
    path = tmp_path / "data.db"
    with SnapshotStore(path) as store:
        for index in range(10):
            start = market.start_time_utc + timedelta(minutes=5 * index)
            current = market.model_validate(
                {
                    **market.model_dump(),
                    "market_id": str(100 + index),
                    "slug": f"btc-updown-5m-{int(start.timestamp())}",
                    "start_time_utc": start,
                    "end_time_utc": start + timedelta(minutes=5),
                }
            )
            stored = item(snapshot, "0", 200).snapshot.model_validate(
                {
                    **item(snapshot, "0", 200).snapshot.model_dump(),
                    "market_id": current.market_id,
                    "market_start_utc": start,
                    "market_end_utc": start + timedelta(minutes=5),
                    "timestamp_utc": start + timedelta(seconds=100),
                    "btc_timestamp_utc": start + timedelta(seconds=100),
                    "up_timestamp_utc": start + timedelta(seconds=100),
                    "down_timestamp_utc": start + timedelta(seconds=100),
                    "btc_open_observed_at_utc": start,
                }
            )
            store.save_market(current)
            store.save_resolution(
                MarketResolution(
                    market_id=current.market_id, outcome="UP", observed_at_utc=current.end_time_utc
                )
            )
            store.save_snapshot(
                stored,
                SnapshotQuality(
                    btc_source_timestamp=stored.timestamp_utc,
                    market_source_timestamp=stored.timestamp_utc,
                    source_skew_ms=0,
                    is_stale=False,
                ),
            )
    research, _excluded = research_replays(path)
    assert [value.market_id for value in research] == [str(value) for value in range(100, 107)]
