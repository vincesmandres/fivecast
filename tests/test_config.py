from pathlib import Path

import pytest
from pydantic import ValidationError

from fivecast.config import Settings, load_settings


def test_defaults():
    settings = load_settings()
    assert settings.interval_seconds == 5
    assert settings.db_path == Path("data/fivecast.db")


def test_example_toml_loads():
    settings = load_settings(Path(__file__).parents[1] / "config/settings.example.toml")
    assert settings == Settings()


def test_environment_overrides_toml(monkeypatch):
    monkeypatch.setenv("FIVECAST_INTERVAL_SECONDS", "7.5")
    monkeypatch.setenv("FIVECAST_DB_PATH", "data/other.db")
    settings = load_settings(Path(__file__).parents[1] / "config/settings.example.toml")
    assert settings.interval_seconds == 7.5
    assert settings.db_path == Path("data/other.db")


@pytest.mark.parametrize("interval", ["0", "-5", "NaN", "Infinity", "not-a-number"])
def test_invalid_environment_rejected(monkeypatch, interval):
    monkeypatch.setenv("FIVECAST_INTERVAL_SECONDS", interval)
    with pytest.raises(ValidationError):
        load_settings()


def test_unknown_environment_setting_rejected(monkeypatch):
    monkeypatch.setenv("FIVECAST_INTERVL_SECONDS", "5")
    with pytest.raises(ValidationError, match="Extra inputs"):
        load_settings()


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "absent.toml")
