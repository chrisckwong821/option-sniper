#!/usr/bin/env python3
"""Serve Option Sniper and adapt Databento live sockets to a browser WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import databento as db
from aiohttp import WSMsgType, web

try:
    from .iv_expansion import IvExpansionTracker, option_contract_symbols
except ImportError:
    from iv_expansion import IvExpansionTracker, option_contract_symbols

try:
    from .option_contract_catalog import (
        fetch_option_contract_catalog,
        load_option_contract_catalog,
        plan_option_subscriptions,
    )
except ImportError:
    from option_contract_catalog import (
        fetch_option_contract_catalog,
        load_option_contract_catalog,
        plan_option_subscriptions,
    )

try:
    from .databento_config import (
        resolve_historical_api_keys,
        resolve_live_api_key,
        resolve_live_option_api_key,
    )
    from .dashboard_state import (
        DEFAULT_STATE_CACHE,
        DashboardStateManager,
        RefreshSettings,
        build_refresh_plan,
        refresh_dashboard_state,
        with_yesterday_snapshot,
    )
except ImportError:  # Support direct execution with `python scripts/...`.
    from databento_config import (
        resolve_historical_api_keys,
        resolve_live_api_key,
        resolve_live_option_api_key,
    )
    from dashboard_state import (
        DEFAULT_STATE_CACHE,
        DashboardStateManager,
        RefreshSettings,
        build_refresh_plan,
        refresh_dashboard_state,
        with_yesterday_snapshot,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "dashboard"
DEFAULT_CACHE = ROOT / ".cache" / "sigma-tape" / "live-prices.json"
DEFAULT_IV_CACHE = ROOT / ".cache" / "sigma-tape" / "live-iv-expansion.json"
DEFAULT_OPTION_CATALOG_CACHE = (
    ROOT / ".cache" / "sigma-tape" / "option-contracts.json"
)
EASTERN = ZoneInfo("America/New_York")
SESSION_LABELS = {
    "regular": "Regular",
    "premarket": "Pre-market",
    "afterhours": "After-hours",
    "overnight": "Overnight",
    "closed": "Closed",
}
STALE_AFTER_SECONDS = {
    "regular": 90,
    "premarket": 300,
    "afterhours": 600,
    "overnight": 900,
    "closed": 900,
}
TASKS_KEY = web.AppKey("tasks", list)
STREAM_SCHEMA_VERSION = "1.2"
DEFAULT_BROADCAST_INTERVAL = 1.0
DEFAULT_STATUS_INTERVAL = 15.0
DEFAULT_SEND_TIMEOUT = 2.0


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def market_session_at(value: datetime) -> str:
    eastern = ensure_aware(value).astimezone(EASTERN)
    weekday = eastern.weekday()
    local_time = eastern.time().replace(tzinfo=None)

    if (weekday == 6 and local_time >= time(20)) or (
        weekday in range(0, 5) and local_time < time(4)
    ) or (weekday in range(0, 4) and local_time >= time(20)):
        return "overnight"
    if weekday >= 5:
        return "closed"
    if time(4) <= local_time < time(9, 30):
        return "premarket"
    if time(9, 30) <= local_time < time(16):
        return "regular"
    if time(16) <= local_time < time(20):
        return "afterhours"
    return "closed"


def trading_date_at(value: datetime) -> str:
    eastern = ensure_aware(value).astimezone(EASTERN)
    if eastern.time().replace(tzinfo=None) >= time(20):
        eastern += timedelta(days=1)
    return eastern.date().isoformat()


def timestamp_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, UTC)


def regular_close_replay_start(value: datetime) -> datetime:
    eastern = ensure_aware(value).astimezone(EASTERN)
    return eastern.replace(hour=15, minute=30, second=0, microsecond=0).astimezone(UTC)


@dataclass(frozen=True)
class FeedConfig:
    dataset: str
    phases: frozenset[str]


class PriceStore:
    def __init__(
        self,
        symbols: list[str],
        cache_path: Path,
        broadcast_interval: float = DEFAULT_BROADCAST_INTERVAL,
        status_interval: float = DEFAULT_STATUS_INTERVAL,
        send_timeout: float = DEFAULT_SEND_TIMEOUT,
        iv_tracker: IvExpansionTracker | None = None,
    ) -> None:
        self.symbols = frozenset(symbols)
        self.cache_path = cache_path
        self.broadcast_interval = broadcast_interval
        self.status_interval = status_interval
        self.send_timeout = send_timeout
        self.iv_tracker = iv_tracker
        self.prices: dict[str, dict[str, Any]] = {}
        self.regular_prices: dict[str, dict[str, Any]] = {}
        self.feed_status: dict[str, dict[str, Any]] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self.dirty_symbols: set[str] = set()
        self.status_dirty = False
        self.cache_dirty = False
        self.sequence = 0
        self.started_at = utc_now()
        self.snapshot_generation = 0
        self.snapshot_cache: tuple[int, int, str] | None = None
        self.broadcasts = 0
        self.delta_broadcasts = 0
        self.status_broadcasts = 0
        self.snapshot_sends = 0
        self.fanout_messages = 0
        self.fanout_bytes = 0
        self.send_failures = 0
        self.last_batch_symbols = 0
        self.max_batch_symbols = 0
        self.last_message_bytes = 0
        self.last_broadcast_duration_ms = 0.0

    def invalidate_snapshot(self) -> None:
        self.snapshot_generation += 1
        self.snapshot_cache = None

    def symbols_missing_regular_close(self, trading_date: str) -> list[str]:
        return sorted(
            ticker
            for ticker in self.symbols
            if self.regular_prices.get(ticker, {}).get("trading_date") != trading_date
        )

    def set_feed_status(self, dataset: str, state: str, detail: str = "") -> None:
        previous = self.feed_status.get(dataset, {})
        last_error = previous.get("last_error")
        if detail and state in {"degraded", "offline", "retrying"}:
            last_error = detail
        self.feed_status[dataset] = {
            "state": state,
            "detail": detail,
            "last_error": last_error,
            "updated_at": utc_now().isoformat(),
            "last_record_at": previous.get("last_record_at"),
        }
        self.status_dirty = True
        self.invalidate_snapshot()

    def touch_feed(self, dataset: str) -> None:
        status = self.feed_status.setdefault(dataset, {})
        status.update({
            "state": "live",
            "detail": "",
            "updated_at": utc_now().isoformat(),
            "last_record_at": utc_now().isoformat(),
        })

    def update_price(
        self,
        ticker: str,
        price: float,
        event_time: datetime,
        dataset: str,
        volume: int | None = None,
        *,
        session_override: str | None = None,
        trading_date_override: str | None = None,
    ) -> bool:
        event_time = ensure_aware(event_time).astimezone(UTC)
        session = session_override or market_session_at(event_time)
        if ticker not in self.symbols or not math.isfinite(price) or price <= 0:
            return False
        if session == "closed" or session not in SESSION_LABELS:
            return False

        point = {
            "ticker": ticker,
            "price": price,
            "ts_event": event_time.isoformat(),
            "source_dataset": dataset,
            "session": session,
            "session_label": SESSION_LABELS[session],
            "trading_date": trading_date_override or trading_date_at(event_time),
            "is_extended_hours": session != "regular",
            "volume": volume,
        }
        iv_updated = (
            self.iv_tracker.update_spot(ticker, price, event_time)
            if self.iv_tracker is not None
            else False
        )
        regular_updated = False
        if session == "regular":
            previous_regular = self.regular_prices.get(ticker)
            if not previous_regular or event_time > datetime.fromisoformat(
                previous_regular["ts_event"]
            ):
                self.regular_prices[ticker] = point
                regular_updated = True

        previous = self.prices.get(ticker)
        latest_updated = not previous or event_time > datetime.fromisoformat(
            previous["ts_event"]
        )
        if latest_updated:
            self.prices[ticker] = point
        if not latest_updated and not regular_updated and not iv_updated:
            return False

        self.touch_feed(dataset)
        self.dirty_symbols.add(ticker)
        self.cache_dirty = True
        self.invalidate_snapshot()
        return True

    def update_option_quote(
        self,
        raw_symbol: str,
        bid: float,
        ask: float,
        event_time: datetime,
    ) -> bool:
        if self.iv_tracker is None:
            return False
        ticker = self.iv_tracker.update_option_quote(
            raw_symbol,
            bid,
            ask,
            event_time,
        )
        if ticker is None:
            return False
        self.dirty_symbols.add(ticker)
        self.cache_dirty = True
        self.invalidate_snapshot()
        return True

    def public_price(self, point: dict[str, Any], now: datetime) -> dict[str, Any]:
        event_time = datetime.fromisoformat(point["ts_event"])
        age_seconds = max(0.0, (now - event_time).total_seconds())
        stale_after = STALE_AFTER_SECONDS.get(point["session"], 300)
        regular = self.regular_prices.get(point["ticker"])
        regular_close = None
        if regular:
            regular_close = {
                "price": regular["price"],
                "ts_event": regular["ts_event"],
                "source_dataset": regular["source_dataset"],
                "trading_date": regular["trading_date"],
            }
        return {
            **point,
            "regular_close": regular_close,
            "age_seconds": round(age_seconds, 1),
            "is_stale": age_seconds > stale_after,
        }

    def message(
        self,
        message_type: str,
        tickers: list[str],
        *,
        advance_sequence: bool = False,
    ) -> dict[str, Any]:
        if advance_sequence:
            self.sequence += 1
        now = utc_now()
        return {
            "type": message_type,
            "schema_version": STREAM_SCHEMA_VERSION,
            "sequence": self.sequence,
            "server_time": now.isoformat(),
            "market_session": market_session_at(now),
            "market_session_label": SESSION_LABELS[market_session_at(now)],
            "trading_date": trading_date_at(now),
            "prices": [
                self.public_price(self.prices[ticker], now)
                for ticker in tickers
                if ticker in self.prices
            ],
            "iv_expansions": (
                self.iv_tracker.public_results(tickers, now)
                if self.iv_tracker is not None
                else []
            ),
            "feeds": self.feed_status,
        }

    def payload(self, message_type: str = "snapshot") -> dict[str, Any]:
        tickers = set(self.prices)
        if self.iv_tracker is not None:
            tickers.update(self.iv_tracker.results)
        return self.message(message_type, sorted(tickers))

    def snapshot_text(self) -> tuple[int, str]:
        cached = self.snapshot_cache
        if cached and cached[:2] == (self.snapshot_generation, self.sequence):
            return cached[1], cached[2]
        text = self.encode(self.payload())
        self.snapshot_cache = (self.snapshot_generation, self.sequence, text)
        return self.sequence, text

    @staticmethod
    def encode(payload: dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    async def send_snapshot(self, client: web.WebSocketResponse) -> int:
        sequence, text = self.snapshot_text()
        await asyncio.wait_for(client.send_str(text), timeout=self.send_timeout)
        self.snapshot_sends += 1
        self.fanout_messages += 1
        self.fanout_bytes += len(text.encode("utf-8"))
        return sequence

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = self.encode(payload)
        message_bytes = len(text.encode("utf-8"))
        clients = tuple(self.clients)
        started = asyncio.get_running_loop().time()

        async def send(client: web.WebSocketResponse) -> bool:
            try:
                await asyncio.wait_for(client.send_str(text), timeout=self.send_timeout)
                return True
            except (asyncio.TimeoutError, ConnectionError, RuntimeError):
                return False

        results = await asyncio.gather(*(send(client) for client in clients))
        disconnected = [client for client, sent in zip(clients, results) if not sent]
        for client in disconnected:
            self.clients.discard(client)

        sent_count = len(clients) - len(disconnected)
        self.broadcasts += 1
        self.fanout_messages += sent_count
        self.fanout_bytes += sent_count * message_bytes
        self.send_failures += len(disconnected)
        self.last_message_bytes = message_bytes
        self.last_broadcast_duration_ms = round(
            (asyncio.get_running_loop().time() - started) * 1000,
            3,
        )

    def stream_metrics(self) -> dict[str, Any]:
        return {
            "connected_clients": len(self.clients),
            "sequence": self.sequence,
            "broadcast_interval_seconds": self.broadcast_interval,
            "status_interval_seconds": self.status_interval,
            "broadcasts": self.broadcasts,
            "delta_broadcasts": self.delta_broadcasts,
            "status_broadcasts": self.status_broadcasts,
            "snapshot_sends": self.snapshot_sends,
            "fanout_messages": self.fanout_messages,
            "fanout_bytes": self.fanout_bytes,
            "send_failures": self.send_failures,
            "last_batch_symbols": self.last_batch_symbols,
            "max_batch_symbols": self.max_batch_symbols,
            "last_message_bytes": self.last_message_bytes,
            "last_broadcast_duration_ms": self.last_broadcast_duration_ms,
        }

    def load_cache(self) -> None:
        if not self.cache_path.exists():
            if self.iv_tracker is not None:
                self.iv_tracker.load_cache()
            return
        try:
            cached = json.loads(self.cache_path.read_text())
            for point in cached.get("prices", []):
                ticker = point.get("ticker")
                if ticker not in self.symbols:
                    continue
                datetime.fromisoformat(point["ts_event"])
                self.prices[ticker] = {
                    key: point.get(key)
                    for key in (
                        "ticker", "price", "ts_event", "source_dataset", "session",
                        "session_label", "trading_date", "is_extended_hours", "volume",
                    )
                }
            for point in cached.get("regular_prices", []):
                ticker = point.get("ticker")
                if ticker not in self.symbols:
                    continue
                datetime.fromisoformat(point["ts_event"])
                self.regular_prices[ticker] = {
                    key: point.get(key)
                    for key in (
                        "ticker", "price", "ts_event", "source_dataset", "session",
                        "session_label", "trading_date", "is_extended_hours", "volume",
                    )
                }
            for ticker, point in self.prices.items():
                if point.get("session") == "regular" and ticker not in self.regular_prices:
                    self.regular_prices[ticker] = point
            self.invalidate_snapshot()
        except (OSError, ValueError, TypeError, KeyError):
            self.prices = {}
            self.regular_prices = {}
        if self.iv_tracker is not None:
            for point in self.regular_prices.values():
                self.iv_tracker.update_spot(
                    str(point["ticker"]),
                    float(point["price"]),
                    datetime.fromisoformat(point["ts_event"]),
                )
            self.iv_tracker.load_cache()

    def save_cache(self) -> None:
        if not self.cache_dirty:
            if self.iv_tracker is not None:
                self.iv_tracker.save_cache()
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "saved_at": utc_now().isoformat(),
            "prices": list(self.prices.values()),
            "regular_prices": list(self.regular_prices.values()),
        }, indent=2) + "\n")
        os.replace(temporary, self.cache_path)
        self.cache_dirty = False
        if self.iv_tracker is not None:
            self.iv_tracker.save_cache()

    async def broadcast_loop(self, stop: asyncio.Event) -> None:
        last_status_at = asyncio.get_running_loop().time()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.broadcast_interval)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            dirty_symbols = sorted(self.dirty_symbols)
            self.dirty_symbols.clear()
            status_dirty = self.status_dirty
            self.status_dirty = False
            if not self.clients:
                continue
            now = asyncio.get_running_loop().time()
            if dirty_symbols:
                self.last_batch_symbols = len(dirty_symbols)
                self.max_batch_symbols = max(self.max_batch_symbols, len(dirty_symbols))
                payload = self.message("delta", dirty_symbols, advance_sequence=True)
                self.delta_broadcasts += 1
                last_status_at = now
            elif status_dirty or now - last_status_at >= self.status_interval:
                self.last_batch_symbols = 0
                payload = self.message("status", [])
                self.status_broadcasts += 1
                last_status_at = now
            else:
                continue
            await self.broadcast(payload)

    async def cache_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(5)
            await asyncio.to_thread(self.save_cache)


class DatabentoFeed:
    def __init__(
        self,
        api_key: str,
        config: FeedConfig,
        symbols: list[str],
        store: PriceStore,
        replay_minutes: int,
        stop: asyncio.Event,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.symbols = symbols
        self.store = store
        self.replay_minutes = replay_minutes
        self.stop = stop
        self.client: db.Live | None = None

    def connect(self) -> db.Live:
        client = db.Live(
            key=self.api_key,
            heartbeat_interval_s=10,
            reconnect_policy="reconnect",
            slow_reader_behavior="skip",
        )
        now = utc_now()
        replay_start = now - timedelta(minutes=self.replay_minutes)
        client.subscribe(
            dataset=self.config.dataset,
            schema="ohlcv-1s",
            symbols=self.symbols,
            stype_in="raw_symbol",
            start=replay_start,
        )
        if market_session_at(now) == "afterhours" and "regular" in self.config.phases:
            missing = self.store.symbols_missing_regular_close(trading_date_at(now))
            if missing:
                client.subscribe(
                    dataset=self.config.dataset,
                    schema="ohlcv-1m",
                    symbols=missing,
                    stype_in="raw_symbol",
                    start=regular_close_replay_start(now),
                )
        return client

    async def connect_without_blocking_server(self) -> db.Live:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[db.Live] = loop.create_future()

        def resolve(client: db.Live | None, error: BaseException | None) -> None:
            if future.done():
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.terminate()
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(client)  # type: ignore[arg-type]

        def connect_in_thread() -> None:
            try:
                client = self.connect()
            except BaseException as exc:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve, None, exc)
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve, client, None)

        threading.Thread(
            target=connect_in_thread,
            name=f"databento-connect-{self.config.dataset}",
            daemon=True,
        ).start()
        return await future

    async def run(self) -> None:
        backoff = 1
        while not self.stop.is_set():
            mappings: dict[int, str] = {}
            self.store.set_feed_status(self.config.dataset, "connecting")
            try:
                self.client = await self.connect_without_blocking_server()
                self.store.set_feed_status(self.config.dataset, "subscribed")
                backoff = 1

                async for record in self.client:
                    if self.stop.is_set():
                        self.client.stop()
                        break
                    if isinstance(record, db.SymbolMappingMsg):
                        mappings[record.instrument_id] = str(record.stype_in_symbol).rstrip("\x00")
                    elif isinstance(record, db.OHLCVMsg):
                        ticker = mappings.get(record.instrument_id)
                        if not ticker:
                            continue
                        event_time = timestamp_from_ns(record.ts_event)
                        if market_session_at(event_time) not in self.config.phases:
                            continue
                        self.store.update_price(
                            ticker=ticker,
                            price=float(record.pretty_close),
                            event_time=event_time,
                            dataset=self.config.dataset,
                            volume=int(record.volume),
                        )
                    elif isinstance(record, db.SystemMsg):
                        self.store.touch_feed(self.config.dataset)
                    elif isinstance(record, db.ErrorMsg):
                        self.store.set_feed_status(
                            self.config.dataset,
                            "degraded",
                            str(record.msg),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Databento exposes several transport error types.
                self.store.set_feed_status(
                    self.config.dataset,
                    "retrying",
                    f"{type(exc).__name__}: {exc}",
                )
            finally:
                if self.client is not None:
                    with contextlib.suppress(Exception):
                        self.client.stop()
                self.client = None

            if self.stop.is_set():
                break
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                backoff = min(30, backoff * 2)


class DatabentoOptionFeed:
    def __init__(
        self,
        api_key: str,
        symbols: list[str],
        store: PriceStore,
        replay_minutes: int,
        stop: asyncio.Event,
        subscription_chunk_size: int = 2_000,
        symbol_provider: Callable[[], list[str]] | None = None,
        replan_interval_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.symbols = set(symbols)
        self.store = store
        self.replay_minutes = replay_minutes
        self.stop = stop
        self.subscription_chunk_size = subscription_chunk_size
        self.symbol_provider = symbol_provider
        self.replan_interval_seconds = replan_interval_seconds
        self.client: db.Live | None = None

    def _subscribe(
        self,
        client: db.Live,
        symbols: list[str],
        *,
        replay_start: datetime | None,
    ) -> None:
        for start in range(0, len(symbols), self.subscription_chunk_size):
            request = {
                "dataset": "OPRA.PILLAR",
                "schema": "cbbo-1s",
                "symbols": symbols[start:start + self.subscription_chunk_size],
                "stype_in": "raw_symbol",
            }
            if replay_start is not None:
                request["start"] = replay_start
            client.subscribe(**request)

    def connect(self) -> db.Live:
        client = db.Live(
            key=self.api_key,
            heartbeat_interval_s=10,
            reconnect_policy="reconnect",
            slow_reader_behavior="skip",
        )
        replay_start = (utc_now() - timedelta(minutes=self.replay_minutes)) if self.replay_minutes > 0 else None
        self._subscribe(client, sorted(self.symbols), replay_start=replay_start)
        return client

    async def refresh_subscriptions(self) -> None:
        if self.symbol_provider is None:
            return
        while not self.stop.is_set() and self.client is not None:
            try:
                await asyncio.wait_for(
                    self.stop.wait(),
                    timeout=self.replan_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set() or self.client is None:
                break
            additions = sorted(set(self.symbol_provider()) - self.symbols)
            if not additions:
                continue
            # Databento accepts additional subscriptions after a session has
            # started, but intraday replay cannot be requested for additions.
            self._subscribe(self.client, additions, replay_start=None)
            self.symbols.update(additions)
            self.store.set_feed_status(
                "OPRA.PILLAR",
                "subscribed",
                f"{len(self.symbols)} definition-backed same-expiry contracts",
            )

    async def connect_without_blocking_server(self) -> db.Live:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[db.Live] = loop.create_future()

        def resolve(client: db.Live | None, error: BaseException | None) -> None:
            if future.done():
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.terminate()
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(client)  # type: ignore[arg-type]

        def connect_in_thread() -> None:
            try:
                client = self.connect()
            except BaseException as exc:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve, None, exc)
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(resolve, client, None)

        threading.Thread(
            target=connect_in_thread,
            name="databento-connect-OPRA.PILLAR",
            daemon=True,
        ).start()
        return await future

    async def run(self) -> None:
        backoff = 1
        dataset = "OPRA.PILLAR"
        while not self.stop.is_set():
            mappings: dict[int, str] = {}
            self.store.set_feed_status(dataset, "connecting")
            try:
                self.client = await self.connect_without_blocking_server()
                self.store.set_feed_status(
                    dataset,
                    "subscribed",
                    f"{len(self.symbols)} definition-backed same-expiry contracts",
                )
                backoff = 1
                planner = asyncio.create_task(
                    self.refresh_subscriptions(),
                    name="option-subscription-planner",
                )
                try:
                    records_since_yield = 0
                    async for record in self.client:
                        records_since_yield += 1
                        if records_since_yield >= 100:
                            await asyncio.sleep(0)
                            records_since_yield = 0
                        if self.stop.is_set():
                            self.client.stop()
                            break
                        if isinstance(record, db.SymbolMappingMsg):
                            mapped_symbol = getattr(record, "stype_out_symbol", None)
                            input_symbol = getattr(record, "stype_in_symbol", None)
                            raw_symbol = str(mapped_symbol or input_symbol or "").rstrip("\x00")
                            if not raw_symbol:
                                continue
                            mappings[record.instrument_id] = raw_symbol
                        elif isinstance(record, db.CBBOMsg):
                            raw_symbol = mappings.get(record.instrument_id)
                            if not raw_symbol:
                                continue
                            try:
                                bid = float(record.pretty_bid_px_00)
                                ask = float(record.pretty_ask_px_00)
                            except (TypeError, ValueError):
                                continue
                            self.store.update_option_quote(
                                raw_symbol=raw_symbol,
                                bid=bid,
                                ask=ask,
                                event_time=timestamp_from_ns(record.ts_recv),
                            )
                            self.store.touch_feed(dataset)
                        elif isinstance(record, db.SystemMsg):
                            self.store.touch_feed(dataset)
                        elif isinstance(record, db.ErrorMsg):
                            self.store.set_feed_status(
                                dataset,
                                "degraded",
                                str(record.msg),
                            )
                finally:
                    planner.cancel()
                    await asyncio.gather(planner, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.set_feed_status(
                    dataset,
                    "retrying",
                    f"{type(exc).__name__}: {exc}",
                )
            finally:
                if self.client is not None:
                    with contextlib.suppress(Exception):
                        self.client.stop()
                self.client = None

            if self.stop.is_set():
                break
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                backoff = min(30, backoff * 2)


async def mock_feed(
    universe: dict[str, Any], store: PriceStore, stop: asyncio.Event
) -> None:
    rng = random.Random(27)
    anchors = {
        row["ticker"]: float(
            row.get("anchor_price") or row.get("live_anchor_price") or row["last_price"]
        )
        for row in universe["rows"]
    }
    offsets = {ticker: rng.uniform(-0.018, 0.028) for ticker in anchors}
    phase = 0.0
    store.set_feed_status("MOCK.LIVE", "live", "Deterministic development stream")
    while not stop.is_set():
        now = utc_now()
        session = market_session_at(now)
        if session == "closed":
            session = "afterhours"
        trading_date = trading_date_at(now)
        mock_iv_time = now
        if session != "regular":
            mock_iv_date = universe.get("data_date", trading_date)
            mock_iv_time = datetime.fromisoformat(
                f"{mock_iv_date}T15:59:00"
            ).replace(tzinfo=EASTERN).astimezone(UTC)
        if session in {"afterhours", "overnight"}:
            regular_date = universe.get("anchor_date", trading_date)
            regular_time = datetime.fromisoformat(
                f"{regular_date}T15:59:59"
            ).replace(tzinfo=EASTERN).astimezone(UTC)
            missing_regular = set(store.symbols_missing_regular_close(regular_date))
            for ticker, anchor in anchors.items():
                if ticker in missing_regular:
                    store.update_price(
                        ticker,
                        anchor * (1 + offsets[ticker]),
                        regular_time,
                        "MOCK.LIVE",
                        session_override="regular",
                        trading_date_override=regular_date,
                    )
        for index, (ticker, anchor) in enumerate(anchors.items()):
            wave = math.sin(phase + index * 0.43) * 0.0025
            store.update_price(
                ticker,
                anchor * (1 + offsets[ticker] + wave),
                now,
                "MOCK.LIVE",
                100 + index,
                session_override=session,
                trading_date_override=trading_date,
            )
            if store.iv_tracker is not None and session in {"regular", "afterhours"}:
                expansion = (-2.0, 6.0, 13.4, 24.0, 38.2)[index % 5]
                store.iv_tracker.update_mock(
                    ticker,
                    expansion + wave * 100,
                    mock_iv_time,
                )
        phase += 0.35
        await asyncio.sleep(1)


def tracked_symbols(snapshot: dict[str, Any], universe: dict[str, Any]) -> list[str]:
    return sorted({
        row["ticker"]
        for payload in (snapshot, universe)
        for row in payload["rows"]
    })


STORE_KEY = web.AppKey("price_store", PriceStore)


def create_app(
    args: argparse.Namespace,
    state_bundle: dict[str, Any] | None = None,
) -> web.Application:
    if state_bundle is None:
        plan = build_refresh_plan()
        settings = RefreshSettings(
            market_cap_min=args.market_cap_min,
            daily_notional_min=args.daily_notional_min,
            candidate_target=args.candidate_target,
            result_limit=args.result_limit,
            spot_dataset=args.spot_dataset,
            max_spot_cost=args.max_spot_cost,
            max_option_cost=args.max_option_cost,
        )
        manager = DashboardStateManager(
            cache_path=args.state_cache,
            refresh=lambda refresh_plan: refresh_dashboard_state(
                refresh_plan,
                args.state_cache.parent,
                settings,
            ),
            settings=settings,
        )
        state_bundle = manager.ensure_state(
            plan,
            mode=args.refresh_mode,
            mock=args.mock,
            mock_symbol_count=getattr(args, "mock_symbols", 30),
        )

    from scripts.historical_cache import HistoricalCache
    state_bundle = with_yesterday_snapshot(
        state_bundle, getattr(args, "yesterday_state_cache", None),
    )
    snapshot = state_bundle["snapshot"]
    universe = state_bundle["today"]
    symbols = tracked_symbols(snapshot, universe)
    iv_tracker = IvExpansionTracker(
        rows=universe["rows"],
        option_expiry_close=state_bundle["refresh_plan"]["option_expiry_close"],
        anchor_date=universe["anchor_date"],
        cache_path=getattr(args, "iv_cache", DEFAULT_IV_CACHE),
        cycle_id=state_bundle.get("generated_at_utc"),
        calculation_interval_seconds=60,
    )
    store = PriceStore(
        symbols=symbols,
        cache_path=args.cache,
        broadcast_interval=getattr(
            args,
            "broadcast_interval",
            DEFAULT_BROADCAST_INTERVAL,
        ),
        status_interval=getattr(args, "status_interval", DEFAULT_STATUS_INTERVAL),
        send_timeout=getattr(args, "send_timeout", DEFAULT_SEND_TIMEOUT),
        iv_tracker=iv_tracker,
    )
    store.load_cache()
    from scripts.calibration_refresh import CalibrationRefresh
    calibration_refresh = CalibrationRefresh(
        store, getattr(args, "iv_cache", DEFAULT_IV_CACHE).parent,
        universe["anchor_date"], state_bundle.get("screening_policy_fingerprint"),
        interval=getattr(args, "calibration_refresh_seconds", 300),
    )
    option_catalog_path = getattr(
        args,
        "option_catalog_cache",
        DEFAULT_OPTION_CATALOG_CACHE,
    )
    option_expiry = state_bundle["refresh_plan"]["option_expiry"]
    option_catalog = None if args.mock else load_option_contract_catalog(
        option_catalog_path,
        option_expiry=option_expiry,
        minimum_as_of_date=state_bundle["refresh_plan"]["prior_session"],
    )
    option_runtime: dict[str, Any] = {
        "catalog": option_catalog,
        "catalog_error": None,
        "feed": None,
        "plans": {},
    }

    def current_spot_prices() -> dict[str, Any]:
        return {
            ticker: point.get("price")
            for ticker, point in store.prices.items()
        }

    def planned_option_symbols() -> list[str]:
        catalog = option_runtime["catalog"]
        if catalog is None:
            option_runtime["plans"] = {}
            return option_contract_symbols(universe["rows"])
        catalog_symbols, plans = plan_option_subscriptions(
            universe["rows"],
            catalog,
            spot_prices=current_spot_prices(),
        )
        covered = set((catalog.get("contracts") or {}).keys())
        fallback_rows = [
            row for row in universe["rows"]
            if row.get("ticker") not in covered
        ]
        option_runtime["plans"] = plans
        return sorted(set(catalog_symbols) | set(option_contract_symbols(fallback_rows)))

    option_symbols = planned_option_symbols()
    stop = asyncio.Event()
    app = web.Application()
    app[TASKS_KEY] = []
    app[STORE_KEY] = store

    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        origin = request.headers.get("Origin")
        allowed_origins = set(args.allowed_origin or [])
        if allowed_origins and origin and origin not in allowed_origins:
            raise web.HTTPForbidden(text="WebSocket origin is not allowed")
        socket = web.WebSocketResponse(heartbeat=20, receive_timeout=60)
        await socket.prepare(request)
        while True:
            snapshot_sequence = await store.send_snapshot(socket)
            if store.sequence == snapshot_sequence:
                break
        store.clients.add(socket)
        try:
            async for message in socket:
                if message.type == WSMsgType.TEXT and message.data == "ping":
                    await socket.send_str("pong")
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            store.clients.discard(socket)
        return socket

    async def health_handler(_: web.Request) -> web.Response:
        payload = store.payload("health")
        payload["tracked_symbols"] = len(symbols)
        payload["fixed_list_symbols"] = len(snapshot["rows"])
        payload["iv_universe_symbols"] = len(universe["rows"])
        payload["cached_symbols"] = len(store.prices)
        payload["iv_expansion"] = {
            "reference_date": iv_tracker.anchor_date,
            "reference_symbols": len(iv_tracker.references),
            "subscribed_contracts": len(
                option_runtime["feed"].symbols
                if option_runtime["feed"] is not None
                else option_symbols
            ),
            "calculated_symbols": len(iv_tracker.results),
            "calculation_interval_seconds": iv_tracker.calculation_interval_seconds,
            "pending_calculations": len(iv_tracker.pending_calculations),
            "method": "moneyness-matched-black76-v1",
            "contract_catalog": {
                "source": (
                    "definition"
                    if option_runtime["catalog"] is not None
                    else "historical_quote_pairs"
                ),
                "as_of_date": (
                    option_runtime["catalog"].get("as_of_date")
                    if option_runtime["catalog"] is not None
                    else None
                ),
                "covered_symbols": len(
                    (option_runtime["catalog"] or {}).get("contracts", {})
                ),
                "planned_symbols": len(option_runtime["plans"]),
                "last_error": option_runtime["catalog_error"],
            },
        }
        payload["dashboard_state"] = {
            "data_date": snapshot["data_date"],
            "status": state_bundle.get("cache", {}).get("status", "unknown"),
            "source": state_bundle.get("cache", {}).get("source", "unknown"),
            "screening_policy_fingerprint": state_bundle.get(
                "screening_policy_fingerprint"
            ),
        }
        payload["calibration"] = dict(calibration_refresh.status)
        payload["history_repair"] = dict(history_repair_status)
        payload["stream"] = store.stream_metrics()
        return web.json_response(payload)

    from scripts.expected_sigma import ExpectedSigma
    expected_sigma = ExpectedSigma(
        getattr(args, "state_cache", args.cache).parent,
        max_option_cost=getattr(args, "max_option_cost", 5.0),
        max_spot_cost=getattr(args, "max_spot_cost", 1.0),
    )

    async def state_handler(request: web.Request) -> web.Response:
        payload = state_bundle if args.mock else expected_sigma.apply(state_bundle)
        current_etag = '"' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() + '"'
        headers = {"Cache-Control": "no-store", "ETag": current_etag}
        if request.headers.get("If-None-Match") == current_etag:
            return web.Response(status=304, headers=headers)
        return web.json_response(payload, headers=headers)

    history_repair_status = {"state": "external_worker", "error": None}

    async def expected_sigma_loop() -> None:
        from scripts.expected_sigma import next_activation
        while not stop.is_set():
            now = utc_now()
            activation = next_activation(now)
            # Prepare tomorrow independently: failure to repair an older active
            # snapshot must not prevent saving a close still inside replay.
            if activation is not None:
                try:
                    await asyncio.to_thread(expected_sigma.refresh, state_bundle, now,
                                            effective_at=activation, regular_prices=dict(store.regular_prices))
                except Exception:
                    pass  # Active-session failures below are user-visible.
            try:
                await asyncio.to_thread(expected_sigma.refresh, state_bundle,
                                        regular_prices=dict(store.regular_prices))
            except Exception as exc:
                expected_sigma.error = f"{type(exc).__name__}: {exc}"
            try:
                from scripts.iv_reference_refresh import shared_references, activate_references
                plan, references = await asyncio.to_thread(shared_references, expected_sigma)
                activate_references(store, plan, references)
            except Exception as exc:
                history_repair_status["reference_error"] = str(exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def index_handler(_: web.Request) -> web.FileResponse:
        return web.FileResponse(args.dashboard / "index.html")

    async def iv_calculation_loop() -> None:
        while not stop.is_set():
            changed = iv_tracker.flush_calculations()
            if changed:
                store.dirty_symbols.update(changed)
                store.cache_dirty = True
                store.invalidate_snapshot()
            await asyncio.sleep(0.1)

    async def on_startup(_: web.Application) -> None:
        tasks = [
            asyncio.create_task(store.broadcast_loop(stop), name="price-broadcast"),
            asyncio.create_task(store.cache_loop(stop), name="price-cache"),
            asyncio.create_task(iv_calculation_loop(), name="iv-calculation"),
        ]
        tasks.append(asyncio.create_task(calibration_refresh.run(stop), name="calibration-refresh"))
        if not args.mock:
            tasks.append(asyncio.create_task(expected_sigma_loop(), name="expected-sigma"))
        if args.mock:
            tasks.append(asyncio.create_task(mock_feed(universe, store, stop), name="mock-feed"))
        else:
            equity_api_key = resolve_live_api_key()
            live_option_api_key = resolve_live_option_api_key()
            _, historical_option_api_key = resolve_historical_api_keys()
            if not equity_api_key:
                store.set_feed_status(
                    "DATABENTO",
                    "offline",
                    "DATABENTO_LIVE_API_KEY is not set",
                )
            else:
                configs = [FeedConfig(
                    dataset=args.day_dataset,
                    phases=frozenset({"premarket", "regular", "afterhours"}),
                )]
                if not args.disable_overnight:
                    configs.append(FeedConfig(
                        dataset=args.overnight_dataset,
                        phases=frozenset({"overnight"}),
                    ))
                for config in configs:
                    feed = DatabentoFeed(
                        api_key=equity_api_key,
                        config=config,
                        symbols=symbols,
                        store=store,
                        replay_minutes=args.replay_minutes,
                        stop=stop,
                    )
                    tasks.append(asyncio.create_task(feed.run(), name=f"feed-{config.dataset}"))
            if live_option_api_key:
                async def run_option_feed() -> None:
                    store.set_feed_status(
                        "OPRA.PILLAR",
                        "connecting",
                        "Discovering listed same-expiry contracts",
                    )
                    if historical_option_api_key:
                        try:
                            option_runtime["catalog"] = await asyncio.to_thread(
                                fetch_option_contract_catalog,
                                HistoricalCache(
                                    historical_option_api_key, root=args.state_cache.parent),
                                universe["rows"],
                                as_of_date=universe["anchor_date"],
                                option_expiry=option_expiry,
                                cache_path=option_catalog_path,
                                max_cost=getattr(args, "max_definition_cost", 1.0),
                            )
                        except Exception as exc:
                            option_runtime["catalog_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                    elif option_runtime["catalog"] is None:
                        option_runtime["catalog_error"] = (
                            "DATABENTO_HISTORICAL_OPTIONS_API_KEY is not set; "
                            "using contracts retained in the dashboard snapshot"
                        )

                    symbols_for_feed = planned_option_symbols()
                    if not symbols_for_feed:
                        store.set_feed_status(
                            "OPRA.PILLAR",
                            "offline",
                            "No same-expiry option contracts are available",
                        )
                        return
                    option_feed = DatabentoOptionFeed(
                        api_key=live_option_api_key,
                        symbols=symbols_for_feed,
                        store=store,
                        replay_minutes=getattr(args, "option_replay_minutes", 0),
                        stop=stop,
                        symbol_provider=planned_option_symbols,
                    )
                    option_runtime["feed"] = option_feed
                    await option_feed.run()

                tasks.append(asyncio.create_task(
                    run_option_feed(),
                    name="feed-OPRA.PILLAR",
                ))
            elif not live_option_api_key:
                store.set_feed_status(
                    "OPRA.PILLAR",
                    "offline",
                    "DATABENTO_LIVE_OPTIONS_API_KEY is not set",
                )
            else:
                store.set_feed_status(
                    "OPRA.PILLAR",
                    "offline",
                    "Today's IV reference has no option-chain strikes; refresh the dashboard state",
                )
        app[TASKS_KEY] = tasks

    async def on_cleanup(_: web.Application) -> None:
        stop.set()
        for client in tuple(store.clients):
            with contextlib.suppress(Exception):
                await client.close(
                    code=1001,
                    message=b"Option Sniper adapter is shutting down",
                )
        store.clients.clear()
        tasks = app[TASKS_KEY]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(store.save_cache)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws/prices", websocket_handler)
    app.router.add_get("/api/health", health_handler)
    app.router.add_get("/api/state", state_handler)
    for asset in ("app.js", "live-calculations.js", "state-refresh.js", "styles.css",
                  "iv-sigma-prototype.html", "iv-sigma-prototype.css", "iv-sigma-prototype.js"):
        async def asset_handler(
            _: web.Request,
            asset_path: Path = args.dashboard / asset,
        ) -> web.FileResponse:
            return web.FileResponse(asset_path)

        app.router.add_get(f"/{asset}", asset_handler)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--state-cache", type=Path, default=DEFAULT_STATE_CACHE)
    parser.add_argument(
        "--yesterday-state-cache", type=Path,
        default=DEFAULT_STATE_CACHE.with_name("yesterday-state.json"),
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--iv-cache", type=Path, default=DEFAULT_IV_CACHE)
    parser.add_argument(
        "--option-catalog-cache",
        type=Path,
        default=DEFAULT_OPTION_CATALOG_CACHE,
    )
    parser.add_argument("--calibration-refresh-seconds", type=float, default=300)
    parser.add_argument("--max-definition-cost", type=float, default=1.0)
    parser.add_argument("--day-dataset", default="EQUS.MINI")
    parser.add_argument("--overnight-dataset", default="OCEA.MEMOIR")
    parser.add_argument("--replay-minutes", type=int, default=60)
    parser.add_argument("--option-replay-minutes", type=int, default=0)
    parser.add_argument("--broadcast-interval", type=float, default=DEFAULT_BROADCAST_INTERVAL)
    parser.add_argument("--status-interval", type=float, default=DEFAULT_STATUS_INTERVAL)
    parser.add_argument("--send-timeout", type=float, default=DEFAULT_SEND_TIMEOUT)
    parser.add_argument("--disable-overnight", action="store_true")
    parser.add_argument("--mock-symbols", type=int, default=30)
    parser.add_argument(
        "--refresh-mode",
        choices=("auto", "force", "never"),
        default="auto",
        help="Refresh stale dashboard state, always refresh, or only use cache.",
    )
    parser.add_argument("--market-cap-min", type=float, default=5_000_000_000)
    parser.add_argument("--daily-notional-min", type=float)
    parser.add_argument("--candidate-target", type=int)
    parser.add_argument("--result-limit", type=int, default=20)
    parser.add_argument(
        "--spot-dataset",
        choices=("EQUS.MINI", "EQUS.SUMMARY"),
        default="EQUS.SUMMARY",
        help="Historical dataset used for completed-session prices and liquidity.",
    )
    parser.add_argument("--max-spot-cost", type=float, default=1.0)
    parser.add_argument("--max-option-cost", type=float, default=5.0)
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[
            origin.strip()
            for origin in os.environ.get("SIGMA_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ],
    )
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    if args.calibration_refresh_seconds <= 0:
        parser.error("--calibration-refresh-seconds must be positive")
    return args


def main() -> None:
    args = parse_args()
    if args.mock and args.cache == DEFAULT_CACHE:
        args.cache = DEFAULT_CACHE.with_name("mock-live-prices.json")
    web.run_app(
        create_app(args),
        host=args.host,
        port=args.port,
        print=lambda message: print(message.replace("======== Running on", "Option Sniper running on")),
    )


if __name__ == "__main__":
    main()
