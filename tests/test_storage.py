from datetime import timedelta
from decimal import Decimal

import pytest

from fivecast.models import MarketSnapshot
from fivecast.storage.sqlite import SnapshotStore


def test_insert_roundtrip_and_empty_database(tmp_path, snapshot):
    path = tmp_path / "nested" / "research.db"
    with SnapshotStore(path) as store:
        assert store.count_snapshots() == 0
        assert store.get_latest_snapshot() is None
        assert store.save_snapshot(snapshot) is True
        assert store.count_snapshots() == 1
        assert store.get_latest_snapshot() == snapshot
        assert isinstance(store.get_latest_snapshot().btc_price, Decimal)
    with SnapshotStore(path) as reopened:
        assert reopened.get_latest_snapshot() == snapshot


def test_identical_insert_is_idempotent(tmp_path, snapshot):
    with SnapshotStore(tmp_path / "research.db") as store:
        assert store.save_snapshot(snapshot) is True
        assert store.save_snapshot(snapshot) is False
        assert store.count_snapshots() == 1


def test_conflicting_duplicate_is_not_silently_overwritten(tmp_path, snapshot):
    different = MarketSnapshot.model_validate(
        {**snapshot.model_dump(), "up_bid": "0.59", "up_spread": "0.03"}
    )
    with SnapshotStore(tmp_path / "research.db") as store:
        store.save_snapshot(snapshot)
        with pytest.raises(ValueError, match="Conflicting snapshot"):
            store.save_snapshot(different)
        assert store.get_latest_snapshot() == snapshot
        assert store.count_snapshots() == 1


def test_latest_uses_timestamp_not_insertion_order(tmp_path, snapshot):
    later = MarketSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "timestamp_utc": snapshot.timestamp_utc + timedelta(microseconds=1),
            "seconds_remaining": 179.999999,
        }
    )
    with SnapshotStore(tmp_path / "research.db") as store:
        store.save_snapshot(later)
        store.save_snapshot(snapshot)
        assert store.get_latest_snapshot() == later
        assert store.count_snapshots() == 2


def test_null_quotes_roundtrip(tmp_path, snapshot):
    missing = MarketSnapshot.model_validate(
        {**snapshot.model_dump(), "up_bid": None, "up_spread": None}
    )
    with SnapshotStore(tmp_path / "research.db") as store:
        store.save_snapshot(missing)
        assert store.get_latest_snapshot() == missing


def test_parameterized_market_identifier(tmp_path, snapshot):
    unusual = MarketSnapshot.model_validate(
        {**snapshot.model_dump(), "market_id": "x');DROP/**/TABLE/**/snapshots;--"}
    )
    with SnapshotStore(tmp_path / "research.db") as store:
        store.save_snapshot(unusual)
        assert store.count_snapshots() == 1
        assert store.get_latest_snapshot() == unusual
