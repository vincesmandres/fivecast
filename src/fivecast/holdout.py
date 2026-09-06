"""Explicit holdout evaluation for one persisted research configuration."""

import argparse
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from fivecast.experiment import evaluate, format_leaderboard, persist_result, split_chronologically
from fivecast.replay import ReplayStore
from fivecast.storage.sqlite import SnapshotStore
from fivecast.strategy import LateMomentumParams


def load_research_parameters(path: Path, run_id: int) -> LateMomentumParams:
    with SnapshotStore(path) as store:
        record = store.get_research_run(run_id)
    if record is None:
        raise ValueError(f"Unknown strategy run {run_id}")
    if record["split_name"] != "research" or record["strategy_name"] != "LateMomentumStrategy":
        raise ValueError(
            "Only persisted LateMomentumStrategy research runs can be evaluated on holdout"
        )
    values = json.loads(record["parameters_json"])
    return LateMomentumParams(
        delta_threshold_usd=Decimal(values["delta_threshold_usd"]),
        max_seconds_remaining=float(values["max_seconds_remaining"]),
        max_entry_price=Decimal(values["max_entry_price"]),
        max_spread=Decimal(values["max_spread"]),
        max_skew_ms=float(values["max_skew_ms"]),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit holdout-only FiveCast shadow evaluation")
    parser.add_argument("--strategy-run", required=True, type=int)
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    args = parser.parse_args(argv)
    params = load_research_parameters(args.db_path, args.strategy_run)
    selection = ReplayStore(args.db_path).load_eligible()
    _research, holdout = split_chronologically(selection.replays)
    result = evaluate(holdout, params)
    print("EXPLICIT HOLDOUT EVALUATION. Never used for research-grid ranking.")
    print(format_leaderboard([result], 1))
    persisted = persist_result(
        args.db_path, result, len(selection.replays), len(selection.rejected), holdout, "holdout"
    )
    print(f"Stored holdout shadow result as strategy_run={persisted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
