"""Small TOML/environment configuration without secrets or endpoint overrides."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    db_path: Path = Path("data/fivecast.db")
    interval_seconds: float = Field(default=5, gt=0, le=300)
    request_timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_quote_age_seconds: float = Field(default=30, gt=0, le=300)
    max_quote_skew_seconds: float = Field(default=10, gt=0, le=60)
    retry_attempts: int = Field(default=3, ge=1, le=5)
    retry_backoff_seconds: float = Field(default=1, gt=0, le=30)
    retry_max_backoff_seconds: float = Field(default=30, gt=0, le=60)
    settlement_poll_interval_seconds: float = Field(default=30, ge=5, le=3600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


def load_settings(path: Path | None = None) -> Settings:
    values = {}
    if path is not None:
        with path.open("rb") as file:
            values = tomllib.load(file)
    for key, value in os.environ.items():
        if key.startswith("FIVECAST_"):
            values[key.removeprefix("FIVECAST_").lower()] = value
    return Settings.model_validate(values)
