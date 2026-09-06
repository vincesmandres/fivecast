import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fivecast.fill import PaperFill
from fivecast.models import MarketSnapshot
from fivecast.replay import ReplayEngine, ReplayQuality, ReplayStore
from fivecast.strategy import Decision, LateMomentumParams, StrategyContext


def _snapshot(index: int, price: str = "100") -> dict:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    timestamp = start + timedelta(seconds=index * 15)
    end = start + timedelta(seconds=300)
    value = Decimal(price)
    return {
        "timestamp_utc": timestamp,
        "market_id": "m1",
        "market_start_utc": start,
        "market_end_utc": end,
        "seconds_remaining": (end - timestamp).total_seconds(),
        "btc_price": value,
        "btc_window_open_price": Decimal("100"),
        "btc_delta_usd": value - Decimal("100"),
        "btc_delta_pct": (value - Decimal("100")) / 100,
        "up_bid": Decimal("0.40"),
        "up_ask": Decimal("0.50"),
        "down_bid": Decimal("0.40"),
        "down_ask": Decimal("0.50"),
        "up_spread": Decimal("0.10"),
        "down_spread": Decimal("0.10"),
        "source_btc": "coinbase_exchange",
        "source_prediction_market": "polymarket_clob",
        "btc_timestamp_utc": timestamp,
        "up_timestamp_utc": timestamp,
        "down_timestamp_utc": timestamp,
        "up_token_id": "1",
        "down_token_id": "2",
        "btc_open_observed_at_utc": start,
        "btc_open_method": "coinbase_1m_candle_open",
    }


def _database(path, outcome="UP", resolved=1, prices=("100", "100", "102")):
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA user_version=2;
        CREATE TABLE markets (market_id TEXT PRIMARY KEY, slug TEXT, condition_id TEXT,
          question TEXT, start_time_utc TEXT, end_time_utc TEXT, up_token_id TEXT,
          down_token_id TEXT, closed INTEGER, resolved INTEGER, outcome TEXT,
          resolution_time_utc TEXT, resolution_time_source TEXT, resolution_observed_at_utc TEXT,
          resolution_source TEXT, metadata_source TEXT, settlement_checked_at_utc TEXT,
          next_settlement_check_utc TEXT, settlement_error_count INTEGER, created_at_utc TEXT,
          updated_at_utc TEXT);
        CREATE TABLE collection_runs (id INTEGER PRIMARY KEY, started_at_utc TEXT,
          heartbeat_at_utc TEXT, stopped_at_utc TEXT, interval_seconds REAL);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY, timestamp_utc TEXT, market_id TEXT,
          market_start_utc TEXT, market_end_utc TEXT, seconds_remaining REAL, btc_price TEXT,
          btc_window_open_price TEXT, btc_delta_usd TEXT, btc_delta_pct TEXT, up_bid TEXT,
          up_ask TEXT, down_bid TEXT, down_ask TEXT, up_spread TEXT, down_spread TEXT,
          source_btc TEXT, source_prediction_market TEXT, btc_timestamp_utc TEXT,
          up_timestamp_utc TEXT, down_timestamp_utc TEXT, up_token_id TEXT, down_token_id TEXT,
          btc_open_observed_at_utc TEXT, btc_open_method TEXT, created_at_utc TEXT,
          btc_source_timestamp TEXT, market_source_timestamp TEXT, source_skew_ms REAL,
          poll_latency_ms REAL, is_stale INTEGER, run_id INTEGER);
    """)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=300)
    connection.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "m1",
            "slug",
            None,
            None,
            start.isoformat(),
            end.isoformat(),
            "1",
            "2",
            1,
            resolved,
            outcome if resolved else None,
            None,
            None,
            None,
            None,
            "test",
            None,
            None,
            0,
            start.isoformat(),
            start.isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO collection_runs VALUES (1,?,?,?,15)",
        (
            start.isoformat(),
            (start + timedelta(seconds=45)).isoformat(),
            (start + timedelta(seconds=45)).isoformat(),
        ),
    )
    for index, price in enumerate(prices):
        model = MarketSnapshot.model_validate(_snapshot(index, price))
        values = model.model_dump(mode="json")
        columns = list(values) + [
            "created_at_utc",
            "btc_source_timestamp",
            "market_source_timestamp",
            "source_skew_ms",
            "poll_latency_ms",
            "is_stale",
            "run_id",
        ]
        row = [values[column] for column in values] + [
            start.isoformat(),
            model.timestamp_utc.isoformat(),
            model.timestamp_utc.isoformat(),
            0.0,
            0.0,
            0,
            1,
        ]
        connection.execute(
            f"INSERT INTO snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in row)})",
            row,
        )
    connection.commit()
    connection.close()


def test_replay_is_chronological_and_features_have_no_future(tmp_path):
    path = tmp_path / "replay.db"
    _database(path)
    selection = ReplayStore(path).load_eligible()
    replay = selection.replays[0]
    timestamps = [item.snapshot.timestamp_utc for item in replay.snapshots]
    assert timestamps == sorted(timestamps)
    assert replay.snapshots[1].features.velocity_15s == Decimal("0")
    assert replay.snapshots[2].features.velocity_15s == Decimal("2")


def test_quality_filters_unresolved_and_coverage(tmp_path):
    path = tmp_path / "replay.db"
    _database(path, resolved=0)
    selection = ReplayStore(path).load_eligible(ReplayQuality(min_coverage_pct=101))
    assert not selection.replays
    assert selection.counts["unresolved"] == 1


class SpyStrategy:
    def __init__(self):
        self.contexts = []

    def decide(self, context: StrategyContext, params=None):
        self.contexts.append(context)
        assert not hasattr(context, "outcome")
        return Decision.BUY_UP


def test_engine_stops_after_one_signal_and_settles_afterward(tmp_path):
    path = tmp_path / "replay.db"
    _database(path)
    replay = ReplayStore(path).load_eligible().replays[0]
    strategy = SpyStrategy()
    result = ReplayEngine().evaluate(replay, strategy)
    assert result.decision is Decision.BUY_UP
    assert len(strategy.contexts) == 1
    assert result.outcome == "UP"


def test_paper_fill_win_loss_fee_and_slippage(tmp_path):
    path = tmp_path / "replay.db"
    _database(path)
    snapshot = ReplayStore(path).load_eligible().replays[0].snapshots[0].snapshot
    trade = PaperFill(Decimal("1"), Decimal("100")).fill(snapshot, Decision.BUY_UP, "UP")
    loss = PaperFill(Decimal("0"), Decimal("100")).fill(snapshot, Decision.BUY_UP, "DOWN")
    assert trade and trade.entry_price == Decimal("0.50005")
    assert trade.net_pnl == Decimal("0.4949495")
    assert loss and loss.gross_payout == 0


def test_strategy_directions_and_none():
    assert LateMomentumParams(Decimal("0"), Decimal("0.1"), 0).max_spread == Decimal("0.05")
