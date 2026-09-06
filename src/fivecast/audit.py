"""Read-only research-split signal-density audit; never ranks or settles strategies."""

import argparse
import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from fivecast.experiment import parameter_grid, split_chronologically
from fivecast.replay import MarketReplay, ReplaySnapshot, ReplayStore

THRESHOLDS = (20, 30, 40, 60, 80, 100, 120)
TIME_BUCKETS = (120, 90, 60, 30, 15)
PRICE_BUCKETS = (
    Decimal("0.60"),
    Decimal("0.70"),
    Decimal("0.75"),
    Decimal("0.80"),
    Decimal("0.85"),
    Decimal("0.90"),
    Decimal("0.95"),
)
REPRESENTATIVE = ((20, 120), (40, 90), (60, 60), (80, 30))


@dataclass(frozen=True, slots=True)
class Crossing:
    market_id: str
    threshold: int
    time_bucket: int
    timestamp_utc: str
    seconds_remaining: float
    side: str
    ask: Decimal | None
    bid: Decimal | None
    spread: Decimal | None
    source_skew_ms: float


def _candidate(replay: MarketReplay, threshold: int, remaining: int) -> ReplaySnapshot | None:
    for item in replay.snapshots:
        if (
            item.snapshot.seconds_remaining <= remaining
            and abs(item.snapshot.btc_delta_usd) >= threshold
        ):
            return item
    return None


def _quote(item: ReplaySnapshot) -> tuple[str, Decimal | None, Decimal | None, Decimal | None]:
    snapshot = item.snapshot
    if snapshot.btc_delta_usd >= 0:
        return "UP", snapshot.up_ask, snapshot.up_bid, snapshot.up_spread
    return "DOWN", snapshot.down_ask, snapshot.down_bid, snapshot.down_spread


def first_crossing(replay: MarketReplay, threshold: int, remaining: int) -> Crossing | None:
    item = _candidate(replay, threshold, remaining)
    if item is None:
        return None
    side, ask, bid, spread = _quote(item)
    return Crossing(
        replay.market_id,
        threshold,
        remaining,
        item.snapshot.timestamp_utc.isoformat(),
        item.snapshot.seconds_remaining,
        side,
        ask,
        bid,
        spread,
        item.quality.source_skew_ms,
    )


def _percentile(values: Iterable[Decimal], percentile: Decimal) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = (Decimal(len(ordered) - 1) * percentile) / Decimal(100)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _summary(values: Iterable[Decimal]) -> dict[str, Decimal | None]:
    values = tuple(values)
    return {
        "mean": None if not values else sum(values, Decimal(0)) / Decimal(len(values)),
        "median": _percentile(values, Decimal(50)),
        "min": None if not values else min(values),
        "max": None if not values else max(values),
        "p25": _percentile(values, Decimal(25)),
        "p75": _percentile(values, Decimal(75)),
        "p90": _percentile(values, Decimal(90)),
        "p95": _percentile(values, Decimal(95)),
    }


def _blockers(
    replay: MarketReplay,
    threshold: Decimal,
    remaining: float,
    entry: Decimal,
    spread: Decimal,
    skew: float,
) -> tuple[str, tuple[str, ...]]:
    candidates = tuple(
        item for item in replay.snapshots if abs(item.snapshot.btc_delta_usd) >= threshold
    )
    if not candidates:
        return "DELTA_NOT_REACHED", ("DELTA_NOT_REACHED",)
    timed = tuple(item for item in candidates if item.snapshot.seconds_remaining <= remaining)
    if not timed:
        return "TIME_NOT_ELIGIBLE", ("TIME_NOT_ELIGIBLE",)
    applicable: set[str] = set()
    for item in timed:
        item_blockers: set[str] = set()
        _side, ask, _bid, item_spread = _quote(item)
        if ask is None or item_spread is None:
            item_blockers.add("NO_VALID_QUOTE")
        else:
            if ask > entry:
                item_blockers.add("ENTRY_PRICE_TOO_HIGH")
            if item_spread > spread:
                item_blockers.add("SPREAD_TOO_WIDE")
        if item.quality.source_skew_ms > skew:
            item_blockers.add("SKEW_TOO_HIGH")
        if item.quality.is_stale is not False:
            item_blockers.add("STALE_SNAPSHOT")
        if not item_blockers:
            return "SIGNAL", ("SIGNAL",)
        applicable.update(item_blockers)
    order = (
        "NO_VALID_QUOTE",
        "ENTRY_PRICE_TOO_HIGH",
        "SPREAD_TOO_WIDE",
        "SKEW_TOO_HIGH",
        "STALE_SNAPSHOT",
    )
    return next(value for value in order if value in applicable), tuple(
        value for value in order if value in applicable
    )


def _funnel(replays: Sequence[MarketReplay], threshold: int, remaining: int) -> dict[str, int]:
    counts = {
        "markets": len(replays),
        "delta_reached": 0,
        "within_time": 0,
        "valid_quote": 0,
        "ask_limit": 0,
        "spread_limit": 0,
        "skew_limit": 0,
        "fresh": 0,
        "signals": 0,
    }
    for replay in replays:
        raw = next(
            (item for item in replay.snapshots if abs(item.snapshot.btc_delta_usd) >= threshold),
            None,
        )
        if raw is None:
            continue
        counts["delta_reached"] += 1
        item = _candidate(replay, threshold, remaining)
        if item is None:
            continue
        counts["within_time"] += 1
        _side, ask, _bid, spread = _quote(item)
        if ask is None or spread is None:
            continue
        counts["valid_quote"] += 1
        if ask > Decimal("0.90"):
            continue
        counts["ask_limit"] += 1
        if spread > Decimal("0.05"):
            continue
        counts["spread_limit"] += 1
        if item.quality.source_skew_ms > 5000:
            continue
        counts["skew_limit"] += 1
        if item.quality.is_stale is not False:
            continue
        counts["fresh"] += 1
        counts["signals"] += 1
    return counts


def audit(replays: Sequence[MarketReplay]) -> dict:
    if any(not replay.snapshots for replay in replays):
        raise ValueError("Audit cannot use an empty market replay")
    matrix: dict[str, dict[str, dict[str, int | Decimal]]] = {}
    crossings: list[Crossing] = []
    prices: dict[str, dict[str, dict]] = {}
    for threshold in THRESHOLDS:
        matrix[str(threshold)] = {}
        prices[str(threshold)] = {}
        for remaining in TIME_BUCKETS:
            values = [
                value
                for replay in replays
                if (value := first_crossing(replay, threshold, remaining))
            ]
            crossings.extend(values)
            matrix[str(threshold)][str(remaining)] = {
                "markets": len(values),
                "pct": Decimal(len(values)) * 100 / Decimal(len(replays)),
            }
            asks = [value.ask for value in values if value.ask is not None]
            prices[str(threshold)][str(remaining)] = {"quoted_markets": len(asks), **_summary(asks)}
    price_buckets = {}
    for threshold in THRESHOLDS:
        rows = [first_crossing(replay, threshold, 120) for replay in replays]
        asks = [row.ask for row in rows if row is not None and row.ask is not None]
        counts = {f"<={upper}": sum(value <= upper for value in asks) for upper in PRICE_BUCKETS}
        counts[">0.95"] = sum(value > Decimal("0.95") for value in asks)
        price_buckets[str(threshold)] = counts
    blockers: dict[str, int] = {}
    all_blockers: dict[str, int] = {}
    for params in parameter_grid():
        for replay in replays:
            first, every = _blockers(
                replay,
                params.delta_threshold_usd,
                params.max_seconds_remaining,
                params.max_entry_price,
                params.max_spread,
                params.max_skew_ms,
            )
            blockers[first] = blockers.get(first, 0) + 1
            for value in every:
                all_blockers[value] = all_blockers.get(value, 0) + 1
    distributions = {
        "max_positive": [],
        "max_negative": [],
        "max_absolute": [],
        **{f"t_minus_{bucket}": [] for bucket in TIME_BUCKETS},
    }
    repricing: dict[str, list[Decimal]] = {
        "0-20": [],
        "20-40": [],
        "40-60": [],
        "60-80": [],
        "80-100": [],
        "100+": [],
    }
    for replay in replays:
        deltas = [item.snapshot.btc_delta_usd for item in replay.snapshots]
        distributions["max_positive"].append(max([Decimal(0), *deltas]))
        distributions["max_negative"].append(abs(min([Decimal(0), *deltas])))
        distributions["max_absolute"].append(max(abs(value) for value in deltas))
        for bucket in TIME_BUCKETS:
            at_cutoff = [
                item for item in replay.snapshots if item.snapshot.seconds_remaining >= bucket
            ]
            if at_cutoff:
                distributions[f"t_minus_{bucket}"].append(abs(at_cutoff[-1].snapshot.btc_delta_usd))
        for item in replay.snapshots:
            side, ask, _bid, _spread = _quote(item)
            if ask is None:
                continue
            delta = abs(item.snapshot.btc_delta_usd)
            label = (
                "100+" if delta >= 100 else f"{int(delta // 20) * 20}-{int(delta // 20) * 20 + 20}"
            )
            repricing[label].append(ask)
    return {
        "research_market_count": len(replays),
        "threshold_matrix": matrix,
        "price_response": prices,
        "price_buckets": price_buckets,
        "first_blockers": blockers,
        "all_blockers": all_blockers,
        "blocker_denominator": len(replays) * len(parameter_grid()),
        "funnels": {
            f"D{threshold}_T{remaining}": _funnel(replays, threshold, remaining)
            for threshold, remaining in REPRESENTATIVE
        },
        "momentum_distribution": {key: _summary(values) for key, values in distributions.items()},
        "repricing": {
            key: {"observations": len(values), **_summary(values)}
            for key, values in repricing.items()
        },
        "crossings": [asdict(value) for value in crossings],
    }


def research_replays(path: Path) -> tuple[tuple[MarketReplay, ...], int]:
    selection = ReplayStore(path).load_eligible()
    research, _holdout = split_chronologically(selection.replays)
    return research, len(selection.rejected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline research-split FiveCast signal-density audit"
    )
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument(
        "--export", type=Path, help="Optional CSV of first threshold/time crossings"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    replays, excluded = research_replays(args.db_path)
    result = audit(replays)
    result["excluded_market_count"] = excluded
    if args.export:
        with args.export.open("w", newline="", encoding="ascii") as file:
            writer = csv.DictWriter(file, fieldnames=Crossing.__dataclass_fields__)
            writer.writeheader()
            writer.writerows(result["crossings"])
    if args.json:
        print(json.dumps(result, default=str, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "crossings"},
                default=str,
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
