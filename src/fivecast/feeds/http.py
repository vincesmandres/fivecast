"""Bounded, read-only HTTP retries for long-running collectors."""

import logging
import threading
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from fivecast.config import Settings

logger = logging.getLogger(__name__)
READ_ONLY_USER_AGENT = "fivecast-readonly/0.1"


class CollectorStopped(RuntimeError):
    """The collector was stopped while waiting for an HTTP retry."""


class RetryingClient(httpx.Client):
    """Synchronous GET-only client with bounded, cancellable transient retries."""

    def __init__(
        self,
        settings: Settings,
        stop_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> None:
        headers = httpx.Headers(kwargs.pop("headers", None))
        if "User-Agent" not in headers:
            headers["User-Agent"] = READ_ONLY_USER_AGENT
        super().__init__(
            headers=headers,
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            **kwargs,
        )
        self._settings = settings
        self._stop_event = stop_event if stop_event is not None else threading.Event()

    def get(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Perform a GET, retrying only transport errors and transient statuses."""
        for attempt in range(self._settings.retry_attempts):
            self._raise_if_stopped()
            try:
                response = super().get(url, **kwargs)
            except httpx.RequestError as error:
                if attempt == self._settings.retry_attempts - 1:
                    logger.warning("HTTP GET failed after %d attempts: %s", attempt + 1, error)
                    raise
                delay = self._backoff(attempt)
                logger.warning(
                    "Transient HTTP GET failure on attempt %d/%d; retrying in %.3fs: %s",
                    attempt + 1,
                    self._settings.retry_attempts,
                    delay,
                    error,
                )
                self._wait(delay)
                continue

            if not self._is_transient(response):
                return response
            if attempt == self._settings.retry_attempts - 1:
                logger.warning(
                    "Transient HTTP status %d exhausted %d attempts",
                    response.status_code,
                    self._settings.retry_attempts,
                )
                response.raise_for_status()
                return response

            retry_after = self._retry_after(response)
            delay = self._backoff(attempt) if retry_after is None else retry_after
            logger.warning(
                "Transient HTTP status %d on attempt %d/%d; retrying in %.3fs",
                response.status_code,
                attempt + 1,
                self._settings.retry_attempts,
                delay,
            )
            response.close()
            self._wait(delay)

        raise AssertionError("retry loop must return or raise")

    def _raise_if_stopped(self) -> None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise CollectorStopped("Collector stop requested")

    def _wait(self, delay: float) -> None:
        if self._stop_event is not None and self._stop_event.wait(delay):
            raise CollectorStopped("Collector stop requested")

    def _backoff(self, attempt: int) -> float:
        return min(
            self._settings.retry_max_backoff_seconds,
            self._settings.retry_backoff_seconds * 2**attempt,
        )

    def _retry_after(self, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            if value.strip().isdigit():
                return min(float(value.strip()), self._settings.retry_max_backoff_seconds)
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            return min(seconds, self._settings.retry_max_backoff_seconds)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Ignoring malformed Retry-After header: %r", value)
            return None

    @staticmethod
    def _is_transient(response: httpx.Response) -> bool:
        return response.status_code == 429 or 500 <= response.status_code <= 599
