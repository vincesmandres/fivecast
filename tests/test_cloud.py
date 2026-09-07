import pytest

from fivecast.cloud import CloudConfig, CloudSupervisor, load_cloud_config
from fivecast.config import Settings
from fivecast.m6 import _default_db_path
from fivecast.storage.sqlite import SnapshotStore


def _config(tmp_path) -> CloudConfig:
    return CloudConfig(Settings(db_path=tmp_path / "shared.db"), 1, 0.001)


def test_cloud_supervisor_starts_collector_and_m6_on_same_stop_signal(tmp_path):
    calls = []

    def collector(stop):
        calls.append(("collector", stop))
        stop.wait()

    def monitor(stop):
        calls.append(("m6", stop))
        stop.set()

    supervisor = CloudSupervisor(_config(tmp_path), collector, monitor)
    supervisor.run()
    assert {name for name, _ in calls} == {"collector", "m6"}
    assert len({id(stop) for _, stop in calls}) == 1


def test_cloud_supervisor_retries_only_transient_task_failure(tmp_path):
    attempts = 0

    def collector(stop):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary")
        stop.set()

    def monitor(stop):
        stop.wait()

    supervisor = CloudSupervisor(
        _config(tmp_path), collector, monitor, restart_limit=1, restart_backoff_seconds=0
    )
    supervisor.run()
    assert attempts == 2


def test_cloud_supervisor_propagates_fatal_invariant_error(tmp_path):
    def fatal(_):
        raise ValueError("frozen manifest mismatch")

    supervisor = CloudSupervisor(_config(tmp_path), fatal, lambda stop: stop.wait())
    with pytest.raises(ValueError, match="manifest mismatch"):
        supervisor.run()


def test_cloud_supervisor_request_stop_is_graceful(tmp_path):
    supervisor = CloudSupervisor(
        _config(tmp_path), lambda stop: stop.wait(), lambda stop: stop.wait()
    )
    supervisor.request_stop()
    supervisor.run()
    assert supervisor.stop.is_set()


def test_cloud_environment_uses_one_configured_database_and_experiment(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "fivecast.db"
    monkeypatch.setenv("FIVECAST_DB_PATH", str(path))
    monkeypatch.setenv("M6_EXPERIMENT_ID", "7")
    monkeypatch.setenv("COLLECT_INTERVAL_SECONDS", "12")
    config = load_cloud_config()
    assert config.settings.db_path == path
    assert config.experiment_id == 7
    assert config.settings.interval_seconds == config.m6_interval_seconds == 12
    assert _default_db_path() == path


def test_cloud_rejects_invalid_environment_values(monkeypatch):
    monkeypatch.setenv("M6_EXPERIMENT_ID", "0")
    with pytest.raises(ValueError, match="positive"):
        load_cloud_config()


def test_snapshot_store_enables_wal_and_busy_timeout(tmp_path):
    with SnapshotStore(tmp_path / "shared.db") as store:
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
