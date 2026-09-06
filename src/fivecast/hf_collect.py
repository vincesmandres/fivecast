"""Read-only high-frequency public websocket collection for M4B."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from fivecast.feeds.polymarket import PolymarketFeed
from fivecast.models import PredictionMarket
from fivecast.storage.sqlite import SnapshotStore

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
logger = logging.getLogger(__name__)


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return result.astimezone(UTC)


def _polymarket_timestamp(value: Any) -> datetime:
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    return _timestamp(value, "timestamp")


def _number(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return str(value)


@dataclass(frozen=True, slots=True)
class HFEvent:
    source: str
    local_receive_timestamp: datetime
    source_event_timestamp: datetime | None = None
    market_id: str | None = None
    token_id: str | None = None
    sequence: int | None = None
    event_type: str = "unknown"
    price: str | None = None
    bid: str | None = None
    ask: str | None = None
    size: str | None = None
    raw_payload: str = ""
    duplicate: int = 0
    out_of_order: int = 0
    source_to_receive_latency_ms: float | None = None

    def record(self, store: SnapshotStore) -> int:
        table = "btc_events" if self.source == "coinbase_exchange" else "market_events"
        return store.save_hf_event(
            table,
            {
                field: getattr(self, field)
                for field in (
                    "source_event_timestamp",
                    "local_receive_timestamp",
                    "source",
                    "market_id",
                    "token_id",
                    "sequence",
                    "event_type",
                    "price",
                    "bid",
                    "ask",
                    "size",
                    "raw_payload",
                    "duplicate",
                    "out_of_order",
                    "source_to_receive_latency_ms",
                )
            }
            | {
                "source_event_timestamp": None
                if self.source_event_timestamp is None
                else self.source_event_timestamp.isoformat(timespec="microseconds"),
                "local_receive_timestamp": self.local_receive_timestamp.isoformat(
                    timespec="microseconds"
                ),
            },
        )


def _raw(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sequence(payload: dict[str, Any]) -> int | None:
    value = payload.get("sequence")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("sequence must be an integer")
    return value


def parse_coinbase_message(message: str | bytes, received_at: datetime) -> list[HFEvent]:
    payload = json.loads(message)
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ValueError("Coinbase websocket message must be a typed object")
    kind = payload["type"]
    if kind not in {"ticker", "match", "last_match"}:
        if kind in {"subscriptions", "heartbeat", "error"}:
            return []
        raise ValueError(f"Unsupported Coinbase websocket message type: {kind}")
    if payload.get("product_id") != "BTC-USD":
        raise ValueError("Coinbase event is not BTC-USD")
    event_time = _timestamp(payload.get("time"), "time")
    raw = _raw(payload)
    common = dict(
        source="coinbase_exchange",
        local_receive_timestamp=received_at,
        source_event_timestamp=event_time,
        sequence=_sequence(payload),
        event_type=kind,
        raw_payload=raw,
        source_to_receive_latency_ms=(received_at - event_time).total_seconds() * 1000,
    )
    if kind == "ticker":
        price = _number(payload.get("price"), "price")
        return [
            HFEvent(
                **common,
                price=price,
                bid=_number(payload["best_bid"], "best_bid") if "best_bid" in payload else None,
                ask=_number(payload["best_ask"], "best_ask") if "best_ask" in payload else None,
            )
        ]
    return [
        HFEvent(
            **common,
            price=_number(payload.get("price"), "price"),
            size=_number(payload.get("size"), "size"),
        )
    ]


def parse_polymarket_message(
    message: str | bytes, received_at: datetime, market_id: str
) -> list[HFEvent]:
    payload = json.loads(message)
    if isinstance(payload, list):
        items = payload
    else:
        items = [payload]
    result: list[HFEvent] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("event_type"), str):
            raise ValueError("Polymarket websocket message must contain typed objects")
        kind = item["event_type"]
        if kind in {"book", "price_change"}:
            changes = item.get("price_changes", [item]) if kind == "price_change" else [item]
            if not isinstance(changes, list):
                raise ValueError("price_changes must be a list")
            for change in changes:
                if not isinstance(change, dict):
                    raise ValueError("price change must be an object")
                token = change.get("asset_id", item.get("asset_id"))
                if not isinstance(token, str) or not token:
                    raise ValueError("Polymarket event requires asset_id")
                bids = item.get("bids", []) if kind == "book" else []
                asks = item.get("asks", []) if kind == "book" else []
                if kind == "book" and (not isinstance(bids, list) or not isinstance(asks, list)):
                    raise ValueError("Polymarket book bids and asks must be lists")
                bid = change.get("best_bid", change.get("bid"))
                ask = change.get("best_ask", change.get("ask"))
                if kind == "book":
                    bid = max(
                        (_number(level["price"], "bid price") for level in bids), default=None
                    )
                    ask = min(
                        (_number(level["price"], "ask price") for level in asks), default=None
                    )
                price = change.get("price")
                result.append(
                    HFEvent(
                        source="polymarket_clob",
                        local_receive_timestamp=received_at,
                        source_event_timestamp=_polymarket_timestamp(item["timestamp"])
                        if "timestamp" in item
                        else None,
                        market_id=market_id,
                        token_id=token,
                        sequence=_sequence(item),
                        event_type=kind,
                        price=None if price is None else _number(price, "price"),
                        bid=None if bid is None else _number(bid, "bid"),
                        ask=None if ask is None else _number(ask, "ask"),
                        size=None
                        if change.get("size") is None
                        else _number(change["size"], "size"),
                        raw_payload=_raw(item),
                    )
                )
        elif kind in {"last_trade_price", "tick_size_change"}:
            continue
        else:
            raise ValueError(f"Unsupported Polymarket websocket message type: {kind}")
    return result


def reconnect_delay(attempt: int, initial: float = 1.0, maximum: float = 30.0) -> float:
    return min(maximum, initial * (2 ** max(0, attempt - 1)))


Connector = Callable[..., Awaitable[Any]]


class HFCollector:
    def __init__(
        self, store: SnapshotStore, market: PredictionMarket, connector: Connector = connect
    ):
        self.store = store
        self.market = market
        self.connector = connector
        self.stop_event = asyncio.Event()
        self._seen: dict[str, set[str]] = {"coinbase_exchange": set(), "polymarket_clob": set()}
        self._last_sequence: dict[str, int] = {}

    def stop(self) -> None:
        self.stop_event.set()

    def _save(self, event: HFEvent) -> None:
        digest = hashlib.sha256(event.raw_payload.encode()).hexdigest()
        identity = f"{event.source}:{event.sequence}:{digest}"
        duplicate = identity in self._seen[event.source]
        if event.sequence is not None and event.source in self._last_sequence:
            out_of_order = event.sequence < self._last_sequence[event.source]
        else:
            out_of_order = False
        self._seen[event.source].add(identity)
        if event.sequence is not None:
            self._last_sequence[event.source] = max(
                event.sequence, self._last_sequence.get(event.source, event.sequence)
            )
        event = event.__class__(
            **{**event.__dict__}
            if hasattr(event, "__dict__")
            else {field: getattr(event, field) for field in event.__dataclass_fields__}
            | {"duplicate": int(duplicate), "out_of_order": int(out_of_order)}
        )
        event.record(self.store)

    async def _source(
        self,
        name: str,
        uri: str,
        subscribe: dict[str, Any],
        parser: Callable[..., list[HFEvent]],
        maximum_connections: int | None = None,
    ) -> None:
        attempt = 0
        connections = 0
        while not self.stop_event.is_set() and (
            maximum_connections is None or connections < maximum_connections
        ):
            connection_id = self.store.save_hf_connection(
                {
                    "source": name,
                    "connected_at_utc": datetime.now(UTC).isoformat(timespec="microseconds"),
                    "disconnected_at_utc": None,
                    "status": "connected",
                    "reconnect_attempt": attempt,
                    "error": None,
                }
            )
            try:
                async with self.connector(uri) as websocket:
                    await websocket.send(json.dumps(subscribe, separators=(",", ":")))
                    attempt = 0
                    connections += 1
                    # A one-second receive timeout makes Ctrl+C/duration shutdown
                    # prompt even when an otherwise healthy stream is quiet.
                    while not self.stop_event.is_set():
                        if hasattr(websocket, "recv"):
                            try:
                                message = await asyncio.wait_for(websocket.recv(), timeout=1)
                            except TimeoutError:
                                continue
                        else:
                            try:
                                message = await websocket.__anext__()
                            except StopAsyncIteration:
                                break
                        received = datetime.now(UTC)
                        for event in parser(message, received):
                            self._save(event)
            except (
                ConnectionClosed,
                OSError,
                TimeoutError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                logger.warning("HF %s disconnected: %s", name, error)
                self.store.close_hf_connection(
                    connection_id, datetime.now(UTC), "failed", str(error)
                )
            else:
                self.store.close_hf_connection(connection_id, datetime.now(UTC))
            if not self.stop_event.is_set():
                attempt += 1
                await asyncio.sleep(reconnect_delay(attempt))

    async def run(self, maximum_connections: int | None = None) -> None:
        coinbase = self._source(
            "coinbase_exchange",
            COINBASE_WS,
            {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker", "matches"]},
            parse_coinbase_message,
            maximum_connections,
        )
        polymarket = self._source(
            "polymarket_clob",
            POLYMARKET_WS,
            {"assets_ids": [self.market.up_token_id, self.market.down_token_id], "type": "market"},
            lambda message, received: parse_polymarket_message(
                message, received, self.market.market_id
            ),
            maximum_connections,
        )
        await asyncio.gather(coinbase, polymarket)


def discover_active_market() -> PredictionMarket:
    with httpx.Client(timeout=10) as client:
        return PolymarketFeed(client).discover_market(datetime.now(UTC))


async def collect(path: Path, duration: float | None = None) -> None:
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("duration must be a finite positive number")
    deadline = None if duration is None else time.monotonic() + duration
    with SnapshotStore(path) as store:
        while deadline is None or time.monotonic() < deadline:
            market = discover_active_market()
            logger.info("HF DISCOVERED market=%s", market.market_id)
            collector = HFCollector(store, market)
            task = asyncio.create_task(collector.run())
            remaining = math.inf if deadline is None else deadline - time.monotonic()
            until_rollover = max(0.1, (market.end_time_utc - datetime.now(UTC)).total_seconds())
            try:
                await asyncio.sleep(min(remaining, until_rollover))
            finally:
                collector.stop()
                await task
            if deadline is not None and time.monotonic() >= deadline:
                break


def main() -> None:
    parser = argparse.ArgumentParser(description="FiveCast public high-frequency collector")
    parser.add_argument("--db-path", type=Path, default=Path("data/fivecast.db"))
    parser.add_argument("--duration", type=float)
    args = parser.parse_args()
    try:
        asyncio.run(collect(args.db_path, args.duration))
    except KeyboardInterrupt:
        logger.info("HF collector stopped gracefully")


if __name__ == "__main__":
    main()
