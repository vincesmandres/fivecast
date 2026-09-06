"""Bounded-skew synchronization; no interpolation or synthetic opening prices."""

from datetime import datetime

from fivecast.config import Settings
from fivecast.models import (
    BTCQuote,
    MarketSnapshot,
    OutcomeQuote,
    PredictionMarket,
    SnapshotQuality,
    WindowOpen,
    spread,
)


class SnapshotUnavailable(RuntimeError):
    """Valid source data cannot form a timely active snapshot on this iteration."""


def measure_quality(
    snapshot: MarketSnapshot,
    poll_latency_ms: float | None = None,
    interval_seconds: float | None = None,
) -> SnapshotQuality:
    times = [snapshot.btc_timestamp_utc, snapshot.up_timestamp_utc, snapshot.down_timestamp_utc]
    return SnapshotQuality(
        btc_source_timestamp=snapshot.btc_timestamp_utc,
        market_source_timestamp=min(snapshot.up_timestamp_utc, snapshot.down_timestamp_utc),
        source_skew_ms=(max(times) - min(times)).total_seconds() * 1000,
        poll_latency_ms=poll_latency_ms,
        is_stale=None
        if interval_seconds is None
        else (snapshot.timestamp_utc - min(times)).total_seconds() > interval_seconds,
    )


def build_snapshot(
    market: PredictionMarket,
    btc: BTCQuote,
    opening: WindowOpen,
    up: OutcomeQuote,
    down: OutcomeQuote,
    timestamp: datetime,
    settings: Settings,
) -> MarketSnapshot:
    if timestamp.utcoffset() is None:
        raise ValueError("Snapshot timestamp must be timezone-aware")
    if not market.start_time_utc <= timestamp < market.end_time_utc:
        raise SnapshotUnavailable("Market window elapsed or has not started")
    if up.outcome != "UP" or up.token_id != market.up_token_id:
        raise ValueError("UP quote does not match discovered market token")
    if down.outcome != "DOWN" or down.token_id != market.down_token_id:
        raise ValueError("DOWN quote does not match discovered market token")
    if opening.window_start_utc != market.start_time_utc or opening.source != btc.source:
        raise ValueError("BTC opening observation belongs to a different window or source")
    times = [btc.timestamp_utc, up.timestamp_utc, down.timestamp_utc]
    for value in times:
        age = (timestamp - value).total_seconds()
        if value < market.start_time_utc or age < 0 or age > settings.max_quote_age_seconds:
            raise SnapshotUnavailable(
                "Quote is pre-window, future-dated, or stale: "
                f"source_time={value.isoformat()} snapshot_time={timestamp.isoformat()} "
                f"age={age:.6f}s"
            )
    if (max(times) - min(times)).total_seconds() > settings.max_quote_skew_seconds:
        raise SnapshotUnavailable("Quote timestamps exceed the configured synchronization skew")
    delta = btc.price - opening.price
    return MarketSnapshot(
        timestamp_utc=timestamp,
        market_id=market.market_id,
        market_start_utc=market.start_time_utc,
        market_end_utc=market.end_time_utc,
        seconds_remaining=(market.end_time_utc - timestamp).total_seconds(),
        btc_price=btc.price,
        btc_window_open_price=opening.price,
        btc_delta_usd=delta,
        btc_delta_pct=delta / opening.price,
        up_bid=up.best_bid,
        up_ask=up.best_ask,
        down_bid=down.best_bid,
        down_ask=down.best_ask,
        up_spread=spread(up.best_bid, up.best_ask),
        down_spread=spread(down.best_bid, down.best_ask),
        source_btc=btc.source,
        source_prediction_market=up.source,
        btc_timestamp_utc=btc.timestamp_utc,
        up_timestamp_utc=up.timestamp_utc,
        down_timestamp_utc=down.timestamp_utc,
        up_token_id=up.token_id,
        down_token_id=down.token_id,
        btc_open_observed_at_utc=opening.observed_at_utc,
        btc_open_method=opening.method,
    )
