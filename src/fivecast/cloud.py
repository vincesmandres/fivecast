"""Single-container supervisor for read-only collection and frozen M6 monitoring."""

import argparse
import logging
import os
import signal
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from fivecast.collector import collect
from fivecast.config import Settings, load_settings
from fivecast.m6 import collect_once
from fivecast.main import configure_logging

logger = logging.getLogger("fivecast.cloud")
TRANSIENT_ERRORS = (OSError, sqlite3.OperationalError, httpx.RequestError)


@dataclass(frozen=True, slots=True)
class CloudConfig:
    settings: Settings
    experiment_id: int
    m6_interval_seconds: float


def load_cloud_config() -> CloudConfig:
    settings = load_settings()
    try:
        experiment_id = int(os.environ.get("M6_EXPERIMENT_ID", "1"))
        interval = float(os.environ.get("COLLECT_INTERVAL_SECONDS", "5"))
    except ValueError as error:
        raise ValueError("M6_EXPERIMENT_ID and COLLECT_INTERVAL_SECONDS must be numeric") from error
    if experiment_id < 1 or interval <= 0:
        raise ValueError("M6_EXPERIMENT_ID and COLLECT_INTERVAL_SECONDS must be positive")
    settings = Settings.model_validate({**settings.model_dump(), "interval_seconds": interval})
    return CloudConfig(settings, experiment_id, interval)


class CloudSupervisor:
    """Coordinate the two existing loops in one process and SQLite database."""

    def __init__(
        self,
        config: CloudConfig,
        collector_task: Callable[[threading.Event], None] | None = None,
        m6_task: Callable[[threading.Event], None] | None = None,
        *,
        restart_limit: int = 3,
        restart_backoff_seconds: float = 1,
    ):
        self.config = config
        self.stop = threading.Event()
        self.collector_task = collector_task or self._collect
        self.m6_task = m6_task or self._monitor_m6
        self.restart_limit = restart_limit
        self.restart_backoff_seconds = restart_backoff_seconds
        self.error: Exception | None = None

    def _collect(self, stop: threading.Event) -> None:
        collect(self.config.settings, stop=stop)

    def _monitor_m6(self, stop: threading.Event) -> None:
        while not stop.is_set():
            collect_once(self.config.settings.db_path, self.config.experiment_id)
            stop.wait(self.config.m6_interval_seconds)

    def request_stop(self, *_: object) -> None:
        logger.info("Cloud shutdown requested; stopping collector and M6 monitor")
        self.stop.set()

    def _run_task(self, name: str, task: Callable[[threading.Event], None]) -> None:
        failures = 0
        logger.info("CLOUD TASK START task=%s", name)
        while not self.stop.is_set():
            try:
                task(self.stop)
                if not self.stop.is_set():
                    raise RuntimeError(f"Cloud task exited unexpectedly: {name}")
            except TRANSIENT_ERRORS as error:
                failures += 1
                if failures > self.restart_limit:
                    self.error = RuntimeError(
                        f"Cloud task exceeded transient restart limit: {name}"
                    )
                    self.stop.set()
                    return
                delay = self.restart_backoff_seconds * failures
                logger.warning(
                    "CLOUD TASK RETRY task=%s attempt=%s error=%s", name, failures, error
                )
                self.stop.wait(delay)
            except Exception as error:
                self.error = error
                self.stop.set()
                logger.exception("CLOUD TASK FATAL task=%s", name)
                return

    def run(self) -> None:
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for value in (signal.SIGINT, signal.SIGTERM):
                previous[value] = signal.signal(value, self.request_stop)
        try:
            logger.info(
                "FIVECAST CLOUD collector=running m6=running experiment=%s db=%s",
                self.config.experiment_id,
                self.config.settings.db_path,
            )
            threads = [
                threading.Thread(
                    target=self._run_task,
                    args=("collector", self.collector_task),
                    name="fivecast-cloud-collector",
                ),
                threading.Thread(
                    target=self._run_task, args=("m6", self.m6_task), name="fivecast-cloud-m6"
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            self.stop.set()
            for value, handler in previous.items():
                signal.signal(value, handler)
        if self.error is not None:
            raise self.error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FiveCast single-container cloud supervisor")
    parser.parse_args(argv)
    config = load_cloud_config()
    configure_logging(config.settings.log_level)
    CloudSupervisor(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
