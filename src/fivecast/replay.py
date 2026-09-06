"""Strict, read-only SQLite replay engine."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fivecast.features import Features, calculate_features
from fivecast.fill import PaperFill, ShadowTrade
from fivecast.models import MarketSnapshot, SnapshotQuality
from fivecast.strategy import Decision, LateMomentumParams, Strategy, StrategyContext


def _micros(value: str) -> int:
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("Stored timestamp must be timezone-aware")
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _grid_count(start: int, end: int, interval: float) -> int:
    if end <= start:
        return 0
    return max(0, (end - start - 1) // max(1, round(interval * 1_000_000)) + 1)


def _grid_intersection_count(run_start: int, lower: int, upper: int, interval: float) -> int:
    if upper <= lower:
        return 0
    interval_us = max(1, round(interval * 1_000_000))
    first_index = max(0, (lower - run_start + interval_us - 1) // interval_us)
    return _grid_count(run_start + first_index * interval_us, upper, interval)


@dataclass(frozen=True, slots=True)
class ReplayQuality:
    min_coverage_pct: float | None = None
    max_skew_ms: float | None = None
    allow_stale: bool = False


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    snapshot: MarketSnapshot
    quality: SnapshotQuality
    features: Features

    @property
    def source_skew_ms(self) -> float:
        return self.quality.source_skew_ms

    @property
    def is_stale(self) -> bool | None:
        return self.quality.is_stale


@dataclass(frozen=True, slots=True)
class MarketReplay:
    market_id: str
    snapshots: tuple[ReplaySnapshot, ...]
    _official_outcome: str

    def settle(self) -> str:
        return self._official_outcome


@dataclass(frozen=True, slots=True)
class ReplaySelection:
    replays: tuple[MarketReplay, ...]
    rejected: dict[str, str]

    @property
    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {"eligible": len(self.replays), "rejected": len(self.rejected)}
        for reason in self.rejected.values():
            result[reason] = result.get(reason, 0) + 1
        return result


class ReplayStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {2, 3}:
                raise RuntimeError(
                    f"Unsupported database schema version {version}; expected 2 or 3"
                )
            return connection
        except sqlite3.Error as exc:
            raise RuntimeError(f"Cannot open database read-only: {self.path}") from exc

    @staticmethod
    def _quality(row: sqlite3.Row) -> SnapshotQuality:
        if row["is_stale"] not in (None, 0, 1):
            raise ValueError("Invalid persisted is_stale value")
        return SnapshotQuality(
            btc_source_timestamp=row["btc_source_timestamp"],
            market_source_timestamp=row["market_source_timestamp"],
            source_skew_ms=row["source_skew_ms"],
            poll_latency_ms=row["poll_latency_ms"],
            is_stale=None if row["is_stale"] is None else bool(row["is_stale"]),
        )

    def load_eligible(self, quality: ReplayQuality | None = None) -> ReplaySelection:
        quality = quality or ReplayQuality()
        if quality.min_coverage_pct is not None and quality.min_coverage_pct < 0:
            raise ValueError("min_coverage_pct must be nonnegative")
        if quality.max_skew_ms is not None and quality.max_skew_ms < 0:
            raise ValueError("max_skew_ms must be nonnegative")
        connection = self._connect()
        rejected: dict[str, str] = {}
        selected: list[MarketReplay] = []
        try:
            runs = connection.execute("SELECT * FROM collection_runs").fetchall()
            for market in connection.execute("SELECT * FROM markets ORDER BY start_time_utc"):
                market_id = market["market_id"]
                if not market["resolved"] or market["outcome"] not in {"UP", "DOWN"}:
                    rejected[market_id] = "unresolved"
                    continue
                rows = connection.execute(
                    "SELECT * FROM snapshots WHERE market_id = ? ORDER BY timestamp_utc",
                    (market_id,),
                ).fetchall()
                if not rows:
                    rejected[market_id] = "no_snapshots"
                    continue
                expected = 0
                start = _micros(market["start_time_utc"])
                end = _micros(market["end_time_utc"])
                for run in runs:
                    expected += _grid_intersection_count(
                        _micros(run["started_at_utc"]),
                        start,
                        min(_micros(run["stopped_at_utc"] or run["heartbeat_at_utc"]), end),
                        run["interval_seconds"],
                    )
                measured = sum(row["run_id"] is not None for row in rows)
                if quality.min_coverage_pct is not None and (
                    not expected or measured * 100 / expected < quality.min_coverage_pct
                ):
                    rejected[market_id] = "coverage"
                    continue
                try:
                    replay_rows: list[ReplaySnapshot] = []
                    history: list[MarketSnapshot] = []
                    previous = None
                    for row in rows:
                        snapshot = MarketSnapshot.model_validate(
                            {key: row[key] for key in MarketSnapshot.model_fields}
                        )
                        if (
                            snapshot.market_id != market_id
                            or _micros(snapshot.market_start_utc.isoformat())
                            != _micros(market["start_time_utc"])
                            or _micros(snapshot.market_end_utc.isoformat())
                            != _micros(market["end_time_utc"])
                        ):
                            raise ValueError("Snapshot does not match persisted market identity")
                        if previous is not None and snapshot.timestamp_utc <= previous:
                            raise ValueError("Snapshots must be strictly chronological")
                        previous = snapshot.timestamp_utc
                        item_quality = self._quality(row)
                        if (
                            quality.max_skew_ms is not None
                            and item_quality.source_skew_ms > quality.max_skew_ms
                        ):
                            raise ValueError("source_skew")
                        if not quality.allow_stale and item_quality.is_stale is not False:
                            raise ValueError("stale")
                        history.append(snapshot)
                        replay_rows.append(
                            ReplaySnapshot(snapshot, item_quality, calculate_features(history))
                        )
                    selected.append(MarketReplay(market_id, tuple(replay_rows), market["outcome"]))
                except (ValueError, TypeError, KeyError) as exc:
                    rejected[market_id] = str(exc) or "corrupt"
            return ReplaySelection(tuple(selected), rejected)
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class ReplayResult:
    market_id: str
    decision: Decision
    trade: ShadowTrade | None
    outcome: str
    signal: ReplaySnapshot | None


class ReplayEngine:
    def evaluate(
        self,
        replay: MarketReplay,
        strategy: Strategy,
        params: LateMomentumParams | None = None,
        fillmodel: PaperFill | None = None,
    ) -> ReplayResult:
        history: list[MarketSnapshot] = []
        decision = Decision.NO_SIGNAL
        for current in replay.snapshots:
            history.append(current.snapshot)
            context = StrategyContext(
                current.snapshot, tuple(history), current.quality, current.features
            )
            decision = strategy.decide(context, params)
            if decision is not Decision.NO_SIGNAL:
                break
        signal = replay.snapshots[len(history) - 1] if decision is not Decision.NO_SIGNAL else None
        trade = (
            None
            if fillmodel is None or signal is None
            else fillmodel.fill(signal.snapshot, decision, replay.settle())
        )
        return ReplayResult(replay.market_id, decision, trade, replay.settle(), signal)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Offline FiveCast replay eligibility report")
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument("--min-coverage-pct", type=float)
    parser.add_argument("--max-skew-ms", type=float)
    parser.add_argument("--allow-stale", action="store_true")
    args = parser.parse_args(argv)
    selection = ReplayStore(args.db_path).load_eligible(
        ReplayQuality(args.min_coverage_pct, args.max_skew_ms, args.allow_stale)
    )
    print(f"Eligible resolved markets: {len(selection.replays)}")
    print(f"Excluded markets: {len(selection.rejected)}")
    for reason, count in sorted(selection.counts.items()):
        if reason not in {"eligible", "rejected"}:
            print(f"{reason}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
