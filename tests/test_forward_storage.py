import sqlite3

import pytest

from fivecast.storage.sqlite import SnapshotStore


def model_record() -> dict:
    return {
        "version": "m5.1",
        "artifact_json": '{"kind":"classifier"}',
        "fingerprint": "model-fp-1",
        "dataset_fingerprint": "dataset-fp-1",
        "training_cutoff_utc": "2026-09-05T00:00:00.000000+00:00",
        "created_at_utc": "2026-09-06T15:00:00.000000+00:00",
    }


def prediction_record(model_version_id: int, market_id: str) -> dict:
    return {
        "model_version_id": model_version_id,
        "market_id": market_id,
        "timestamp_utc": "2026-09-06T15:01:00.000000+00:00",
        "features_json": '{"btc_delta":"1.2"}',
        "features_fingerprint": "features-fp-1",
        "predicted_up_probability": "0.61",
        "up_ask": "0.58",
        "down_ask": "0.44",
        "selected_side": "UP",
        "selected_ask": "0.58",
        "raw_edge": "0.03",
        "estimated_fees": "opaque-fee",
        "estimated_slippage": "opaque-slip",
        "estimated_latency": "opaque-latency",
        "net_edge": "0.01",
        "eligible": 1,
        "rejection_reason": None,
        "official_outcome": None,
        "paper_pnl": None,
        "created_at_utc": "2026-09-06T15:01:01.000000+00:00",
    }


def trade_record(prediction_id: int, model_version_id: int, market_id: str) -> dict:
    return {
        "prediction_id": prediction_id,
        "model_version_id": model_version_id,
        "market_id": market_id,
        "timestamp_utc": "2026-09-06T15:01:00.000000+00:00",
        "side": "UP",
        "entry_price": "opaque-entry",
        "gross_pnl": None,
        "estimated_fees": "opaque-fee",
        "estimated_slippage": "opaque-slip",
        "estimated_latency": "opaque-latency",
        "net_pnl": None,
        "official_outcome": None,
        "created_at_utc": "2026-09-06T15:01:02.000000+00:00",
    }


def test_v4_upgrade_is_additive_and_preserves_prior_schema_and_rows(tmp_path, snapshot):
    path = tmp_path / "forward.db"
    with SnapshotStore(path) as store:
        store.save_snapshot(snapshot)
        before = {
            row["name"]: (
                row["sql"],
                tuple(
                    store.connection.execute(
                        f"SELECT * FROM {row['name']} ORDER BY rowid"
                    ).fetchone()
                    or ()
                ),
            )
            for row in store.connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT IN "
                "('model_versions', 'forward_predictions', 'forward_paper_trades')"
            )
        }
        store.connection.execute("DROP TABLE forward_paper_trades")
        store.connection.execute("DROP TABLE forward_predictions")
        store.connection.execute("DROP TABLE model_versions")
        store.connection.execute("PRAGMA user_version=4")

    with SnapshotStore(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        after = {
            row["name"]: (
                row["sql"],
                tuple(
                    store.connection.execute(
                        f"SELECT * FROM {row['name']} ORDER BY rowid"
                    ).fetchone()
                    or ()
                ),
            )
            for row in store.connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT IN "
                "('model_versions', 'forward_predictions', 'forward_paper_trades')"
            )
        }
        assert after == before
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_forward_foreign_keys_and_uniqueness(tmp_path, market):
    with SnapshotStore(tmp_path / "forward.db") as store:
        store.save_market(market)
        model_id = store.persist_model_version(model_record())
        prediction = prediction_record(model_id, market.market_id)
        prediction_id = store.save_forward_prediction(prediction)
        assert store.save_forward_prediction(prediction) == prediction_id
        trade = trade_record(prediction_id, model_id, market.market_id)
        assert store.save_forward_paper_trade(trade) == trade["prediction_id"]
        assert store.save_forward_paper_trade(trade) == trade["prediction_id"]
        assert store.get_model_version("m5.1")["id"] == model_id
        with pytest.raises(sqlite3.IntegrityError):
            store.save_forward_prediction({**prediction, "market_id": "unknown"})
        with pytest.raises(sqlite3.IntegrityError):
            store.persist_model_version(
                {**model_record(), "version": "m5.2", "fingerprint": "model-fp-1"}
            )


@pytest.mark.parametrize("outcome", ["UP", "DOWN"])
def test_attach_forward_settlement_only_updates_forward_tables(tmp_path, market, outcome):
    with SnapshotStore(tmp_path / "forward.db") as store:
        store.save_market(market)
        model_id = store.persist_model_version(model_record())
        prediction_id = store.save_forward_prediction(prediction_record(model_id, market.market_id))
        store.save_forward_paper_trade(trade_record(prediction_id, model_id, market.market_id))
        market_before = store.get_market(market.market_id)

        assert (
            store.attach_forward_settlement(
                {"market_id": market.market_id, "outcome": outcome, "paper_pnl": "opaque-pnl"}
            )
            == 1
        )
        prediction = dict(
            store.connection.execute(
                "SELECT official_outcome, paper_pnl FROM forward_predictions WHERE id=?",
                (prediction_id,),
            ).fetchone()
        )
        trade = dict(
            store.connection.execute(
                "SELECT official_outcome, net_pnl FROM forward_paper_trades WHERE prediction_id=?",
                (prediction_id,),
            ).fetchone()
        )
        assert prediction == {"official_outcome": outcome, "paper_pnl": "opaque-pnl"}
        assert trade["official_outcome"] == outcome
        assert trade["net_pnl"] == "opaque-pnl"
        assert store.get_market(market.market_id) == market_before
        assert (
            store.attach_forward_settlement(
                market.market_id, "DOWN" if outcome == "UP" else "UP", "bad"
            )
            == 0
        )
        unchanged = store.connection.execute(
            "SELECT official_outcome, paper_pnl FROM forward_predictions WHERE id=?",
            (prediction_id,),
        ).fetchone()
        assert tuple(unchanged) == (outcome, "opaque-pnl")
