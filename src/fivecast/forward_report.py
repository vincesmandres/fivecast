"""Read-only reporting for frozen-model forward research records."""

import argparse
import math
import sqlite3
from collections.abc import Sequence
from pathlib import Path


def report(path: Path) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT p.*, m.outcome FROM forward_predictions p "
            "JOIN markets m ON m.market_id=p.market_id "
            "ORDER BY p.timestamp_utc"
        ).fetchall()
        resolved = [row for row in rows if row["outcome"] in {"UP", "DOWN"}]
        probabilities = [float(row["predicted_up_probability"]) for row in resolved]
        baseline = [float(row["up_ask"]) for row in resolved if row["up_ask"] is not None]
        labels = [int(row["outcome"] == "UP") for row in resolved]

        def scores(values: list[float]) -> dict[str, float | None]:
            if not values or len(values) != len(labels):
                return {"brier": None, "logloss": None}
            eps = 1e-15
            return {
                "brier": sum(
                    (value - label) ** 2 for value, label in zip(values, labels, strict=True)
                )
                / len(labels),
                "logloss": -sum(
                    label * math.log(max(eps, value)) + (1 - label) * math.log(max(eps, 1 - value))
                    for value, label in zip(values, labels, strict=True)
                )
                / len(labels),
            }

        eligible = [row for row in rows if row["eligible"]]
        calibration = []
        for bucket in range(5):
            lower, upper = bucket / 10 + 0.5, bucket / 10 + 0.6
            members = [
                i
                for i, value in enumerate(probabilities)
                if lower <= value < upper or (bucket == 4 and value == 1)
            ]
            if members:
                calibration.append(
                    {
                        "bucket": f"{lower:.2f}-{upper:.2f}",
                        "count": len(members),
                        "mean_prediction": sum(probabilities[i] for i in members) / len(members),
                        "observed_up_rate": sum(labels[i] for i in members) / len(members),
                    }
                )
        return {
            "markets_observed": len({row["market_id"] for row in rows}),
            "predictions_generated": len(rows),
            "eligible_paper_signals": len(eligible),
            "resolved_predictions": len(resolved),
            "model": scores(probabilities),
            "market_baseline": scores(baseline),
            "calibration": calibration,
            "average_raw_edge": None
            if not rows
            else sum(float(row["raw_edge"] or 0) for row in rows) / len(rows),
            "average_net_edge": None
            if not rows
            else sum(float(row["net_edge"] or 0) for row in rows) / len(rows),
            "resolved_paper_signals": sum(row["outcome"] in {"UP", "DOWN"} for row in eligible),
            "realized_ev": None,
            "paper_gross_pnl": None,
            "paper_net_pnl": None,
            "max_drawdown": None,
        }
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only FiveCast frozen forward-model report")
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    args = parser.parse_args(argv)
    import json

    print(json.dumps(report(args.db_path), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
