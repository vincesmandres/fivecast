"""Research-split-only shadow experiments over immutable local observations."""

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product
from pathlib import Path

from fivecast.fill import PaperFill, ShadowTrade
from fivecast.replay import MarketReplay, ReplayEngine, ReplayStore
from fivecast.storage.migrations import iso
from fivecast.storage.sqlite import SnapshotStore
from fivecast.strategy import LateMomentumParams, LateMomentumStrategy


@dataclass(frozen=True, slots=True)
class Metrics:
    market_count: int
    trade_count: int
    wins: int
    losses: int
    win_rate: Decimal | None
    average_entry_price: Decimal | None
    gross_pnl: Decimal
    net_pnl: Decimal
    roi: Decimal | None
    ev_per_trade: Decimal | None
    max_drawdown: Decimal
    profit_factor: Decimal | None
    naive_edge: Decimal | None


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    params: LateMomentumParams
    metrics: Metrics
    results: tuple


def split_chronologically(
    replays: Sequence[MarketReplay], research_ratio: Decimal = Decimal("0.7")
) -> tuple[tuple[MarketReplay, ...], tuple[MarketReplay, ...]]:
    if not Decimal(0) < research_ratio < Decimal(1):
        raise ValueError("research_ratio must be between zero and one")
    ordered = tuple(sorted(replays, key=lambda value: value.snapshots[0].snapshot.market_start_utc))
    cut = int(len(ordered) * research_ratio)
    return ordered[:cut], ordered[cut:]


def parameter_grid() -> tuple[LateMomentumParams, ...]:
    return tuple(
        LateMomentumParams(
            delta_threshold_usd=Decimal(str(threshold)),
            max_seconds_remaining=float(seconds),
            max_entry_price=Decimal(str(entry)),
        )
        for threshold, seconds, entry in product(
            (40, 60, 80, 100, 120), (120, 90, 60, 30), ("0.70", "0.75", "0.80", "0.85", "0.90")
        )
    )


def metrics(market_count: int, trades: Iterable[ShadowTrade]) -> Metrics:
    values = tuple(trades)
    wins = sum(trade.gross_pnl > 0 for trade in values)
    losses = len(values) - wins
    gross = sum((trade.gross_pnl for trade in values), Decimal(0))
    net = sum((trade.net_pnl for trade in values), Decimal(0))
    entry_total = sum((trade.entry_price for trade in values), Decimal(0))
    costs = sum((trade.entry_price + trade.fees for trade in values), Decimal(0))
    profit = sum((trade.net_pnl for trade in values if trade.net_pnl > 0), Decimal(0))
    loss = -sum((trade.net_pnl for trade in values if trade.net_pnl < 0), Decimal(0))
    running = peak = Decimal(0)
    drawdown = Decimal(0)
    for trade in values:
        running += trade.net_pnl
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    count = Decimal(len(values))
    win_rate = None if not values else Decimal(wins) / count
    average = None if not values else entry_total / count
    return Metrics(
        market_count=market_count,
        trade_count=len(values),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        average_entry_price=average,
        gross_pnl=gross,
        net_pnl=net,
        roi=None if not values or costs == 0 else net / costs,
        ev_per_trade=None if not values else net / count,
        max_drawdown=drawdown,
        profit_factor=None if loss == 0 else profit / loss,
        naive_edge=None if win_rate is None or average is None else win_rate - average,
    )


def evaluate(replays: Sequence[MarketReplay], params: LateMomentumParams) -> ExperimentResult:
    engine = ReplayEngine()
    strategy = LateMomentumStrategy(params)
    fill = PaperFill()
    results = tuple(engine.evaluate(replay, strategy, params, fill) for replay in replays)
    trades = tuple(result.trade for result in results if result.trade is not None)
    return ExperimentResult(params, metrics(len(replays), trades), results)


def rank(results: Iterable[ExperimentResult]) -> list[ExperimentResult]:
    # Tie-breakers are deterministic and based on research-only evidence.
    return sorted(
        results,
        key=lambda value: (
            value.metrics.ev_per_trade is not None,
            value.metrics.ev_per_trade or Decimal("-Infinity"),
            value.metrics.trade_count,
            -value.params.delta_threshold_usd,
            -value.params.max_seconds_remaining,
            -value.params.max_entry_price,
        ),
        reverse=True,
    )


def _json(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def persist_result(
    path: Path,
    result: ExperimentResult,
    selection_count: int,
    excluded_count: int,
    dataset: Sequence[MarketReplay],
    split_name: str,
) -> int:
    if not dataset:
        raise ValueError("Cannot persist an experiment without eligible markets")
    params = {key: str(value) for key, value in asdict(result.params).items()}
    start = dataset[0].snapshots[0].snapshot.market_start_utc
    end = dataset[-1].snapshots[0].snapshot.market_end_utc
    with SnapshotStore(path) as store:
        run_id = store.save_strategy_run(
            {
                "strategy_name": "LateMomentumStrategy",
                "strategy_version": "1",
                "parameters_json": _json(params),
                "created_at_utc": iso(datetime.now(UTC)),
                "dataset_start_utc": iso(start),
                "dataset_end_utc": iso(end),
                "split_name": split_name,
                "quality_filters_json": _json({"selection_count": selection_count}),
                "source_snapshot_count": sum(len(item.snapshots) for item in dataset),
                "eligible_market_count": len(dataset),
                "excluded_market_count": excluded_count,
            }
        )
        for engine_result in result.results:
            if engine_result.trade is None or engine_result.signal is None:
                continue
            trade = engine_result.trade
            context = engine_result.signal
            store.save_shadow_trade(
                {
                    "strategy_run_id": run_id,
                    "market_id": trade.market_id,
                    "signal_timestamp_utc": iso(context.snapshot.timestamp_utc),
                    "side": trade.decision.value,
                    "entry_price": str(trade.entry_price),
                    "slippage": str(trade.slippage),
                    "fees": str(trade.fees),
                    "btc_delta_usd": str(context.snapshot.btc_delta_usd),
                    "btc_delta_pct": str(context.snapshot.btc_delta_pct),
                    "seconds_remaining": context.snapshot.seconds_remaining,
                    "spread": str(
                        context.snapshot.up_spread
                        if trade.decision.value == "BUY_UP"
                        else context.snapshot.down_spread
                    ),
                    "source_skew_ms": context.quality.source_skew_ms,
                    "official_outcome": engine_result.outcome,
                    "gross_pnl": str(trade.gross_pnl),
                    "net_pnl": str(trade.net_pnl),
                    "roi": str(trade.roi),
                }
            )
    return run_id


def format_leaderboard(results: Sequence[ExperimentResult], top: int) -> str:
    lines = [
        "Research split only. Preliminary observations, not profitability claims.",
        "Strategy             N    WR      AvgPx   EV/trade    NetPnL",
    ]
    for value in results[:top]:
        p, m = value.params, value.metrics
        lines.append(
            f"D{p.delta_threshold_usd} T{p.max_seconds_remaining:g} P{p.max_entry_price} "
            f"{m.trade_count:3}  {('N/A' if m.win_rate is None else f'{m.win_rate:.1%}'):>6}  "
            f"{('N/A' if m.average_entry_price is None else f'{m.average_entry_price:.3f}'):>5}  "
            f"{('N/A' if m.ev_per_trade is None else f'{m.ev_per_trade:+.4f}'):>8}  "
            f"{m.net_pnl:+.4f}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Research-only FiveCast grid experiment; holdout stays untouched"
    )
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be positive")
    selection = ReplayStore(args.db_path).load_eligible()
    research, holdout = split_chronologically(selection.replays)
    results = rank(evaluate(research, params) for params in parameter_grid())
    print(
        f"Eligible markets={len(selection.replays)} excluded={len(selection.rejected)} "
        f"research={len(research)} holdout={len(holdout)} grid=100"
    )
    print(format_leaderboard(results, args.top))
    # Persist research experiments only. Holdout evaluation requires its separate command.
    for result in results:
        persist_result(
            args.db_path,
            result,
            len(selection.replays),
            len(selection.rejected),
            research,
            "research",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
