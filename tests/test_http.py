from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from fivecast.feeds.http import CollectorStopped, RetryingClient


def settings(**overrides):
    values = {
        "request_timeout_seconds": 2,
        "retry_attempts": 3,
        "retry_backoff_seconds": 1,
        "retry_max_backoff_seconds": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingEvent:
    def __init__(self, stop_on_wait=False):
        self.waits = []
        self.stopped = False
        self.stop_on_wait = stop_on_wait

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.waits.append(delay)
        if self.stop_on_wait:
            self.stopped = True
            return True
        return self.stopped


def client(handler, **kwargs):
    return RetryingClient(
        settings(**kwargs.pop("settings", {})),
        stop_event=kwargs.pop("stop_event", None),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_timeout_then_success(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, request=request)

    event = RecordingEvent()
    with client(handler, stop_event=event) as http:
        response = http.get("https://example.test")
    assert response.status_code == 200
    assert calls == 2
    assert event.waits == [1]


def test_connection_error_is_retried_then_exhausted():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("reset", request=request)

    event = RecordingEvent()
    with client(handler, stop_event=event, settings={"retry_attempts": 2}) as http:
        with pytest.raises(httpx.ConnectError):
            http.get("https://example.test")
    assert calls == 2
    assert event.waits == [1]


def test_5xx_then_success():
    statuses = iter((503, 200))

    def handler(request):
        return httpx.Response(next(statuses), request=request)

    event = RecordingEvent()
    with client(handler, stop_event=event) as http:
        assert http.get("https://example.test").status_code == 200
    assert event.waits == [1]


def test_retry_after_numeric_is_capped():
    responses = iter((httpx.Response(429, headers={"Retry-After": "99"}), httpx.Response(200)))

    def handler(request):
        response = next(responses)
        response.request = request
        return response

    event = RecordingEvent()
    with client(handler, stop_event=event, settings={"retry_max_backoff_seconds": 4}) as http:
        http.get("https://example.test")
    assert event.waits == [4]


def test_retry_after_http_date_and_malformed_header(monkeypatch):
    now = datetime(2026, 9, 6, 6, 0, tzinfo=UTC)
    monkeypatch.setattr("fivecast.feeds.http.datetime", SimpleNamespace(now=lambda _tz: now))
    future = (now + timedelta(seconds=8)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    events = iter((future, "not-a-date", None))

    def handler(request):
        value = next(events)
        headers = {} if value is None else {"Retry-After": value}
        return httpx.Response(429 if value is not None else 200, headers=headers, request=request)

    event = RecordingEvent()
    with client(handler, stop_event=event, settings={"retry_max_backoff_seconds": 5}) as http:
        http.get("https://example.test")
    assert event.waits == [5, 2]


def test_nontransient_response_is_returned_without_retry():
    event = RecordingEvent()
    with client(lambda request: httpx.Response(404, request=request), stop_event=event) as http:
        response = http.get("https://example.test")
    assert response.status_code == 404
    assert event.waits == []


def test_cancelled_wait_raises_and_makes_no_further_request():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    event = RecordingEvent(stop_on_wait=True)
    with client(handler, stop_event=event) as http:
        with pytest.raises(CollectorStopped):
            http.get("https://example.test")
    assert calls == 1
    assert event.waits == [1]


def test_stop_set_before_request():
    event = RecordingEvent()
    event.stopped = True
    with client(lambda request: httpx.Response(200, request=request), stop_event=event) as http:
        with pytest.raises(CollectorStopped):
            http.get("https://example.test")


def test_default_stop_event_still_waits_between_attempts(monkeypatch):
    waits = []
    statuses = iter((503, 503, 200))
    with client(lambda request: httpx.Response(next(statuses))) as http:
        monkeypatch.setattr(http._stop_event, "wait", lambda delay: waits.append(delay) or False)
        assert http.get("https://example.test").status_code == 200
    assert waits == [1, 2]
