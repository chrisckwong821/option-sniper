#!/usr/bin/env python3
"""Serve Sigma Tape and adapt Databento live sockets to a browser WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import databento as db
from aiohttp import WSMsgType, web

try:
    from .databento_config import resolve_live_api_key
    from .dashboard_state import (
        DEFAULT_STATE_CACHE,
        DashboardStateManager,
        RefreshSettings,
        build_refresh_plan,
        refresh_dashboard_state,
    )
except ImportError:  # Support direct execution with `python scripts/...`.
    from databento_config import resolve_live_api_key
    from dashboard_state import (
        DEFAULT_STATE_CACHE,
        DashboardStateManager,
        RefreshSettings,
        build_refresh_plan,
        refresh_dashboard_state,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "dashboard"
DEFAULT_CACHE = ROOT / ".cache" / "sigma-tape" / "live-prices.json"
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


@dataclass(frozen=True)
class FeedConfig:
    dataset: str
    phases: frozenset[str]


class PriceStore:
    def __init__(self, symbols: list[str], cache_path: Path) -> None:
        self.symbols = frozenset(symbols)
        self.cache_path = cache_path
        self.prices: dict[str, dict[str, Any]] = {}
        self.feed_status: dict[str, dict[str, Any]] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self.changed = asyncio.Event()
        self.cache_dirty = False
        self.sequence = 0
        self.started_at = utc_now()

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
        self.changed.set()

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
    ) -> bool:
        event_time = ensure_aware(event_time).astimezone(UTC)
        session = market_session_at(event_time)
        if ticker not in self.symbols or not math.isfinite(price) or price <= 0:
            return False
        if session == "closed":
            return False

        previous = self.prices.get(ticker)
        if previous and event_time <= datetime.fromisoformat(previous["ts_event"]):
            return False

        self.prices[ticker] = {
            "ticker": ticker,
            "price": price,
            "ts_event": event_time.isoformat(),
            "source_dataset": dataset,
            "session": session,
            "session_label": SESSION_LABELS[session],
            "trading_date": trading_date_at(event_time),
            "is_extended_hours": session != "regular",
            "volume": volume,
        }
        self.touch_feed(dataset)
        self.cache_dirty = True
        self.changed.set()
        return True

    def public_price(self, point: dict[str, Any], now: datetime) -> dict[str, Any]:
        event_time = datetime.fromisoformat(point["ts_event"])
        age_seconds = max(0.0, (now - event_time).total_seconds())
        stale_after = STALE_AFTER_SECONDS.get(point["session"], 300)
        return {
            **point,
            "age_seconds": round(age_seconds, 1),
            "is_stale": age_seconds > stale_after,
        }

    def payload(self, message_type: str = "snapshot") -> dict[str, Any]:
        now = utc_now()
        self.sequence += 1
        return {
            "type": message_type,
            "schema_version": "1.0",
            "sequence": self.sequence,
            "server_time": now.isoformat(),
            "market_session": market_session_at(now),
            "market_session_label": SESSION_LABELS[market_session_at(now)],
            "trading_date": trading_date_at(now),
            "prices": [
                self.public_price(self.prices[ticker], now)
                for ticker in sorted(self.prices)
            ],
            "feeds": self.feed_status,
        }

    def load_cache(self) -> None:
        if not self.cache_path.exists():
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
        except (OSError, ValueError, TypeError, KeyError):
            self.prices = {}

    def save_cache(self) -> None:
        if not self.cache_dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "saved_at": utc_now().isoformat(),
            "prices": list(self.prices.values()),
        }, indent=2) + "\n")
        os.replace(temporary, self.cache_path)
        self.cache_dirty = False

    async def broadcast_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=5)
                await asyncio.sleep(0.2)
            self.changed.clear()
            if not self.clients:
                continue
            payload = self.payload("prices")
            disconnected: list[web.WebSocketResponse] = []
            for client in tuple(self.clients):
                try:
                    await client.send_json(payload)
                except (ConnectionError, RuntimeError):
                    disconnected.append(client)
            for client in disconnected:
                self.clients.discard(client)

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
        replay_start = utc_now() - timedelta(minutes=self.replay_minutes)
        client.subscribe(
            dataset=self.config.dataset,
            schema="ohlcv-1s",
            symbols=self.symbols,
            stype_in="raw_symbol",
            start=replay_start,
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
            now = now.replace(hour=23, minute=0)
        for index, (ticker, anchor) in enumerate(anchors.items()):
            wave = math.sin(phase + index * 0.43) * 0.0025
            store.update_price(
                ticker,
                anchor * (1 + offsets[ticker] + wave),
                now,
                "MOCK.LIVE",
                100 + index,
            )
        phase += 0.35
        await asyncio.sleep(1)


def tracked_symbols(snapshot: dict[str, Any], universe: dict[str, Any]) -> list[str]:
    return sorted({
        row["ticker"]
        for payload in (snapshot, universe)
        for row in payload["rows"]
    })


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
        )
        state_bundle = manager.ensure_state(
            plan,
            mode=args.refresh_mode,
            mock=args.mock,
        )

    snapshot = state_bundle["snapshot"]
    universe = state_bundle["today"]
    symbols = tracked_symbols(snapshot, universe)
    store = PriceStore(symbols=symbols, cache_path=args.cache)
    store.load_cache()
    stop = asyncio.Event()
    app = web.Application()
    app[TASKS_KEY] = []

    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        origin = request.headers.get("Origin")
        allowed_origins = set(args.allowed_origin or [])
        if allowed_origins and origin and origin not in allowed_origins:
            raise web.HTTPForbidden(text="WebSocket origin is not allowed")
        socket = web.WebSocketResponse(heartbeat=20, receive_timeout=60)
        await socket.prepare(request)
        store.clients.add(socket)
        await socket.send_json(store.payload())
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
        payload["dashboard_state"] = {
            "data_date": snapshot["data_date"],
            "status": state_bundle.get("cache", {}).get("status", "unknown"),
            "source": state_bundle.get("cache", {}).get("source", "unknown"),
        }
        return web.json_response(payload)

    async def state_handler(_: web.Request) -> web.Response:
        return web.json_response(state_bundle, headers={"Cache-Control": "no-store"})

    async def index_handler(_: web.Request) -> web.FileResponse:
        return web.FileResponse(args.dashboard / "index.html")

    async def on_startup(_: web.Application) -> None:
        tasks = [
            asyncio.create_task(store.broadcast_loop(stop), name="price-broadcast"),
            asyncio.create_task(store.cache_loop(stop), name="price-cache"),
        ]
        if args.mock:
            tasks.append(asyncio.create_task(mock_feed(universe, store, stop), name="mock-feed"))
        else:
            api_key = resolve_live_api_key()
            if not api_key:
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
                        api_key=api_key,
                        config=config,
                        symbols=symbols,
                        store=store,
                        replay_minutes=args.replay_minutes,
                        stop=stop,
                    )
                    tasks.append(asyncio.create_task(feed.run(), name=f"feed-{config.dataset}"))
        app[TASKS_KEY] = tasks

    async def on_cleanup(_: web.Application) -> None:
        stop.set()
        for client in tuple(store.clients):
            with contextlib.suppress(Exception):
                await client.close(
                    code=1001,
                    message=b"Sigma Tape adapter is shutting down",
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
    for asset in ("app.js", "live-calculations.js", "styles.css"):
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
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--day-dataset", default="EQUS.MINI")
    parser.add_argument("--overnight-dataset", default="OCEA.MEMOIR")
    parser.add_argument("--replay-minutes", type=int, default=60)
    parser.add_argument("--disable-overnight", action="store_true")
    parser.add_argument(
        "--refresh-mode",
        choices=("auto", "force", "never"),
        default="auto",
        help="Refresh stale dashboard state, always refresh, or only use cache.",
    )
    parser.add_argument("--market-cap-min", type=float, default=10_000_000_000)
    parser.add_argument("--daily-notional-min", type=float, default=25_000_000)
    parser.add_argument("--candidate-target", type=int, default=300)
    parser.add_argument("--result-limit", type=int, default=20)
    parser.add_argument(
        "--spot-dataset",
        choices=("EQUS.MINI", "EQUS.SUMMARY"),
        default="EQUS.MINI",
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mock and args.cache == DEFAULT_CACHE:
        args.cache = DEFAULT_CACHE.with_name("mock-live-prices.json")
    web.run_app(
        create_app(args),
        host=args.host,
        port=args.port,
        print=lambda message: print(message.replace("======== Running on", "Sigma Tape running on")),
    )


if __name__ == "__main__":
    main()
