#!/usr/bin/env python3
"""Preflight and sample a Databento OPRA historical IV backfill."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import databento as db
import exchange_calendars as xcals
import pandas as pd

try:
    from .databento_config import resolve_historical_api_keys
    from .nasdaq_universe_movers import (
        estimate_option_iv,
        normalize_symbol,
        parse_occ_symbol,
        prepare_option_pairs,
    )
except ImportError:  # Support direct execution with `python scripts/...`.
    from databento_config import resolve_historical_api_keys
    from nasdaq_universe_movers import (
        estimate_option_iv,
        normalize_symbol,
        parse_occ_symbol,
        prepare_option_pairs,
    )


SECONDS_PER_YEAR = 365 * 24 * 60 * 60
XNYS = xcals.get_calendar("XNYS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate the Databento cost for a daily OPRA CBBO snapshot backfill "
            "and optionally download a tiny sample to derive ATM option IV."
        )
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=["AAPL"],
        help="Tickers to probe. Supports comma-separated values.",
    )
    parser.add_argument(
        "--ticker-source",
        type=Path,
        help=(
            "Optional JSON source containing rows[].ticker, today.rows[].ticker, "
            "snapshot.rows[].ticker, candidate_manifest[].ticker, or holdings[].symbol."
        ),
    )
    parser.add_argument("--ticker-limit", type=int, help="Limit tickers loaded from source.")
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--start-date", type=pd.Timestamp)
    parser.add_argument("--end-date", type=pd.Timestamp)
    parser.add_argument("--snapshot-time", default="15:59:00")
    parser.add_argument("--snapshot-duration-seconds", type=int, default=61)
    parser.add_argument("--schema", default="cbbo-1m")
    parser.add_argument("--spot-dataset", default="EQUS.MINI")
    parser.add_argument("--spot-schema", default="ohlcv-1m")
    parser.add_argument("--target-dte", type=int, default=30)
    parser.add_argument("--min-dte", type=int, default=7)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument(
        "--cost-sample-sessions",
        type=int,
        default=8,
        help="Number of trading sessions to preflight and extrapolate from.",
    )
    parser.add_argument(
        "--download-sample-sessions",
        type=int,
        default=0,
        help="Download and derive IV for this many sampled sessions.",
    )
    parser.add_argument(
        "--max-download-cost",
        type=float,
        default=1.0,
        help="Abort sample download if preflight cost exceeds this amount.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--dry-run-plan",
        action="store_true",
        help="Print request shape without requiring Databento credentials.",
    )
    return parser.parse_args()


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        symbol = normalize_symbol(value)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def parse_tickers(values: Iterable[str] | None) -> list[str]:
    if not values:
        return []
    pieces = []
    for value in values:
        pieces.extend(part.strip() for part in str(value).split(","))
    return unique_preserving_order(pieces)


def _rows_at_path(payload: object, path: tuple[str, ...]) -> list[dict]:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return current if isinstance(current, list) else []


def load_tickers_from_source(path: Path, limit: int | None = None) -> list[str]:
    payload = json.loads(path.read_text())
    candidate_paths = [
        ("rows",),
        ("today", "rows"),
        ("snapshot", "rows"),
        ("candidate_manifest",),
        ("holdings",),
    ]
    tickers = []
    for candidate_path in candidate_paths:
        for row in _rows_at_path(payload, candidate_path):
            if not isinstance(row, dict):
                continue
            value = row.get("ticker") or row.get("symbol")
            if value:
                tickers.append(str(value))
        if tickers:
            break
    resolved = unique_preserving_order(tickers)
    return resolved[:limit] if limit else resolved


def resolve_ticker_list(
    tickers: Iterable[str] | None,
    ticker_source: Path | None,
    ticker_limit: int | None,
) -> list[str]:
    explicit = parse_tickers(tickers)
    sourced = load_tickers_from_source(ticker_source, ticker_limit) if ticker_source else []
    result = unique_preserving_order([*explicit, *sourced])
    if not result:
        raise ValueError("No tickers resolved for the IV backfill probe")
    return result


def default_end_date() -> date:
    now = pd.Timestamp.now(tz="America/New_York")
    candidate = now.date()
    if now.hour < 17:
        candidate = (pd.Timestamp(candidate) - pd.Timedelta(days=1)).date()
    sessions = XNYS.sessions_in_range(
        (pd.Timestamp(candidate) - pd.Timedelta(days=14)).date(),
        candidate,
    )
    if len(sessions) == 0:
        raise ValueError("Could not resolve a recent completed XNYS session")
    return sessions[-1].date()


def trading_sessions(
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp | None,
    years: int,
) -> list[date]:
    end = end_date.date() if end_date is not None else default_end_date()
    if start_date is None:
        start = (pd.Timestamp(end) - pd.DateOffset(years=years)).date()
    else:
        start = start_date.date()
    sessions = XNYS.sessions_in_range(start, end)
    return [session.date() for session in sessions]


def pick_evenly(values: list[date], count: int) -> list[date]:
    if count <= 0 or not values:
        return []
    if count == 1:
        return [values[len(values) // 2]]
    if count >= len(values):
        return values
    indexes = [
        round(index * (len(values) - 1) / (count - 1))
        for index in range(count)
    ]
    result = []
    seen = set()
    for index in indexes:
        if index in seen:
            continue
        seen.add(index)
        result.append(values[index])
    return result


def snapshot_bounds(
    session: date,
    snapshot_time: str,
    duration_seconds: int,
) -> tuple[str, str]:
    start = pd.Timestamp(f"{session} {snapshot_time}", tz="America/New_York")
    end = start + pd.Timedelta(seconds=duration_seconds)
    return start.isoformat(), end.isoformat()


def option_request(
    tickers: list[str],
    start: str,
    end: str,
    schema: str,
) -> dict[str, object]:
    return {
        "dataset": "OPRA.PILLAR",
        "schema": schema,
        "stype_in": "parent",
        "symbols": [f"{ticker}.OPT" for ticker in tickers],
        "start": start,
        "end": end,
    }


def spot_request(
    tickers: list[str],
    start: str,
    end: str,
    dataset: str,
    schema: str,
) -> dict[str, object]:
    return {
        "dataset": dataset,
        "schema": schema,
        "stype_in": "raw_symbol",
        "symbols": tickers,
        "start": start,
        "end": end,
    }


def request_plan(
    tickers: list[str],
    sessions: list[date],
    sampled_sessions: list[date],
    args: argparse.Namespace,
) -> dict[str, object]:
    example_start, example_end = snapshot_bounds(
        sampled_sessions[0] if sampled_sessions else sessions[0],
        args.snapshot_time,
        args.snapshot_duration_seconds,
    )
    return {
        "dataset": "OPRA.PILLAR",
        "schema": args.schema,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "session_count": len(sessions),
        "first_session": str(sessions[0]) if sessions else None,
        "last_session": str(sessions[-1]) if sessions else None,
        "sampled_sessions": [str(session) for session in sampled_sessions],
        "option_request_example": option_request(tickers, example_start, example_end, args.schema),
        "spot_request_example": spot_request(
            tickers,
            example_start,
            example_end,
            args.spot_dataset,
            args.spot_schema,
        ),
    }


def historical_unit_price(unit_prices: list[dict], schema: str) -> float | None:
    for row in unit_prices:
        if row.get("mode") == "historical":
            prices = row.get("unit_prices", {})
            if isinstance(prices, dict) and schema in prices:
                return float(prices[schema])
    return None


def cost_preflight(
    option_client,
    spot_client,
    tickers: list[str],
    sessions: list[date],
    sampled_sessions: list[date],
    args: argparse.Namespace,
) -> dict[str, object]:
    unit_prices = option_client.metadata.list_unit_prices(dataset="OPRA.PILLAR")
    samples = []
    for session in sampled_sessions:
        start, end = snapshot_bounds(session, args.snapshot_time, args.snapshot_duration_seconds)
        option_cost = float(
            option_client.metadata.get_cost(**option_request(tickers, start, end, args.schema))
        )
        spot_cost = float(
            spot_client.metadata.get_cost(
                **spot_request(tickers, start, end, args.spot_dataset, args.spot_schema)
            )
        )
        samples.append({
            "session": str(session),
            "option_cost_usd": option_cost,
            "spot_cost_usd": spot_cost,
            "total_cost_usd": option_cost + spot_cost,
        })
    average_option = sum(sample["option_cost_usd"] for sample in samples) / len(samples)
    average_spot = sum(sample["spot_cost_usd"] for sample in samples) / len(samples)
    estimated_option = average_option * len(sessions)
    estimated_spot = average_spot * len(sessions)
    estimated_total = estimated_option + estimated_spot
    return {
        "unit_price_historical_usd_per_gb": historical_unit_price(unit_prices, args.schema),
        "sample_count": len(samples),
        "samples": samples,
        "estimated_coverage_option_cost_usd": estimated_option,
        "estimated_coverage_spot_cost_usd": estimated_spot,
        "estimated_coverage_total_cost_usd": estimated_total,
        "estimated_per_ticker_total_cost_usd": estimated_total / len(tickers),
        "notes": [
            "Cost is extrapolated from sampled trading sessions, so use an exact full preflight before production.",
            "Databento warns get_cost may over-report ranges that are not discrete 10-minute multiples.",
        ],
    }


def parsed_symbols_for_ticker(quotes: pd.DataFrame, ticker: str) -> list[dict]:
    if quotes.empty or "symbol" not in quotes.columns:
        return []
    target = normalize_symbol(ticker)
    parsed = []
    for symbol in quotes["symbol"].dropna().astype(str).unique():
        item = parse_occ_symbol(symbol)
        if item and normalize_symbol(item["root"]) == target:
            parsed.append({**item, "symbol": symbol})
    return parsed


def quotes_for_ticker(quotes: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if quotes.empty or "symbol" not in quotes.columns:
        return quotes
    target = normalize_symbol(ticker)
    keep = []
    for symbol in quotes["symbol"].astype(str):
        item = parse_occ_symbol(symbol)
        keep.append(bool(item and normalize_symbol(item["root"]) == target))
    return quotes.loc[keep].copy()


def choose_target_expiry(
    quotes: pd.DataFrame,
    ticker: str,
    session: date,
    target_dte: int,
    min_dte: int,
    max_dte: int,
) -> date | None:
    expiries = {}
    for item in parsed_symbols_for_ticker(quotes, ticker):
        dte = (item["expiry"] - session).days
        if dte <= 0:
            continue
        if min_dte <= dte <= max_dte:
            expiries.setdefault(item["expiry"], set()).add(item["side"])
    paired = [expiry for expiry, sides in expiries.items() if {"C", "P"} <= sides]
    if not paired:
        return None
    return min(paired, key=lambda expiry: (abs((expiry - session).days - target_dte), expiry))


def latest_spot(spot_bars: pd.DataFrame, ticker: str) -> float | None:
    if spot_bars.empty or "close" not in spot_bars.columns:
        return None
    bars = spot_bars
    if "symbol" in bars.columns:
        bars = bars[bars["symbol"].astype(str).map(normalize_symbol) == normalize_symbol(ticker)]
    if bars.empty:
        return None
    timestamp_col = bars.columns[0]
    bars = bars.sort_values(timestamp_col)
    close = float(bars.iloc[-1]["close"])
    return close if math.isfinite(close) and close > 0 else None


def derive_iv_snapshot(
    ticker: str,
    session: date,
    snapshot_start: str,
    spot_bars: pd.DataFrame,
    option_quotes: pd.DataFrame,
    target_dte: int,
    min_dte: int,
    max_dte: int,
) -> dict[str, object]:
    spot = latest_spot(spot_bars, ticker)
    ticker_quotes = quotes_for_ticker(option_quotes, ticker)
    expiry = choose_target_expiry(
        ticker_quotes,
        ticker,
        session,
        target_dte,
        min_dte,
        max_dte,
    )
    quote_count = len(ticker_quotes)
    if spot is None or expiry is None:
        return {
            "ticker": ticker,
            "session": str(session),
            "status": "no_iv",
            "spot": spot,
            "expiry": str(expiry) if expiry else None,
            "option_quote_records": quote_count,
        }

    pairs_by_ticker, diagnostics = prepare_option_pairs(
        ticker_quotes,
        [{"ticker": ticker}],
        expiry,
    )
    cutoff = pd.Timestamp(snapshot_start)
    expiry_close = pd.Timestamp(f"{expiry} 16:00:00", tz="America/New_York")
    year_fraction = (expiry_close - cutoff).total_seconds() / SECONDS_PER_YEAR
    estimate = estimate_option_iv(
        {"ticker": ticker, "anchor_price": spot},
        pairs_by_ticker.get(ticker, []),
        year_fraction,
    )
    if estimate is None:
        return {
            "ticker": ticker,
            "session": str(session),
            "status": "no_iv",
            "spot": spot,
            "expiry": str(expiry),
            "dte": (expiry - session).days,
            "option_quote_records": quote_count,
            "paired_strikes": len(pairs_by_ticker.get(ticker, [])),
            "diagnostics": diagnostics,
        }
    return {
        "ticker": ticker,
        "session": str(session),
        "status": "valid_iv",
        "spot": spot,
        "expiry": str(expiry),
        "dte": (expiry - session).days,
        "option_quote_records": quote_count,
        "paired_strikes": len(pairs_by_ticker.get(ticker, [])),
        "diagnostics": diagnostics,
        "iv": estimate,
    }


def sample_download(
    option_client,
    spot_client,
    tickers: list[str],
    sampled_sessions: list[date],
    args: argparse.Namespace,
) -> list[dict[str, object]]:
    results = []
    for session in sampled_sessions[: args.download_sample_sessions]:
        start, end = snapshot_bounds(session, args.snapshot_time, args.snapshot_duration_seconds)
        option_payload = option_request(tickers, start, end, args.schema)
        spot_payload = spot_request(tickers, start, end, args.spot_dataset, args.spot_schema)
        option_cost = float(option_client.metadata.get_cost(**option_payload))
        spot_cost = float(spot_client.metadata.get_cost(**spot_payload))
        if option_cost + spot_cost > args.max_download_cost:
            raise RuntimeError(
                f"Sample download preflight ${option_cost + spot_cost:.4f} exceeds "
                f"--max-download-cost ${args.max_download_cost:.4f}"
            )
        spot_bars = spot_client.timeseries.get_range(**spot_payload).to_df().reset_index()
        option_quotes = option_client.timeseries.get_range(**option_payload).to_df().reset_index()
        for ticker in tickers:
            results.append(
                derive_iv_snapshot(
                    ticker,
                    session,
                    start,
                    spot_bars,
                    option_quotes,
                    args.target_dte,
                    args.min_dte,
                    args.max_dte,
                )
            )
    return results


def write_or_print(payload: dict[str, object], output: Path | None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)
    print(rendered, end="")


def main() -> int:
    args = parse_args()
    tickers = resolve_ticker_list(args.tickers, args.ticker_source, args.ticker_limit)
    sessions = trading_sessions(args.start_date, args.end_date, args.years)
    if not sessions:
        raise SystemExit("No XNYS trading sessions found for the requested range.")
    sampled_sessions = pick_evenly(sessions, args.cost_sample_sessions)
    payload = {
        "schema_version": "1.0",
        "plan": request_plan(tickers, sessions, sampled_sessions, args),
    }

    if args.dry_run_plan:
        payload["status"] = "dry_run_plan"
        write_or_print(payload, args.output)
        return 0

    spot_key, option_key = resolve_historical_api_keys()
    if not spot_key or not option_key:
        payload["status"] = "missing_credentials"
        payload["error"] = (
            "Set DATABENTO_LIVE_API_KEY and "
            "DATABENTO_HISTORICAL_OPTIONS_API_KEY, or use the legacy "
            "DATABENTO_OPTIONS_API_KEY/DATABENTO_API_KEY fallbacks."
        )
        write_or_print(payload, args.output)
        return 2

    spot_client = db.Historical(spot_key)
    option_client = db.Historical(option_key)
    payload["status"] = "preflight_complete"
    payload["cost_preflight"] = cost_preflight(
        option_client,
        spot_client,
        tickers,
        sessions,
        sampled_sessions,
        args,
    )
    if args.download_sample_sessions > 0:
        payload["sample_iv"] = sample_download(
            option_client,
            spot_client,
            tickers,
            sampled_sessions,
            args,
        )
    write_or_print(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
