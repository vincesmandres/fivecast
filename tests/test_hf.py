import asyncio
import json
import socket
from datetime import UTC, datetime

from fivecast.hf_collect import HFCollector, parse_coinbase_message, parse_polymarket_message
from fivecast.hf_report import report
from fivecast.storage.sqlite import SnapshotStore

RECEIVED = datetime(2026, 9, 6, 5, 30, 1, tzinfo=UTC)


def test_coinbase_parser_rejects_malformed_typed_messages():
    try:
        parse_coinbase_message('{"type":"ticker","product_id":"BTC-USD"}', RECEIVED)
    except ValueError as error:
        assert "time" in str(error)
    else:
        raise AssertionError("malformed ticker was accepted")


def test_coinbase_last_match_is_a_valid_subscribe_snapshot():
    events = parse_coinbase_message(
        '{"type":"last_match","product_id":"BTC-USD","time":"2026-09-06T05:30:00Z",'
        '"price":"1","size":"2","sequence":1}',
        RECEIVED,
    )
    assert events[0].event_type == "last_match"
    assert events[0].price == "1"


def test_parsers_persist_typed_events_and_flags(tmp_path):
    ticker = json.dumps(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "time": "2026-09-06T05:30:00Z",
            "price": "100000",
            "best_bid": "99999",
            "best_ask": "100001",
            "sequence": 2,
        }
    )
    market = json.dumps(
        {
            "event_type": "price_change",
            "asset_id": "up",
            "timestamp": "1788672600000",
            "price": "0.61",
            "size": "4",
            "sequence": 4,
        }
    )
    with SnapshotStore(tmp_path / "hf.db") as store:
        collector = HFCollector(store, _market())
        collector._save(parse_coinbase_message(ticker, RECEIVED)[0])
        collector._save(parse_coinbase_message(ticker, RECEIVED)[0])
        collector._save(
            parse_coinbase_message(ticker.replace('"sequence": 2', '"sequence": 1'), RECEIVED)[0]
        )
        collector._save(parse_polymarket_message(market, RECEIVED, "market")[0])
        btc = store.connection.execute("SELECT * FROM btc_events ORDER BY id").fetchall()
        assert len(btc) == 3
        assert btc[1]["duplicate"] == 1
        assert btc[2]["out_of_order"] == 1
        assert store.connection.execute("SELECT COUNT(*) FROM market_events").fetchone()[0] == 1


class _Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration from None


def _market():
    from fivecast.models import PredictionMarket

    return PredictionMarket(
        market_id="market",
        condition_id="0x" + "a" * 64,
        question="q",
        slug="btc-updown-5m-1788672600",
        start_time_utc=datetime(2026, 9, 6, 5, 30, tzinfo=UTC),
        end_time_utc=datetime(2026, 9, 6, 5, 35, tzinfo=UTC),
        up_token_id="111",
        down_token_id="222",
    )


def test_reconnect_subscribes_publicly_and_graceful_run(tmp_path, monkeypatch):
    # asyncio's Windows proactor uses a localhost socketpair for its wakeup pipe.
    monkeypatch.setattr(socket.socket, "connect", socket._socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket._socket.socket.connect_ex)
    sockets = []
    ticker = json.dumps(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "time": "2026-09-06T05:30:00Z",
            "price": "1",
            "sequence": 1,
        }
    )

    def connector(uri):
        socket = _Socket([ticker] if "coinbase" in uri else [])
        sockets.append(socket)
        return socket

    async def run():
        with SnapshotStore(tmp_path / "hf.db") as store:
            await HFCollector(store, _market(), connector).run(maximum_connections=1)
            assert (
                store.connection.execute("SELECT COUNT(*) FROM hf_connections").fetchone()[0] == 2
            )

    asyncio.run(run())
    assert any(socket.sent and socket.sent[0]["type"] == "subscribe" for socket in sockets)
    assert any(socket.sent and socket.sent[0]["type"] == "market" for socket in sockets)


def test_report_metrics_is_read_only(tmp_path):
    path = tmp_path / "hf.db"
    with SnapshotStore(path) as store:
        event = parse_coinbase_message(
            '{"type":"ticker","product_id":"BTC-USD","time":"2026-09-06T05:30:00Z","price":"1","sequence":1}',
            RECEIVED,
        )[0]
        event.record(store)
    before = path.stat().st_mtime_ns
    values = report(path)
    assert values["event_count"] == 1
    assert values["source_timestamp_available_count"] == 1
    assert values["source_timestamp_missing_count"] == 0
    assert values["median_latency_ms"] == 1000.0
    assert path.stat().st_mtime_ns == before
