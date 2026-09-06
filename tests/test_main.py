import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from fivecast.config import Settings
from fivecast.feeds.polymarket import NoActiveMarket
from fivecast.main import Observer, format_snapshot, main
from fivecast.models import PredictionMarket
from fivecast.storage.sqlite import SnapshotStore


def test_observer_end_to_end_with_mocked_public_gets(tmp_path, start, monkeypatch, capsys):
    calls = []
    times = iter(start + timedelta(seconds=n) for n in (120, 120, 125, 125))
    monkeypatch.setattr("fivecast.main.utc_now", lambda: next(times))
    monkeypatch.setattr("fivecast.feeds.btc.utc_now", lambda: start + timedelta(seconds=119))
    monkeypatch.setattr("fivecast.main.time.sleep", lambda _: None)

    def respond(request):
        assert request.method == "GET"
        calls.append(request.url.path)
        if request.url.host == "gamma-api.polymarket.com":
            return httpx.Response(
                200,
                json={
                    "id": "1234",
                    "conditionId": "0x" + "a" * 64,
                    "question": "Bitcoin Up or Down?",
                    "slug": f"btc-updown-5m-{int(start.timestamp())}",
                    "eventStartTime": start.isoformat(),
                    "endDate": (start + timedelta(minutes=5)).isoformat(),
                    "outcomes": json.dumps(["Up", "Down"]),
                    "clobTokenIds": json.dumps(["111", "222"]),
                    "active": True,
                    "closed": False,
                    "archived": False,
                    "enableOrderBook": True,
                    "acceptingOrders": True,
                },
            )
        if request.url.path.endswith("candles"):
            return httpx.Response(
                200, json=[[int(start.timestamp()), 99900, 100200, 100000, 100100, 5]]
            )
        if request.url.path.endswith("ticker"):
            return httpx.Response(
                200,
                json={"price": "100100", "time": (start + timedelta(seconds=118)).isoformat()},
            )
        assert request.url.host == "clob.polymarket.com"
        assert request.url.path == "/book"
        return httpx.Response(
            200,
            json={
                "market": "0x" + "a" * 64,
                "asset_id": request.url.params["token_id"],
                "timestamp": str(int((start.timestamp() + 119) * 1000)),
                "bids": [{"price": "0.4", "size": "10"}],
                "asks": [{"price": "0.5", "size": "10"}],
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        SnapshotStore(tmp_path / "research.db") as store,
    ):
        Observer(client, store, Settings()).run(iterations=2)
        assert store.count_snapshots() == 2
        assert store.get_latest_snapshot().seconds_remaining == 175
        assert calls.count("/products/BTC-USD/candles") == 1
    assert capsys.readouterr().out.count("Snapshot saved.") == 2


def test_human_readable_output(snapshot):
    output = format_snapshot(snapshot)
    assert "UTC" in output
    assert "$100,100.00" in output
    assert "+0.1000%" in output
    assert "Coinbase 1m candle open" in output


def test_malformed_response_is_not_hidden(tmp_path, monkeypatch):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        ) as client,
        SnapshotStore(tmp_path / "research.db") as store,
    ):
        with pytest.raises(ValidationError):
            Observer(client, store, Settings()).run()
        assert store.count_snapshots() == 0


def test_bounded_run_fails_on_http_error(tmp_path):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client,
        SnapshotStore(tmp_path / "research.db") as store,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            Observer(client, store, Settings()).run(iterations=1)
        assert store.count_snapshots() == 0


def test_invalid_iteration_count():
    with pytest.raises(SystemExit) as error:
        main(["--iterations", "0"])
    assert error.value.code == 2


def test_ctrl_c_closes_resources(tmp_path, monkeypatch):
    def interrupt(self, iterations):
        raise KeyboardInterrupt

    monkeypatch.setattr(Observer, "run", interrupt)
    main(["--once", "--db-path", str(tmp_path / "research.db")])
    with SnapshotStore(tmp_path / "research.db") as store:
        assert store.count_snapshots() == 0


def test_observer_refreshes_opening_at_window_rollover(tmp_path, inputs, monkeypatch):
    opening_requests = []

    def opening(start):
        opening_requests.append(start)
        return inputs["opening"]

    with httpx.Client() as client, SnapshotStore(tmp_path / "research.db") as store:
        observer = Observer(client, store, Settings())
        monkeypatch.setattr(observer.polymarket, "discover_market", lambda _: inputs["market"])
        monkeypatch.setattr(observer.btc, "get_window_open", opening)
        monkeypatch.setattr(observer.btc, "get_quote", lambda: inputs["btc"])
        monkeypatch.setattr(observer.polymarket, "get_quote", lambda _, side: inputs[side.lower()])
        monkeypatch.setattr("fivecast.main.utc_now", lambda: inputs["timestamp"])
        first = observer.observe()
        later_start = inputs["market"].start_time_utc + timedelta(minutes=5)
        inputs["market"] = PredictionMarket.model_validate(
            {
                **inputs["market"].model_dump(),
                "market_id": "5678",
                "slug": f"btc-updown-5m-{int(later_start.timestamp())}",
                "start_time_utc": later_start,
                "end_time_utc": later_start + timedelta(minutes=5),
                "up_token_id": "333",
                "down_token_id": "444",
            }
        )
        for key in ("btc", "opening", "up", "down"):
            old = inputs[key]
            values = old.model_dump()
            for field in values:
                if field.endswith("_utc"):
                    values[field] += timedelta(minutes=5)
            if key in ("up", "down"):
                values["token_id"] = "333" if key == "up" else "444"
            if key == "opening":
                values["price"] = "100050"
            inputs[key] = type(old).model_validate(values)
        inputs["timestamp"] += timedelta(minutes=5)
        second = observer.observe()
        assert second.market_id != first.market_id
        assert second.btc_window_open_price != first.btc_window_open_price
        assert opening_requests == [first.market_start_utc, second.market_start_utc]
        assert store.count_snapshots() == 2


def test_continuous_loop_logs_transient_absence_then_resumes(tmp_path, monkeypatch, caplog):
    attempts = []

    def unavailable_then_interrupt():
        attempts.append(1)
        if len(attempts) == 1:
            raise NoActiveMarket("No current book")
        raise KeyboardInterrupt

    with httpx.Client() as client, SnapshotStore(tmp_path / "research.db") as store:
        observer = Observer(client, store, Settings())
        monkeypatch.setattr(observer, "observe", unavailable_then_interrupt)
        monkeypatch.setattr("fivecast.main.time.sleep", lambda _: None)
        with pytest.raises(KeyboardInterrupt):
            observer.run()
        assert len(attempts) == 2
        assert "Observation skipped" in caplog.text
        assert store.count_snapshots() == 0


@pytest.mark.parametrize("lead", [0.2, 2])
def test_source_clock_lead_waits_only_within_one_second(tmp_path, inputs, monkeypatch, lead):
    from fivecast.market.snapshot import SnapshotUnavailable

    source_time = inputs["timestamp"] + timedelta(seconds=lead)
    inputs["down"] = type(inputs["down"]).model_validate(
        {**inputs["down"].model_dump(), "timestamp_utc": source_time}
    )
    current_time = inputs["timestamp"]
    waits = []

    def advance(seconds):
        nonlocal current_time
        waits.append(seconds)
        current_time += timedelta(seconds=seconds)

    with httpx.Client() as client, SnapshotStore(tmp_path / "research.db") as store:
        observer = Observer(client, store, Settings())
        monkeypatch.setattr(observer.polymarket, "discover_market", lambda _: inputs["market"])
        monkeypatch.setattr(observer.btc, "get_window_open", lambda _: inputs["opening"])
        monkeypatch.setattr(observer.btc, "get_quote", lambda: inputs["btc"])
        monkeypatch.setattr(observer.polymarket, "get_quote", lambda _, side: inputs[side.lower()])
        monkeypatch.setattr("fivecast.main.utc_now", lambda: current_time)
        monkeypatch.setattr("fivecast.main.time.sleep", advance)
        if lead <= 1:
            result = observer.observe()
            assert result.timestamp_utc == source_time
            assert waits == [lead]
            assert store.count_snapshots() == 1
        else:
            with pytest.raises(SnapshotUnavailable, match="future-dated"):
                observer.observe()
            assert waits == []
            assert store.count_snapshots() == 0
