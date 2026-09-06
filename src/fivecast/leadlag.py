"""Descriptive research-only lead/lag responses; never imported by strategy code."""

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from fivecast.experiment import split_chronologically
from fivecast.replay import MarketReplay, ReplaySnapshot, ReplayStore

RETURN_WINDOWS = (5, 10, 15, 30, 60)
RESPONSE_LAGS = (-30, -15, -10, -5, 0, 5, 10, 15, 30)
EVENT_BUCKETS = ((5, 10), (10, 20), (20, 30), (30, 40), (40, None))
RESPONSE_TIMES = (0, 5, 10, 15, 30)
EVENT_COOLDOWN_SECONDS = 30
MATCH_TOLERANCE_SECONDS = 7.5


def _quantile(values: Iterable[Decimal], percentage: Decimal) -> Decimal | None:
    values = sorted(values)
    if not values:
        return None
    position = Decimal(len(values) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summary(values: Iterable[Decimal]) -> dict[str, Decimal | int | None]:
    values = tuple(values)
    return {
        "count": len(values),
        "mean": None if not values else sum(values, Decimal(0)) / len(values),
        "median": _quantile(values, Decimal(50)),
        "p25": _quantile(values, Decimal(25)),
        "p75": _quantile(values, Decimal(75)),
    }


def _at_or_before(
    rows: Sequence[ReplaySnapshot], index: int, seconds: int
) -> ReplaySnapshot | None:
    cutoff = rows[index].snapshot.timestamp_utc.timestamp() - seconds
    choices = [row for row in rows[: index + 1] if row.snapshot.timestamp_utc.timestamp() <= cutoff]
    return choices[-1] if choices else None


def _at_or_after(rows: Sequence[ReplaySnapshot], index: int, seconds: int) -> ReplaySnapshot | None:
    target = rows[index].snapshot.timestamp_utc.timestamp() + seconds
    choices = [row for row in rows[index:] if row.snapshot.timestamp_utc.timestamp() >= target]
    if not choices:
        return None
    candidate = choices[0]
    return (
        candidate
        if candidate.snapshot.timestamp_utc.timestamp() - target <= MATCH_TOLERANCE_SECONDS
        else None
    )


def _selected_ask(row: ReplaySnapshot, direction: Decimal) -> Decimal | None:
    return row.snapshot.up_ask if direction >= 0 else row.snapshot.down_ask


def _up_ask(row: ReplaySnapshot) -> Decimal | None:
    return row.snapshot.up_ask


def _pearson(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    if len(pairs) < 2:
        return None
    left = [value[0] for value in pairs]
    right = [value[1] for value in pairs]
    mean_left = sum(left, Decimal(0)) / len(left)
    mean_right = sum(right, Decimal(0)) / len(right)
    numerator = sum((x - mean_left) * (y - mean_right) for x, y in pairs)
    left_sq = sum((x - mean_left) ** 2 for x in left)
    right_sq = sum((y - mean_right) ** 2 for y in right)
    if not left_sq or not right_sq:
        return None
    return numerator / (left_sq * right_sq).sqrt()


@dataclass(frozen=True, slots=True)
class Event:
    market_id: str
    timestamp_utc: str
    direction: str
    bucket: str
    btc_change_usd: Decimal
    selected_ask: Decimal


def detect_events(
    replay: MarketReplay, window: int = 5, cooldown: int = EVENT_COOLDOWN_SECONDS
) -> tuple[Event, ...]:
    events: list[Event] = []
    last_event = None
    for index, current in enumerate(replay.snapshots):
        prior = _at_or_before(replay.snapshots, index, window)
        if prior is None:
            continue
        change = current.snapshot.btc_price - prior.snapshot.btc_price
        magnitude = abs(change)
        bucket = next(
            (
                f"{low}-{high}" if high else "40+"
                for low, high in EVENT_BUCKETS
                if magnitude >= low and (high is None or magnitude < high)
            ),
            None,
        )
        if bucket is None:
            continue
        now = current.snapshot.timestamp_utc
        if last_event is not None and (now - last_event).total_seconds() < cooldown:
            continue
        ask = _selected_ask(current, change)
        if ask is None:
            continue
        events.append(
            Event(
                replay.market_id,
                now.isoformat(),
                "positive" if change >= 0 else "negative",
                bucket,
                change,
                ask,
            )
        )
        last_event = now
    return tuple(events)


def event_responses(replay: MarketReplay) -> list[dict]:
    rows: list[dict] = []
    for event in detect_events(replay):
        index = next(
            index
            for index, value in enumerate(replay.snapshots)
            if value.snapshot.timestamp_utc.isoformat() == event.timestamp_utc
        )
        for lag in RESPONSE_TIMES:
            future = _at_or_after(replay.snapshots, index, lag)
            ask = None if future is None else _selected_ask(future, event.btc_change_usd)
            if ask is not None:
                rows.append(
                    {
                        **asdict(event),
                        "response_lag_seconds": lag,
                        "ask_change": ask - event.selected_ask,
                    }
                )
    return rows


def cross_correlation(replays: Sequence[MarketReplay], window: int = 5) -> dict[str, dict]:
    answer = {}
    for lag in RESPONSE_LAGS:
        pairs: list[tuple[Decimal, Decimal]] = []
        for replay in replays:
            for index, current in enumerate(replay.snapshots):
                prior = _at_or_before(replay.snapshots, index, window)
                if prior is None or current.snapshot.btc_price == 0:
                    continue
                target = (
                    _at_or_before(replay.snapshots, index, -lag)
                    if lag < 0
                    else _at_or_after(replay.snapshots, index, lag)
                )
                if target is None:
                    continue
                start, end = (
                    (_up_ask(target), _up_ask(current))
                    if lag < 0
                    else (_up_ask(current), _up_ask(target))
                )
                if start is None or end is None:
                    continue
                pairs.append(
                    (
                        (current.snapshot.btc_price - prior.snapshot.btc_price)
                        / prior.snapshot.btc_price,
                        end - start,
                    )
                )
        answer[str(lag)] = {"observations": len(pairs), "pearson_correlation": _pearson(pairs)}
    return answer


def repricing_time(replays: Sequence[MarketReplay]) -> dict[str, dict]:
    values: dict[str, list[Decimal]] = {"40+": []}
    for replay in replays:
        for event in detect_events(replay):
            if abs(event.btc_change_usd) < 40:
                continue
            index = next(
                i
                for i, row in enumerate(replay.snapshots)
                if row.snapshot.timestamp_utc.isoformat() == event.timestamp_utc
            )
            initial = event.selected_ask
            final = _at_or_after(replay.snapshots, index, 30)
            final_ask = None if final is None else _selected_ask(final, event.btc_change_usd)
            if final_ask is None or final_ask == initial:
                continue
            target = abs(final_ask - initial) * Decimal("0.8")
            for lag in RESPONSE_TIMES:
                response = _at_or_after(replay.snapshots, index, lag)
                ask = None if response is None else _selected_ask(response, event.btc_change_usd)
                if ask is not None and abs(ask - initial) >= target:
                    values["40+"].append(
                        Decimal(
                            str(
                                (
                                    response.snapshot.timestamp_utc
                                    - replay.snapshots[index].snapshot.timestamp_utc
                                ).total_seconds()
                            )
                        )
                    )
                    break
    return {bucket: summary(value) for bucket, value in values.items()}


def analyze(replays: Sequence[MarketReplay]) -> dict:
    responses = event_responses_by_bucket(replays)
    return {
        "research_markets": len(replays),
        "event_study": responses,
        "cross_correlation": {
            str(window): cross_correlation(replays, window) for window in RETURN_WINDOWS
        },
        "time_to_reprice_80pct_of_30s": repricing_time(replays),
    }


def event_responses_by_bucket(replays: Sequence[MarketReplay]) -> dict:
    grouped: dict[str, dict[str, list[Decimal]]] = {}
    for replay in replays:
        for row in event_responses(replay):
            key = f"{row['bucket']}:{row['direction']}"
            grouped.setdefault(key, {}).setdefault(str(row["response_lag_seconds"]), []).append(
                row["ask_change"]
            )
    return {
        key: {lag: summary(values) for lag, values in lags.items()} for key, lags in grouped.items()
    }


def research_replays(path: Path) -> tuple[MarketReplay, ...]:
    selected = ReplayStore(path).load_eligible().replays
    research, _holdout = split_chronologically(selected)
    return research


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline research-split BTC/market lead-lag study")
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    args = parser.parse_args(argv)
    print(
        json.dumps(analyze(research_replays(args.db_path)), default=str, indent=2, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
