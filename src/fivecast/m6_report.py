"""Read-only M6 prospective metrics, uncertainty and frozen status gates."""

import argparse
import json
import math
import os
import random
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path


def bootstrap(values: list[float], seed: int, replicates: int = 1000) -> tuple[float, float] | None:
    """Return a deterministic percentile CI, or N/A for fewer than two markets."""
    if len(values) < 2:
        return None
    if replicates < 1:
        raise ValueError("Bootstrap requires at least one replicate")
    rng = random.Random(seed)
    samples = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(replicates)
    )
    lower = min(replicates - 1, int(replicates * 0.025))
    upper = min(replicates - 1, max(0, int(replicates * 0.975) - 1))
    return samples[lower], samples[upper]


def _scores(values: list[float], labels: list[int]) -> dict[str, float | None]:
    if not values or len(values) != len(labels):
        return {"brier": None, "log_loss": None}
    epsilon = 1e-15
    return {
        "brier": sum((p - y) ** 2 for p, y in zip(values, labels, strict=True)) / len(labels),
        "log_loss": -sum(
            y * math.log(max(epsilon, p)) + (1 - y) * math.log(max(epsilon, 1 - p))
            for p, y in zip(values, labels, strict=True)
        )
        / len(labels),
    }


def _edge_bucket(edge: float) -> str:
    if edge <= 0:
        return "<=0"
    if edge < 0.02:
        return "0-0.02"
    if edge < 0.05:
        return "0.02-0.05"
    if edge < 0.08:
        return "0.05-0.08"
    return ">0.08"


def _max_drawdown(values: list[float]) -> float:
    balance = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        balance += value
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    return drawdown


def _trade_values(row: sqlite3.Row) -> tuple[float, float, float, bool]:
    outcome = row["outcome"]
    won = outcome == row["selected_side"]
    payout = 1.0 if won else 0.0
    entry = float(row["selected_ask"])
    costs = sum(
        float(row[name]) for name in ("estimated_fees", "estimated_slippage", "estimated_latency")
    )
    gross = payout - entry
    net = float(row["paper_pnl"]) if row["paper_pnl"] is not None else gross - costs
    return gross, net, entry + costs, won


def _paper_metrics(rows: list[sqlite3.Row]) -> dict:
    values = [_trade_values(row) for row in rows if row["paper_pnl"] is not None]
    nets = [value[1] for value in values]
    gross = [value[0] for value in values]
    costs = [value[2] for value in values]
    wins = sum(value[3] for value in values)
    losses = len(values) - wins
    losing_streak = longest = 0
    for value in values:
        if not value[3]:
            losing_streak += 1
            longest = max(longest, losing_streak)
        else:
            losing_streak = 0
    positive = sum(value for value in nets if value > 0)
    negative = abs(sum(value for value in nets if value < 0))
    return {
        "wins": wins,
        "losses": losses,
        "gross_pnl": sum(gross),
        "net_pnl": sum(nets),
        "ev_per_trade": sum(nets) / len(nets) if nets else None,
        "roi": sum(nets) / sum(costs) if costs else None,
        "max_drawdown": _max_drawdown(nets),
        "profit_factor": positive / negative if negative else None,
        "longest_losing_streak": longest,
    }


def _calibration(rows: list[sqlite3.Row]) -> list[dict]:
    result = []
    for bucket in range(10):
        lower, upper = bucket / 10, (bucket + 1) / 10
        members = [
            row
            for row in rows
            if lower <= float(row["predicted_up_probability"]) < upper
            or (bucket == 9 and float(row["predicted_up_probability"]) == 1)
        ]
        if members:
            result.append(
                {
                    "bucket": f"{lower:.1f}-{upper:.1f}",
                    "markets": len({row["market_id"] for row in members}),
                    "mean_prediction": sum(
                        float(row["predicted_up_probability"]) for row in members
                    )
                    / len(members),
                    "observed_up_rate": sum(row["outcome"] == "UP" for row in members)
                    / len(members),
                }
            )
    return result


def _share(value: float, total: float) -> float | None:
    return None if total <= 0 else value / total


def _robustness(rows: list[sqlite3.Row], settled_trades: list[sqlite3.Row]) -> dict:
    ordered = sorted(settled_trades, key=lambda row: row["timestamp_utc"])
    nets = [float(row["paper_pnl"]) for row in ordered]
    total = sum(nets)
    best = max(nets, default=0.0)
    top_five = sum(sorted(nets, reverse=True)[:5])
    by_day: dict[str, float] = defaultdict(float)
    for row in ordered:
        day = datetime.fromisoformat(row["timestamp_utc"]).date().isoformat()
        by_day[day] += float(row["paper_pnl"])
    best_day = max(by_day.values(), default=0.0)
    observed_days = {
        datetime.fromisoformat(row["timestamp_utc"]).date().isoformat() for row in rows
    }
    profitable_days = sum(value > 0 for value in by_day.values())
    losing_days = sum(value < 0 for value in by_day.values())
    flat_or_no_trade_days = len(observed_days) - profitable_days - losing_days

    def remaining(excluded: set[int]) -> float:
        return sum(
            float(row["paper_pnl"]) for index, row in enumerate(ordered) if index not in excluded
        )

    return {
        "best_trade_pnl": best,
        "top_five_trade_pnl": top_five,
        "best_day_pnl": best_day,
        "best_trade_pnl_share": _share(best, total),
        "top_five_trade_pnl_share": _share(top_five, total),
        "best_day_pnl_share": _share(best_day, total),
        "pnl_excluding_best_trade": remaining({nets.index(best)}) if nets else 0.0,
        "pnl_excluding_top_five_trades": remaining(
            set(sorted(range(len(nets)), key=lambda index: nets[index], reverse=True)[:5])
        )
        if nets
        else 0.0,
        "pnl_by_calendar_day": dict(sorted(by_day.items())),
        "profitable_days": profitable_days,
        "losing_days": losing_days,
        "flat_or_no_trade_days": flat_or_no_trade_days,
        "observed_rejected_predictions": sum(not row["eligible"] for row in rows),
    }


def _data_quality(
    connection: sqlite3.Connection, rows: list[sqlite3.Row], cutoff: str, started: str
) -> dict:
    market_ids = tuple(sorted({row["market_id"] for row in rows}))
    if market_ids:
        marks = ",".join("?" for _ in market_ids)
        snapshot_rows = connection.execute(
            "SELECT market_id, is_stale, source_skew_ms FROM snapshots "
            f"WHERE market_id IN ({marks})",
            market_ids,
        ).fetchall()
    else:
        snapshot_rows = []
    stale = sum(row["is_stale"] == 1 for row in snapshot_rows)
    known_skew = [float(row["source_skew_ms"]) for row in snapshot_rows]
    ordered_skew = sorted(known_skew)

    def percentile(percent: float) -> float | None:
        if not ordered_skew:
            return None
        return ordered_skew[round((len(ordered_skew) - 1) * percent)]

    hf = connection.execute(
        "SELECT COUNT(*) AS events FROM btc_events UNION ALL SELECT COUNT(*) FROM market_events"
    ).fetchall()
    event_times = connection.execute(
        "SELECT local_receive_timestamp FROM btc_events UNION ALL "
        "SELECT local_receive_timestamp FROM market_events ORDER BY local_receive_timestamp"
    ).fetchall()
    event_gap = [
        (
            datetime.fromisoformat(current["local_receive_timestamp"])
            - datetime.fromisoformat(previous["local_receive_timestamp"])
        ).total_seconds()
        for previous, current in zip(event_times, event_times[1:], strict=False)
    ]
    observed_markets = connection.execute(
        "SELECT COUNT(DISTINCT market_id) FROM snapshots "
        "WHERE timestamp_utc > ? AND timestamp_utc > ?",
        (cutoff, started),
    ).fetchone()[0]
    failed_polls = 0
    invalid_settlements = 0
    if market_ids:
        failed_polls = connection.execute(
            f"SELECT COUNT(*) FROM polls WHERE market_id IN ({marks}) AND status='failed'",
            market_ids,
        ).fetchone()[0]
        invalid_settlements = connection.execute(
            "SELECT COALESCE(SUM(settlement_error_count), 0) FROM markets "
            f"WHERE market_id IN ({marks})",
            market_ids,
        ).fetchone()[0]
    return {
        "expected_observable_markets": "UNAVAILABLE: public market publication is not enumerable",
        "markets_discovered": observed_markets,
        "markets_with_valid_predictions": len({row["market_id"] for row in rows}),
        "markets_rejected": len({row["market_id"] for row in rows if not row["eligible"]}),
        "forward_markets_with_snapshots": len({row["market_id"] for row in snapshot_rows}),
        "snapshot_count": len(snapshot_rows),
        "stale_snapshot_count": stale,
        "stale_rate": stale / len(snapshot_rows) if snapshot_rows else None,
        "source_skew_ms_mean": sum(known_skew) / len(known_skew) if known_skew else None,
        "source_skew_ms_median": percentile(0.5),
        "source_skew_ms_p95": percentile(0.95),
        "source_skew_ms_max": max(known_skew, default=None),
        "missing_settlements": sum(row["outcome"] not in {"UP", "DOWN"} for row in rows),
        "pending_settlements": sum(row["outcome"] not in {"UP", "DOWN"} for row in rows),
        "invalid_settlements": invalid_settlements,
        "failed_polls": failed_polls,
        "rejected_predictions": sum(not row["eligible"] for row in rows),
        "hf_event_count": sum(row["events"] for row in hf),
        "hf_disconnects": connection.execute(
            "SELECT COUNT(*) FROM hf_connections WHERE status != 'connected'"
        ).fetchone()[0],
        "hf_reconnects": connection.execute(
            "SELECT COUNT(*) FROM hf_connections WHERE reconnect_attempt > 0"
        ).fetchone()[0],
        "hf_duplicate_events": sum(
            row[0]
            for row in connection.execute(
                "SELECT COUNT(*) FROM btc_events WHERE duplicate=1 UNION ALL "
                "SELECT COUNT(*) FROM market_events WHERE duplicate=1"
            )
        ),
        "hf_out_of_order_events": sum(
            row[0]
            for row in connection.execute(
                "SELECT COUNT(*) FROM btc_events WHERE out_of_order=1 UNION ALL "
                "SELECT COUNT(*) FROM market_events WHERE out_of_order=1"
            )
        ),
        "hf_largest_event_gap_seconds": max(event_gap, default=None),
        "collection_coverage": "UNAVAILABLE: M6 monitor has no polling schedule ledger",
        "duplicate_predictions_prevented": "UNAVAILABLE: conflicts are not persisted",
        "duplicate_paper_trades_prevented": "UNAVAILABLE: conflicts are not persisted",
        "invalid_predictions": "UNAVAILABLE: rejected feature construction is not persisted",
        "missing_executable_quotes": "UNAVAILABLE: incomplete snapshots do not create predictions",
    }


def _period_metrics(rows: list[sqlite3.Row]) -> dict:
    resolved = [row for row in rows if row["outcome"] in {"UP", "DOWN"}]
    trades = [row for row in resolved if row["eligible"] and row["paper_pnl"] is not None]
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in resolved:
        grouped[row["market_id"]].append(row)
    labels = [int(values[0]["outcome"] == "UP") for values in grouped.values()]
    model = [
        sum(float(row["predicted_up_probability"]) for row in values) / len(values)
        for values in grouped.values()
    ]
    baseline_rows = [
        values for values in grouped.values() if any(row["up_ask"] is not None for row in values)
    ]
    baseline = [
        sum(float(row["up_ask"]) for row in values if row["up_ask"] is not None)
        / len([row for row in values if row["up_ask"] is not None])
        for values in baseline_rows
    ]
    baseline_labels = [int(values[0]["outcome"] == "UP") for values in baseline_rows]
    fivecast = _scores(model, labels)
    market = _scores(baseline, baseline_labels)
    net = [float(row["paper_pnl"]) for row in trades]
    return {
        "resolved_markets": len(grouped),
        "eligible_trades": len(trades),
        "wins": sum(row["outcome"] == row["selected_side"] for row in trades),
        "losses": sum(row["outcome"] != row["selected_side"] for row in trades),
        "average_entry": sum(float(row["selected_ask"]) for row in trades) / len(trades)
        if trades
        else None,
        "average_predicted_net_edge": sum(float(row["net_edge"]) for row in trades) / len(trades)
        if trades
        else None,
        "gross_pnl": sum(_trade_values(row)[0] for row in trades),
        "net_pnl": sum(net),
        "ev_per_trade": sum(net) / len(net) if net else None,
        "fivecast_brier": fivecast["brier"],
        "market_brier": market["brier"],
        "brier_difference": None
        if fivecast["brier"] is None or market["brier"] is None
        else fivecast["brier"] - market["brier"],
    }


def _daily_metrics(rows: list[sqlite3.Row]) -> dict[str, dict]:
    by_day: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_day[datetime.fromisoformat(row["timestamp_utc"]).date().isoformat()].append(row)
    return {day: _period_metrics(values) for day, values in sorted(by_day.items())}


def _time_blocks(rows: list[sqlite3.Row]) -> dict[str, dict]:
    blocks = {"00:00-05:59": [], "06:00-11:59": [], "12:00-17:59": [], "18:00-23:59": []}
    for row in rows:
        hour = datetime.fromisoformat(row["timestamp_utc"]).hour
        blocks[f"{hour // 6 * 6:02d}:00-{hour // 6 * 6 + 5:02d}:59"].append(row)
    return {
        block: {**_period_metrics(values), "low_n": len({row["market_id"] for row in values}) < 5}
        for block, values in blocks.items()
    }


def report(path: Path, experiment: int, detailed: bool = False) -> dict:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        manifest = connection.execute(
            "SELECT * FROM m6_experiments WHERE id=?", (experiment,)
        ).fetchone()
        if manifest is None:
            raise ValueError("Unknown M6 experiment")
        policy = json.loads(manifest["manifest_json"])
        rows = connection.execute(
            "SELECT p.*, m.outcome FROM m6_prediction_links l "
            "JOIN forward_predictions p ON p.id=l.prediction_id "
            "JOIN markets m ON m.market_id=p.market_id "
            "WHERE l.experiment_id=? ORDER BY p.timestamp_utc",
            (experiment,),
        ).fetchall()
        resolved = [row for row in rows if row["outcome"] in {"UP", "DOWN"}]
        eligible = [row for row in rows if row["eligible"]]
        settled_trades = [row for row in eligible if row["paper_pnl"] is not None]
        resolved_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in resolved:
            resolved_groups[row["market_id"]].append(row)
        model_values = [
            sum(float(row["predicted_up_probability"]) for row in values) / len(values)
            for values in resolved_groups.values()
        ]
        market_values = [
            sum(float(row["up_ask"]) for row in values if row["up_ask"] is not None)
            / len([row for row in values if row["up_ask"] is not None])
            for values in resolved_groups.values()
            if any(row["up_ask"] is not None for row in values)
        ]
        labels = [int(values[0]["outcome"] == "UP") for values in resolved_groups.values()]
        model_scores = _scores(model_values, labels)
        market_scores = _scores(
            market_values,
            [
                int(values[0]["outcome"] == "UP")
                for values in resolved_groups.values()
                if any(row["up_ask"] is not None for row in values)
            ],
        )

        market_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in settled_trades:
            market_groups[row["market_id"]].append(row)
        market_net = [
            sum(float(row["paper_pnl"]) for row in values) for values in market_groups.values()
        ]
        market_win_rate = [
            float(any(row["outcome"] == row["selected_side"] for row in values))
            for values in market_groups.values()
        ]
        market_ev = [
            sum(float(row["paper_pnl"]) for row in values) / len(values)
            for values in market_groups.values()
        ]
        market_brier_difference = []
        for values in resolved_groups.values():
            label = int(values[0]["outcome"] == "UP")
            market_values_for_group = [row for row in values if row["up_ask"] is not None]
            if not market_values_for_group:
                continue
            market_brier_difference.append(
                sum((float(row["predicted_up_probability"]) - label) ** 2 for row in values)
                / len(values)
                - sum((float(row["up_ask"]) - label) ** 2 for row in market_values_for_group)
                / len(market_values_for_group)
            )

        edge_metrics = {}
        for bucket in policy["edge_buckets"]:
            bucket_rows = [
                row for row in resolved if _edge_bucket(float(row["net_edge"])) == bucket
            ]
            bucket_trades = [
                row for row in bucket_rows if row["eligible"] and row["paper_pnl"] is not None
            ]
            net_values = [float(row["paper_pnl"]) for row in bucket_trades]
            edge_metrics[bucket] = {
                "independent_markets": len({row["market_id"] for row in bucket_rows}),
                "eligible_trades": len(bucket_trades),
                "mean_predicted_edge": sum(float(row["net_edge"]) for row in bucket_rows)
                / len(bucket_rows)
                if bucket_rows
                else None,
                "realized_ev": sum(net_values) / len(net_values) if net_values else None,
                "win_rate": sum(row["outcome"] == row["selected_side"] for row in bucket_trades)
                / len(bucket_trades)
                if bucket_trades
                else None,
                "average_entry": sum(float(row["selected_ask"]) for row in bucket_trades)
                / len(bucket_trades)
                if bucket_trades
                else None,
            }

        paper = _paper_metrics(settled_trades)
        resolved_markets = len({row["market_id"] for row in resolved})
        minimums_met = (
            resolved_markets >= policy["minimum_resolved_markets"]
            and len(eligible) >= policy["minimum_eligible_records"]
        )
        kills = []
        if minimums_met:
            if (
                model_scores["brier"] is None
                or market_scores["brier"] is None
                or model_scores["brier"] - market_scores["brier"] > 0.01
            ):
                kills.append("BRIER_WORSE_THAN_MARKET")
            if (
                model_scores["log_loss"] is None
                or market_scores["log_loss"] is None
                or model_scores["log_loss"] - market_scores["log_loss"] > 0.01
            ):
                kills.append("LOG_LOSS_WORSE_THAN_MARKET")
            if paper["ev_per_trade"] is None or paper["ev_per_trade"] <= 0:
                kills.append("NON_POSITIVE_NET_EV")
            if paper["max_drawdown"] > 10:
                kills.append("DRAWDOWN_TOO_HIGH")
            if len(market_groups) < 5:
                kills.append("PNL_CONCENTRATED_IN_FEWER_THAN_FIVE_MARKETS")
        status = "INCONCLUSIVE" if not minimums_met else ("KILL" if kills else "CONTINUE")

        result = {
            "experiment_id": manifest["experiment_id"],
            "manifest_hash": manifest["manifest_hash"],
            "experiment_start_utc": manifest["experiment_start_utc"],
            "forward_runtime_seconds": (
                datetime.now(UTC) - datetime.fromisoformat(manifest["experiment_start_utc"])
            ).total_seconds(),
            "frozen_artifact": {
                "model_version": policy["model_version"],
                "model_fingerprint": policy["model_fingerprint"],
                "dataset_cutoff_utc": policy["dataset_cutoff_utc"],
                "dataset_fingerprint": policy["dataset_fingerprint"],
                "configuration_fingerprint": policy["configuration_fingerprint"],
            },
            "forward_predictions": len(rows),
            "resolved_markets": resolved_markets,
            "minimum_resolved_markets": policy["minimum_resolved_markets"],
            "eligible_trades": len(eligible),
            "resolved_eligible_trades": len(settled_trades),
            "minimum_eligible_trades": policy["minimum_eligible_records"],
            "scores": {"fivecast": model_scores, "market_baseline": market_scores},
            "calibration": _calibration(resolved),
            "edge_buckets": edge_metrics,
            "paper": paper,
            "robustness": _robustness(rows, settled_trades),
            "data_quality": _data_quality(
                connection,
                rows,
                policy["dataset_cutoff_utc"],
                manifest["experiment_start_utc"],
            ),
            "status": status,
            "kill_reasons": kills,
            "outstanding_requirements": {
                "resolved_markets": max(0, policy["minimum_resolved_markets"] - resolved_markets),
                "eligible_trades": max(0, policy["minimum_eligible_records"] - len(eligible)),
            },
            "net_pnl_ci_95": bootstrap(
                market_net, policy["bootstrap_seed"], policy["bootstrap_replicates"]
            ),
            "win_rate_ci_95": bootstrap(
                market_win_rate, policy["bootstrap_seed"], policy["bootstrap_replicates"]
            ),
            "ev_per_trade_ci_95": bootstrap(
                market_ev, policy["bootstrap_seed"], policy["bootstrap_replicates"]
            ),
            "brier_difference_ci_95": bootstrap(
                market_brier_difference, policy["bootstrap_seed"], policy["bootstrap_replicates"]
            ),
        }
        if detailed:
            result["daily_performance"] = _daily_metrics(rows)
            result["calendar_days"] = result["daily_performance"]
            result["time_of_day_blocks_utc"] = _time_blocks(rows)
        return result
    finally:
        connection.close()


def _display(value: object) -> str:
    if value is None:
        return "UNAVAILABLE"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def format_report(value: dict, detailed: bool = False) -> str:
    """Format an operational read-only view without changing the JSON API."""
    artifact = value["frozen_artifact"]
    quality = value["data_quality"]
    paper = value["paper"]
    lines = [
        "FIVECAST M6",
        "================================",
        "",
        "EXPERIMENT",
        value["experiment_id"],
        "",
        "FROZEN ARTIFACT",
        f"Model                 {artifact['model_version']}",
        f"Manifest              {value['manifest_hash']}",
        f"Model fingerprint     {artifact['model_fingerprint']}",
        f"Started               {value['experiment_start_utc']}",
        f"Runtime seconds       {_display(value['forward_runtime_seconds'])}",
        "",
        "SAMPLE PROGRESS",
        f"Resolved markets      {value['resolved_markets']} / {value['minimum_resolved_markets']}",
        f"Eligible trades       {value['eligible_trades']} / {value['minimum_eligible_trades']}",
        "",
        "PREDICTION QUALITY",
        f"FiveCast Brier        {_display(value['scores']['fivecast']['brier'])}",
        f"Market Brier          {_display(value['scores']['market_baseline']['brier'])}",
        f"Net EV/trade          {_display(paper['ev_per_trade'])}",
        f"95% PnL CI            {_display(value['net_pnl_ci_95'])}",
        "",
        "DATA QUALITY",
        f"Snapshots used        {quality['snapshot_count']}",
        f"Stale rate            {_display(quality['stale_rate'])}",
        f"Mean skew ms          {_display(quality['source_skew_ms_mean'])}",
        f"Pending settlements   {quality['missing_settlements']}",
        "",
        "STATUS",
        value["status"],
        "Reasons               "
        + (
            ", ".join(value["kill_reasons"])
            if value["kill_reasons"]
            else "minimum prospective evidence not reached"
            if value["status"] == "INCONCLUSIVE"
            else "all pre-registered criteria met"
        ),
    ]
    if detailed:
        lines.extend(
            ["", "DAILY PERFORMANCE", json.dumps(value["daily_performance"], sort_keys=True)]
        )
        lines.extend(
            [
                "",
                "TIME-OF-DAY DIAGNOSTICS",
                json.dumps(value["time_of_day_blocks_utc"], sort_keys=True),
            ]
        )
        lines.extend(
            ["", "ROBUSTNESS / CONCENTRATION", json.dumps(value["robustness"], sort_keys=True)]
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only M6 forward validation report")
    parser.add_argument("--experiment", required=True, type=int)
    parser.add_argument(
        "--db-path", type=Path, default=Path(os.environ.get("FIVECAST_DB_PATH", "data/fivecast.db"))
    )
    parser.add_argument("--detailed", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(report(args.db_path, args.experiment, args.detailed), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
