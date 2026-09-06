from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fivecast.experiment import metrics, parameter_grid, split_chronologically
from fivecast.features import calculate_features
from fivecast.fill import ShadowTrade
from fivecast.holdout import load_research_parameters
from fivecast.replay import MarketReplay, ReplayEngine, ReplayQuality, ReplaySnapshot, ReplayStore
from fivecast.storage.migrations import iso
from fivecast.storage.sqlite import SnapshotStore
from fivecast.strategy import Decision, LateMomentumParams, LateMomentumStrategy, StrategyContext


def shifted(snapshot, seconds, price, delta, remaining):
    timestamp = snapshot.market_start_utc + timedelta(seconds=seconds)
    return snapshot.model_validate(
        {
            **snapshot.model_dump(),
            "timestamp_utc": timestamp,
            "btc_timestamp_utc": timestamp,
            "up_timestamp_utc": timestamp,
            "down_timestamp_utc": timestamp,
            "btc_price": price,
            "btc_delta_usd": delta,
            "btc_delta_pct": Decimal(delta) / snapshot.btc_window_open_price,
            "seconds_remaining": remaining,
            "btc_open_observed_at_utc": snapshot.market_start_utc,
        }
    )


def test_grid_has_exactly_100_unique_combinations():
    values = parameter_grid()
    assert len(values) == 100
    assert len(set(values)) == 100


def test_split_is_chronological_and_holdout_is_disjoint(snapshot):
    replay = MarketReplay("1", (), "UP")
    replays = tuple(
        replace(
            replay,
            market_id=str(index),
            snapshots=(
                ReplaySnapshot(
                    shifted(
                        snapshot,
                        10 + index,
                        str(snapshot.btc_window_open_price),
                        "0",
                        290 - index,
                    ),
                    None,
                    None,
                ),
            ),
        )
        for index in range(10)
    )
    research, holdout = split_chronologically(replays)
    assert [item.market_id for item in research] == [str(value) for value in range(7)]
    assert [item.market_id for item in holdout] == [str(value) for value in range(7, 10)]
    assert not set(research) & set(holdout)


def test_fill_uses_selected_ask_and_correct_pnl(snapshot):
    from fivecast.fill import PaperFill

    fill = PaperFill(Decimal("100"), Decimal("50"))
    up = fill.fill(snapshot, Decision.BUY_UP, "UP")
    down = fill.fill(snapshot, Decision.BUY_DOWN, "UP")
    assert up.entry_price == snapshot.up_ask * Decimal("1.01")
    assert up.slippage == snapshot.up_ask * Decimal("0.01")
    assert up.gross_pnl == Decimal(1) - up.entry_price
    assert up.net_pnl == up.gross_pnl - up.fees
    assert down.entry_price == snapshot.down_ask * Decimal("1.01")
    assert down.gross_pnl == -down.entry_price


def test_late_momentum_uses_delta_threshold_and_all_guards(snapshot):
    quality = type("Quality", (), {"is_stale": False, "source_skew_ms": 10})()
    context = StrategyContext(snapshot, (snapshot,), quality, None)
    strategy = LateMomentumStrategy(
        LateMomentumParams(Decimal("50"), 200, Decimal("0.8"), Decimal("0.1"), 20)
    )
    assert strategy.decide(context) is Decision.BUY_UP
    negative = snapshot.model_copy(
        update={
            "btc_price": Decimal("99900"),
            "btc_delta_usd": Decimal("-100"),
            "btc_delta_pct": Decimal("-0.001"),
        }
    )
    assert (
        strategy.decide(StrategyContext(negative, (negative,), quality, None)) is Decision.BUY_DOWN
    )
    assert (
        strategy.decide(
            StrategyContext(
                snapshot,
                (snapshot,),
                type("Q", (), {"is_stale": True, "source_skew_ms": 10})(),
                None,
            )
        )
        is Decision.NO_SIGNAL
    )


def test_metrics_drawdown_profit_factor_and_naive_edge():
    trades = (
        ShadowTrade(
            "1",
            Decision.BUY_UP,
            Decimal("0.5"),
            Decimal(0),
            Decimal(0),
            Decimal("0.5"),
            Decimal("0.5"),
            Decimal("0.5"),
            Decimal(1),
        ),
        ShadowTrade(
            "2",
            Decision.BUY_UP,
            Decimal("0.5"),
            Decimal(0),
            Decimal(0),
            Decimal("-0.5"),
            Decimal("-0.5"),
            Decimal("-0.5"),
            Decimal(-1),
        ),
    )
    result = metrics(3, trades)
    assert result.wins == result.losses == 1
    assert result.max_drawdown == Decimal("0.5")
    assert result.profit_factor == Decimal(1)
    assert result.naive_edge == Decimal(0)


def test_replay_store_quality_filters_and_rejects_corruption(tmp_path, snapshot, market):
    path = tmp_path / "data.db"
    with SnapshotStore(path) as store:
        store.save_market(market)
        store.save_resolution(
            __import__("fivecast.models", fromlist=["MarketResolution"]).MarketResolution(
                market_id=market.market_id, outcome="UP", observed_at_utc=market.end_time_utc
            )
        )
        run = store.start_run(market.start_time_utc, 5)
        store.save_snapshot(snapshot, run_id=run)
        store.finish_run(run, market.end_time_utc)
    assert len(ReplayStore(path).load_eligible(ReplayQuality(0, 3000, True)).replays) == 1
    with SnapshotStore(path) as store:
        with store.connection:
            store.connection.execute("UPDATE snapshots SET source_skew_ms=10")
    assert market.market_id in ReplayStore(path).load_eligible(ReplayQuality(0, 1, True)).rejected
    assert (
        ReplayStore(path).load_eligible(ReplayQuality(101, None, True)).rejected[market.market_id]
        == "coverage"
    )
    with SnapshotStore(path) as store:
        with store.connection:
            store.connection.execute("UPDATE snapshots SET timestamp_utc='bad'")
    assert market.market_id in ReplayStore(path).load_eligible().rejected


def test_holdout_requires_explicit_research_run(tmp_path, snapshot):
    path = tmp_path / "data.db"
    with SnapshotStore(path) as store:
        run = store.save_strategy_run(
            {
                "strategy_name": "LateMomentumStrategy",
                "strategy_version": "1",
                "parameters_json": (
                    '{"delta_threshold_usd":"40","max_entry_price":"0.7",'
                    '"max_seconds_remaining":"60","max_skew_ms":"5000","max_spread":"0.05"}'
                ),
                "created_at_utc": iso(datetime.now(UTC)),
                "dataset_start_utc": iso(snapshot.market_start_utc),
                "dataset_end_utc": iso(snapshot.market_end_utc),
                "split_name": "research",
                "quality_filters_json": "{}",
                "source_snapshot_count": 1,
                "eligible_market_count": 1,
                "excluded_market_count": 0,
            }
        )
    assert load_research_parameters(path, run).delta_threshold_usd == Decimal("40")
    with pytest.raises(ValueError, match="Unknown strategy run"):
        load_research_parameters(path, 999)


def test_historical_feature_windows_use_no_future_interpolation(snapshot):
    first = shifted(snapshot, 0, "100000", "0", 300)
    second = shifted(snapshot, 15, "100015", "15", 285)
    third = shifted(snapshot, 30, "100030", "30", 270)
    current = calculate_features((first, second, third))
    before_future = calculate_features((first, second))
    assert before_future.btc_velocity_15s == Decimal("15")
    assert current.btc_velocity_15s == Decimal("15")
    assert current.btc_velocity_30s == Decimal("30")
    assert current.btc_volatility_30s > 0
    assert current.btc_volatility_60s == current.btc_volatility_30s


def test_no_signal_has_no_shadow_fill_and_no_outcome_in_context(tmp_path):
    from fivecast.fill import PaperFill

    class NoSignal:
        def decide(self, context, params=None):
            assert not hasattr(context, "official_outcome")
            assert not hasattr(context, "outcome")
            return Decision.NO_SIGNAL

    engine = ReplayEngine()
    snapshot = __import__(
        "fivecast.models", fromlist=["MarketSnapshot"]
    ).MarketSnapshot.model_validate(
        {
            "timestamp_utc": "2026-01-01T00:00:01Z",
            "market_id": "m",
            "market_start_utc": "2026-01-01T00:00:00Z",
            "market_end_utc": "2026-01-01T00:05:00Z",
            "seconds_remaining": 299,
            "btc_price": "100",
            "btc_window_open_price": "100",
            "btc_delta_usd": "0",
            "btc_delta_pct": "0",
            "up_bid": "0.4",
            "up_ask": "0.5",
            "down_bid": "0.4",
            "down_ask": "0.5",
            "up_spread": "0.1",
            "down_spread": "0.1",
            "source_btc": "coinbase_exchange",
            "source_prediction_market": "polymarket_clob",
            "btc_timestamp_utc": "2026-01-01T00:00:01Z",
            "up_timestamp_utc": "2026-01-01T00:00:01Z",
            "down_timestamp_utc": "2026-01-01T00:00:01Z",
            "up_token_id": "1",
            "down_token_id": "2",
            "btc_open_observed_at_utc": "2026-01-01T00:00:00Z",
            "btc_open_method": "coinbase_1m_candle_open",
        }
    )
    from fivecast.models import SnapshotQuality
    from fivecast.replay import ReplaySnapshot

    replay = MarketReplay(
        "m",
        (
            ReplaySnapshot(
                snapshot,
                SnapshotQuality(
                    btc_source_timestamp=snapshot.timestamp_utc,
                    market_source_timestamp=snapshot.timestamp_utc,
                    source_skew_ms=0,
                    is_stale=False,
                ),
                calculate_features((snapshot,)),
            ),
        ),
        "UP",
    )
    result = engine.evaluate(replay, NoSignal(), fillmodel=PaperFill())
    assert result.trade is None
    assert result.outcome == "UP"
