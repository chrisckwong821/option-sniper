#!/usr/bin/env python3
"""Load-test Option Sniper's WebSocket fan-out with concurrent viewers."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector, WSMsgType


@dataclass
class ViewerResult:
    connected: bool = False
    snapshots: int = 0
    deltas: int = 0
    statuses: int = 0
    symbol_updates: int = 0
    snapshot_bytes: int = 0
    delta_bytes: int = 0
    sequence_gaps: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    error: str | None = None


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def health_url_for(websocket_url: str) -> str:
    parsed = urlsplit(websocket_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "/api/health", "", ""))


def message_latency_ms(server_time: str | None) -> float | None:
    if not server_time:
        return None
    try:
        sent = datetime.fromisoformat(server_time).astimezone(UTC)
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - sent).total_seconds() * 1000)


def parse_server_time(server_time: str | None) -> datetime | None:
    if not server_time:
        return None
    try:
        return datetime.fromisoformat(server_time).astimezone(UTC)
    except ValueError:
        return None


async def fetch_health(session: ClientSession, url: str) -> dict[str, Any]:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def run_viewer(
    viewer_id: int,
    session: ClientSession,
    websocket_url: str,
    start: asyncio.Event,
    stop: asyncio.Event,
    ready: asyncio.Queue[int],
    connect_timeout: float,
    ramp_delay: float,
    origin: str | None,
    compression: int,
) -> ViewerResult:
    result = ViewerResult()
    if ramp_delay:
        await asyncio.sleep(ramp_delay)
    headers = {"Origin": origin} if origin else None
    try:
        async with session.ws_connect(
            websocket_url,
            headers=headers,
            heartbeat=20,
            compress=compression,
            max_msg_size=0,
        ) as socket:
            first = await socket.receive(timeout=connect_timeout)
            if first.type != WSMsgType.TEXT:
                raise RuntimeError(f"Expected snapshot text, received {first.type.name}")
            payload = json.loads(first.data)
            if payload.get("type") != "snapshot":
                raise RuntimeError(f"Expected snapshot, received {payload.get('type')}")
            result.connected = True
            result.snapshots = 1
            result.snapshot_bytes = len(first.data.encode("utf-8"))
            sequence = int(payload["sequence"])
            ready.put_nowait(viewer_id)
            await start.wait()
            measurement_started_at = datetime.now(UTC)

            while not stop.is_set():
                try:
                    message = await socket.receive(timeout=1)
                except asyncio.TimeoutError:
                    continue
                if message.type != WSMsgType.TEXT:
                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        raise RuntimeError(f"Socket closed during load: {message.type.name}")
                    continue
                payload = json.loads(message.data)
                message_type = payload.get("type")
                incoming_sequence = int(payload.get("sequence", -1))
                if message_type == "snapshot":
                    result.snapshots += 1
                    result.snapshot_bytes += len(message.data.encode("utf-8"))
                    sequence = incoming_sequence
                elif message_type == "delta":
                    if incoming_sequence != sequence + 1:
                        result.sequence_gaps += 1
                    sequence = max(sequence, incoming_sequence)
                    sent_at = parse_server_time(payload.get("server_time"))
                    if sent_at is not None and sent_at < measurement_started_at:
                        continue
                    result.deltas += 1
                    result.delta_bytes += len(message.data.encode("utf-8"))
                    result.symbol_updates += len(payload.get("prices", []))
                    latency = message_latency_ms(payload.get("server_time"))
                    if latency is not None:
                        result.latencies_ms.append(latency)
                elif message_type == "status":
                    result.statuses += 1
    except Exception as exc:
        result.error = f"viewer {viewer_id}: {type(exc).__name__}: {exc}"
    return result


def stream_delta(after: dict[str, Any], before: dict[str, Any], key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def summarize(
    results: list[ViewerResult],
    duration: float,
    requested_viewers: int,
    minimum_deltas: int,
    max_p95_latency_ms: float,
    before_stream: dict[str, Any],
    after_stream: dict[str, Any],
) -> dict[str, Any]:
    connected = [result for result in results if result.connected]
    latencies = [latency for result in connected for latency in result.latencies_ms]
    delta_counts = [result.deltas for result in connected]
    total_bytes = sum(result.snapshot_bytes + result.delta_bytes for result in connected)
    total_delta_bytes = sum(result.delta_bytes for result in connected)
    errors = [result.error for result in results if result.error]
    p95_latency = percentile(latencies, 95)
    min_deltas = min(delta_counts, default=0)
    sequence_gaps = sum(result.sequence_gaps for result in connected)
    send_failures = stream_delta(after_stream, before_stream, "send_failures")
    passed = (
        len(connected) == requested_viewers
        and not errors
        and sequence_gaps == 0
        and send_failures == 0
        and min_deltas >= minimum_deltas
        and p95_latency is not None
        and p95_latency <= max_p95_latency_ms
    )
    return {
        "passed": passed,
        "requested_viewers": requested_viewers,
        "connected_viewers": len(connected),
        "duration_seconds": duration,
        "errors": errors[:20],
        "sequence_gaps": sequence_gaps,
        "messages_per_viewer": {
            "delta_min": min_deltas,
            "delta_median": statistics.median(delta_counts) if delta_counts else 0,
            "delta_max": max(delta_counts, default=0),
            "required_minimum": minimum_deltas,
        },
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": p95_latency,
            "p99": percentile(latencies, 99),
            "max": max(latencies, default=None),
            "required_p95_max": max_p95_latency_ms,
        },
        "application_payload": {
            "snapshot_and_delta_mb": total_bytes / 1_000_000,
            "delta_mb": total_delta_bytes / 1_000_000,
            "aggregate_mbps": total_delta_bytes * 8 / duration / 1_000_000,
            "average_kbps_per_viewer": (
                total_delta_bytes * 8 / duration / len(connected) / 1000
                if connected else 0
            ),
            "symbol_updates": sum(result.symbol_updates for result in connected),
        },
        "server": {
            "delta_broadcasts": stream_delta(after_stream, before_stream, "delta_broadcasts"),
            "fanout_messages": stream_delta(after_stream, before_stream, "fanout_messages"),
            "fanout_mb": stream_delta(after_stream, before_stream, "fanout_bytes") / 1_000_000,
            "send_failures": send_failures,
            "max_batch_symbols": after_stream.get("max_batch_symbols", 0),
            "last_message_bytes": after_stream.get("last_message_bytes", 0),
            "last_broadcast_duration_ms": after_stream.get("last_broadcast_duration_ms", 0),
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    timeout = ClientTimeout(total=None, sock_connect=args.connect_timeout)
    connector = TCPConnector(limit=0, ttl_dns_cache=300)
    async with ClientSession(timeout=timeout, connector=connector) as session:
        health_url = args.health_url or health_url_for(args.url)
        before = await fetch_health(session, health_url)
        start = asyncio.Event()
        stop = asyncio.Event()
        ready: asyncio.Queue[int] = asyncio.Queue()
        compression = 0 if args.no_compression else 15
        tasks = [asyncio.create_task(run_viewer(
            viewer_id=index,
            session=session,
            websocket_url=args.url,
            start=start,
            stop=stop,
            ready=ready,
            connect_timeout=args.connect_timeout,
            ramp_delay=(index / max(1, args.viewers - 1)) * args.ramp_seconds,
            origin=args.origin,
            compression=compression,
        )) for index in range(args.viewers)]

        connection_deadline = perf_counter() + args.connect_timeout + args.ramp_seconds
        ready_count = 0
        while ready_count < args.viewers and perf_counter() < connection_deadline:
            try:
                await asyncio.wait_for(
                    ready.get(),
                    timeout=max(0.01, connection_deadline - perf_counter()),
                )
            except asyncio.TimeoutError:
                break
            ready_count += 1

        start.set()
        started = perf_counter()
        await asyncio.sleep(args.duration)
        after = await fetch_health(session, health_url)
        measured_duration = max(args.duration, perf_counter() - started)
        stop.set()
        results = await asyncio.gather(*tasks)

    minimum_deltas = max(1, math.floor(args.duration * args.min_delivery_ratio))
    return summarize(
        results=results,
        duration=measured_duration,
        requested_viewers=args.viewers,
        minimum_deltas=minimum_deltas,
        max_p95_latency_ms=args.max_p95_latency_ms,
        before_stream=before.get("stream", {}),
        after_stream=after.get("stream", {}),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8876/ws/prices")
    parser.add_argument("--health-url")
    parser.add_argument("--viewers", type=int, default=300)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--ramp-seconds", type=float, default=5)
    parser.add_argument("--connect-timeout", type=float, default=20)
    parser.add_argument("--max-p95-latency-ms", type=float, default=250)
    parser.add_argument("--min-delivery-ratio", type=float, default=0.8)
    parser.add_argument("--origin")
    parser.add_argument("--no-compression", action="store_true")
    return parser.parse_args()


def main() -> int:
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
