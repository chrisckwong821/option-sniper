#!/usr/bin/env python3
"""Send one Discord alert per ticker when it crosses the daily +Z threshold."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType


EASTERN = ZoneInfo("America/New_York")
ALERT_CYCLE_START = time(4)
ALERT_SESSIONS = frozenset({"premarket", "regular", "afterhours", "overnight"})
DEFAULT_BACKEND_URL = "http://backend:8765"
DEFAULT_DATABASE = Path("/app/.cache/sigma-tape/discord-alerts.sqlite3")
DEFAULT_DISCORD_API = "https://discord.com/api/v10"
DEFAULT_DISCORD_CHANNEL_ID = "1544929277009465374"
DEFAULT_THRESHOLD = 1.5
Z_SCORE_EPSILON = 1e-9


def ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def alert_cycle_date_at(value: datetime) -> str:
    """Return the ET date for the alert cycle beginning at 04:00 ET."""
    eastern = ensure_aware(value).astimezone(EASTERN)
    if eastern.time().replace(tzinfo=None) < ALERT_CYCLE_START:
        eastern -= timedelta(days=1)
    return eastern.date().isoformat()


def calculate_z_score(price: Any, anchor_price: Any, sigma_pct: Any) -> float | None:
    try:
        current = float(price)
        anchor = float(anchor_price)
        sigma = float(sigma_pct) / 100
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (current, anchor, sigma)):
        return None
    return math.log(current / anchor) / sigma


def websocket_url(backend_url: str) -> str:
    parsed = urlsplit(backend_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/ws/prices", "", ""))


def state_url(backend_url: str) -> str:
    return f"{backend_url.rstrip('/')}/api/state"


@dataclass(frozen=True)
class AlertUniverse:
    target_session: str
    rows: dict[str, dict[str, Any]]


def build_universe(payload: dict[str, Any]) -> AlertUniverse:
    target_session = str(payload.get("refresh_plan", {}).get("next_session") or "")
    rows = {
        str(row["ticker"]): row
        for row in payload.get("today", {}).get("rows", [])
        if row.get("ticker")
    }
    if not target_session:
        raise ValueError("Dashboard state is missing refresh_plan.next_session")
    if not rows:
        raise ValueError("Dashboard state has no live-universe rows")
    return AlertUniverse(target_session=target_session, rows=rows)


@dataclass(frozen=True)
class AlertObservation:
    trading_date: str
    ticker: str
    company_name: str
    session: str
    observed_at: str
    price: float
    anchor_price: float
    sigma_pct: float
    z_score: float
    iv_reliability: str

    def event_key(self, threshold: float) -> str:
        return f"{self.trading_date}:{self.ticker}:+{threshold:g}"


class AlertStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                trading_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                threshold REAL NOT NULL,
                observed_at TEXT NOT NULL,
                price REAL NOT NULL,
                z_score REAL NOT NULL,
                status TEXT NOT NULL,
                discord_message_id TEXT,
                last_error TEXT,
                PRIMARY KEY (trading_date, ticker, threshold)
            )
            """
        )
        self.connection.commit()
        self.connection.execute(
            "UPDATE alerts SET status = 'uncertain', "
            "last_error = 'worker stopped while Discord delivery was in flight' "
            "WHERE status = 'sending'"
        )
        self.connection.commit()

    def reserve(self, observation: AlertObservation, threshold: float) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO alerts (
                trading_date, ticker, threshold, observed_at, price, z_score, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'sending')
            """,
            (
                observation.trading_date,
                observation.ticker,
                threshold,
                observation.observed_at,
                observation.price,
                observation.z_score,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_sent(
        self,
        observation: AlertObservation,
        threshold: float,
        message_id: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE alerts
            SET status = 'sent', discord_message_id = ?, last_error = NULL
            WHERE trading_date = ? AND ticker = ? AND threshold = ?
            """,
            (message_id, observation.trading_date, observation.ticker, threshold),
        )
        self.connection.commit()

    def mark_uncertain(
        self,
        observation: AlertObservation,
        threshold: float,
        error: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE alerts
            SET status = 'uncertain', last_error = ?
            WHERE trading_date = ? AND ticker = ? AND threshold = ?
            """,
            (error[:500], observation.trading_date, observation.ticker, threshold),
        )
        self.connection.commit()

    def release(
        self,
        observation: AlertObservation,
        threshold: float,
        error: str,
    ) -> None:
        self.connection.execute(
            """
            DELETE FROM alerts
            WHERE trading_date = ? AND ticker = ? AND threshold = ? AND status = 'sending'
            """,
            (observation.trading_date, observation.ticker, threshold),
        )
        self.connection.commit()
        print(
            json.dumps({
                "event": "discord_alert_released",
                "ticker": observation.ticker,
                "trading_date": observation.trading_date,
                "error": error,
            }),
            flush=True,
        )

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])

    def summary(self, trading_date: str) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT ticker, status
            FROM alerts
            WHERE trading_date = ?
            ORDER BY ticker
            """,
            (trading_date,),
        ).fetchall()
        statuses: dict[str, int] = {}
        for _, status in rows:
            statuses[status] = statuses.get(status, 0) + 1
        return {
            "count": len(rows),
            "statuses": statuses,
            "sent_tickers": [ticker for ticker, status in rows if status == "sent"],
        }

    def close(self) -> None:
        self.connection.close()


class DefinitiveSendError(RuntimeError):
    """Discord explicitly rejected a request and did not create a message."""


class UncertainSendError(RuntimeError):
    """Delivery may have succeeded, so retrying later could create a duplicate."""


class DiscordSender:
    def __init__(
        self,
        session: ClientSession,
        token: str,
        channel_id: str,
        api_base: str = DEFAULT_DISCORD_API,
    ) -> None:
        self.session = session
        self.token = token
        self.channel_id = channel_id
        self.api_base = api_base.rstrip("/")

    async def send(self, observation: AlertObservation, threshold: float) -> str:
        event_key = observation.event_key(threshold)
        nonce = hashlib.sha256(event_key.encode()).hexdigest()[:25]
        payload = {
            "nonce": nonce,
            "enforce_nonce": True,
            "allowed_mentions": {"parse": []},
            "embeds": [{
                "title": f"+{threshold:g}σ crossing — {observation.ticker}",
                "description": observation.company_name,
                "color": 0x2ECC71,
                "fields": [
                    {"name": "Z-score", "value": f"+{observation.z_score:.3f}", "inline": True},
                    {"name": "Price", "value": f"${observation.price:,.2f}", "inline": True},
                    {"name": "Anchor", "value": f"${observation.anchor_price:,.2f}", "inline": True},
                    {"name": "Expected σ", "value": f"{observation.sigma_pct:.2f}%", "inline": True},
                    {"name": "Session", "value": observation.session.title(), "inline": True},
                    {"name": "IV reliability", "value": observation.iv_reliability.title(), "inline": True},
                ],
                "footer": {"text": f"Option Sniper · {event_key}"},
                "timestamp": observation.observed_at,
            }],
        }
        url = f"{self.api_base}/channels/{self.channel_id}/messages"
        headers = {"Authorization": f"Bot {self.token}"}

        for attempt in range(1, 5):
            try:
                async with self.session.post(url, headers=headers, json=payload) as response:
                    body = await response.text()
                    if 200 <= response.status < 300:
                        decoded = json.loads(body)
                        message_id = str(decoded.get("id") or "")
                        if not message_id:
                            raise UncertainSendError("Discord accepted the request without a message id")
                        return message_id
                    if response.status == 429:
                        try:
                            retry_after = float(json.loads(body).get("retry_after", 1))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            retry_after = 1
                        await asyncio.sleep(min(30, max(0.1, retry_after)))
                        continue
                    if 400 <= response.status < 500:
                        raise DefinitiveSendError(
                            f"Discord returned HTTP {response.status}: {body[:300]}"
                        )
                    raise UncertainSendError(
                        f"Discord returned HTTP {response.status}: {body[:300]}"
                    )
            except DefinitiveSendError:
                raise
            except UncertainSendError:
                raise
            except (ClientError, asyncio.TimeoutError) as exc:
                if attempt == 4:
                    raise UncertainSendError(f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(min(8, 2 ** (attempt - 1)))

        raise UncertainSendError("Discord rate limit did not clear after four attempts")


SendAlert = Callable[[AlertObservation, float], Awaitable[str]]


class AlertEngine:
    def __init__(
        self,
        store: AlertStore,
        send_alert: SendAlert,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError("Alert threshold must be a positive number")
        self.store = store
        self.send_alert = send_alert
        self.threshold = threshold
        self.previous_z: dict[tuple[str, str], float] = {}

    async def process_message(
        self,
        message: dict[str, Any],
        universe: AlertUniverse,
    ) -> int:
        if message.get("type") not in {"snapshot", "delta"}:
            return 0
        sent = 0
        for point in message.get("prices", []):
            ticker = str(point.get("ticker") or "")
            row = universe.rows.get(ticker)
            session = str(point.get("session") or "")
            if row is None or session not in ALERT_SESSIONS or point.get("is_stale"):
                continue
            try:
                observed = datetime.fromisoformat(str(point["ts_event"]))
            except (KeyError, TypeError, ValueError):
                continue
            trading_date = alert_cycle_date_at(observed)
            if trading_date != universe.target_session:
                continue
            anchor = row.get("live_anchor_price") or row.get("anchor_price")
            sigma = row.get("sigma_1d_pct")
            z_score = calculate_z_score(point.get("price"), anchor, sigma)
            if z_score is None:
                continue

            key = (trading_date, ticker)
            previous = self.previous_z.get(key, 0.0)
            self.previous_z[key] = z_score
            if not (
                previous < self.threshold - Z_SCORE_EPSILON
                and z_score >= self.threshold - Z_SCORE_EPSILON
            ):
                continue

            observation = AlertObservation(
                trading_date=trading_date,
                ticker=ticker,
                company_name=str(row.get("company_name") or ticker),
                session=session,
                observed_at=ensure_aware(observed).astimezone(UTC).isoformat(),
                price=float(point["price"]),
                anchor_price=float(anchor),
                sigma_pct=float(sigma),
                z_score=z_score,
                iv_reliability=str(row.get("iv_reliability") or "unknown"),
            )
            if not self.store.reserve(observation, self.threshold):
                continue
            try:
                message_id = await self.send_alert(observation, self.threshold)
            except DefinitiveSendError as exc:
                self.store.release(observation, self.threshold, str(exc))
                continue
            except Exception as exc:
                self.store.mark_uncertain(observation, self.threshold, str(exc))
                print(
                    json.dumps({
                        "event": "discord_alert_uncertain",
                        "ticker": ticker,
                        "trading_date": trading_date,
                        "error": str(exc),
                    }),
                    flush=True,
                )
                continue
            self.store.mark_sent(observation, self.threshold, message_id)
            sent += 1
            print(
                json.dumps({
                    "event": "discord_alert_sent",
                    "ticker": ticker,
                    "trading_date": trading_date,
                    "z_score": round(z_score, 4),
                    "discord_message_id": message_id,
                }),
                flush=True,
            )
        return sent


@dataclass(frozen=True)
class Config:
    backend_url: str
    database: Path
    discord_token: str
    discord_channel_id: str
    discord_api_base: str
    threshold: float


def parse_args() -> Config:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend-url",
        default=os.environ.get("SIGMA_BACKEND_URL", DEFAULT_BACKEND_URL),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DISCORD_ALERT_DATABASE", DEFAULT_DATABASE)),
    )
    parser.add_argument(
        "--discord-api-base",
        default=os.environ.get("DISCORD_API_BASE_URL", DEFAULT_DISCORD_API),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("ZSCORE_ALERT_THRESHOLD") or DEFAULT_THRESHOLD),
    )
    args = parser.parse_args()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", DEFAULT_DISCORD_CHANNEL_ID).strip()
    if not token:
        parser.error("DISCORD_BOT_TOKEN is required")
    return Config(
        backend_url=args.backend_url,
        database=args.database,
        discord_token=token,
        discord_channel_id=channel_id,
        discord_api_base=args.discord_api_base,
        threshold=args.threshold,
    )


async def load_state(session: ClientSession, backend_url: str) -> AlertUniverse:
    async with session.get(state_url(backend_url)) as response:
        response.raise_for_status()
        return build_universe(await response.json())


async def run(config: Config) -> None:
    store = AlertStore(config.database)
    timeout = ClientTimeout(total=20, connect=10, sock_read=60)
    try:
        async with ClientSession(timeout=timeout) as session:
            sender = DiscordSender(
                session,
                config.discord_token,
                config.discord_channel_id,
                config.discord_api_base,
            )
            engine = AlertEngine(store, sender.send, config.threshold)
            backoff = 1
            while True:
                try:
                    universe = await load_state(session, config.backend_url)
                    print(
                        json.dumps({
                            "event": "alert_stream_connecting",
                            "target_session": universe.target_session,
                            "tracked_symbols": len(universe.rows),
                            "existing_alerts": store.count(),
                            "cycle_ledger": store.summary(universe.target_session),
                        }),
                        flush=True,
                    )
                    async with session.ws_connect(
                        websocket_url(config.backend_url),
                        heartbeat=20,
                        receive_timeout=90,
                    ) as socket:
                        backoff = 1
                        async for message in socket:
                            if message.type == WSMsgType.TEXT:
                                await engine.process_message(json.loads(message.data), universe)
                            elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(
                        json.dumps({
                            "event": "alert_stream_retrying",
                            "error": f"{type(exc).__name__}: {exc}",
                            "retry_seconds": backoff,
                        }),
                        flush=True,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(30, backoff * 2)
    finally:
        store.close()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
