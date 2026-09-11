#!/usr/bin/env python3
"""Build six-month, time-bucketed IV-expansion rarity statistics.

The downloader has two orchestration modes:

* ``individual`` processes exactly one ticker and is intended for validation.
* ``batch`` processes a dashboard/watchlist universe in bounded ticker chunks.

Raw Databento responses are cached as DBN/Zstd files.  Re-running the same
request reads the local file and does not purchase the data again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import databento as db
import exchange_calendars as xcals
import numpy as np
import pandas as pd

try:
    from .databento_config import resolve_historical_api_keys
    from .nasdaq_universe_movers import (
        estimate_option_iv,
        normalize_symbol,
        parse_occ_symbol,
        prepare_option_pairs,
    )
    from .option_contract_catalog import (
        plan_option_subscriptions,
    )
except ImportError:  # Support direct script execution.
    from databento_config import resolve_historical_api_keys
    from nasdaq_universe_movers import (
        estimate_option_iv,
        normalize_symbol,
        parse_occ_symbol,
        prepare_option_pairs,
    )
    from option_contract_catalog import (
        plan_option_subscriptions,
    )


EASTERN = ZoneInfo("America/New_York")
XNYS = xcals.get_calendar("XNYS")
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
SCHEMA_VERSION = "1.2"
METHOD = "moneyness-matched-black76-robust-mad-min2dte-v2"
DEFAULT_STATE_CACHE = Path(".cache/sigma-tape/dashboard-state.json")
DEFAULT_CACHE_ROOT = Path(".cache/sigma-tape/iv-expansion-history")
DEFAULT_DATABASE = Path(".cache/sigma-tape/iv-expansion-history.sqlite3")
DEFAULT_STATS_OUTPUT = Path(".cache/sigma-tape/iv-expansion-statistics.json")
DEFAULT_MIN_OBSERVATIONS = 20
DEFAULT_QUOTE_LOOKBACK_MINUTES = 15
DEFAULT_MAX_FALLBACK_SESSIONS = 5
DEFAULT_TICKER_CHUNK_SIZE = 25
DEFAULT_OPTION_SYMBOL_CHUNK_SIZE = 1_500
DEFAULT_DOWNLOAD_WORKERS = 3
DEFAULT_DOWNLOAD_RETRIES = 4
DEFAULT_INDIVIDUAL_COST_LIMIT = 50.0
DEFAULT_BATCH_COST_LIMIT = 1_500.0
MIN_OPTION_CALENDAR_DTE = 2


@dataclass(frozen=True)
class BucketMark:
    label: str
    start: datetime
    mark: datetime
    end: datetime


@dataclass(frozen=True)
class HistoryWindow:
    calibration_sessions: tuple[date, ...]
    required_sessions: tuple[date, ...]
    holdout_session: date


@dataclass(frozen=True)
class Baseline:
    ticker: str
    requested_reference_session: date
    reference_session: date
    fallback_sessions: int
    expiry: date
    expiry_status: str
    spot: float
    estimate: dict[str, Any]
    contracts: list[dict[str, Any]]


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and derive six-month, 30-minute IV-expansion history "
            "for one ticker or a ticker batch."
        )
    )
    parser.add_argument("--mode", choices=("individual", "batch"), required=True)
    parser.add_argument(
        "--ticker",
        action="append",
        dest="tickers",
        help="Ticker to process. Individual mode requires exactly one.",
    )
    parser.add_argument("--ticker-source", type=Path, default=DEFAULT_STATE_CACHE)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--end-date", type=parse_iso_date)
    parser.add_argument(
        "--session-limit",
        type=int,
        help="Process only the newest N calibration sessions; useful for smoke tests.",
    )
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--stats-output", type=Path, default=DEFAULT_STATS_OUTPUT)
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument("--quote-lookback-minutes", type=int, default=DEFAULT_QUOTE_LOOKBACK_MINUTES)
    parser.add_argument("--max-fallback-sessions", type=int, default=DEFAULT_MAX_FALLBACK_SESSIONS)
    parser.add_argument("--ticker-chunk-size", type=int, default=DEFAULT_TICKER_CHUNK_SIZE)
    parser.add_argument(
        "--option-symbol-chunk-size",
        type=int,
        default=DEFAULT_OPTION_SYMBOL_CHUNK_SIZE,
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=DEFAULT_DOWNLOAD_WORKERS,
        help=(
            "Concurrent Databento option-request lanes. Session processing and "
            "database writes remain ordered and single-threaded."
        ),
    )
    parser.add_argument(
        "--download-retries",
        type=int,
        default=DEFAULT_DOWNLOAD_RETRIES,
        help="Retries for transient metadata or historical-stream failures.",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        help=(
            "Hard cap for newly requested data. Defaults to $50 for individual "
            "mode and $1,500 for batch mode. Cached data costs $0."
        ),
    )
    parser.add_argument(
        "--prior-cost",
        type=float,
        default=0.0,
        help=(
            "Successful cost from earlier invocations of the same resumable job. "
            "It counts toward --max-cost but is not assigned to this run's categories."
        ),
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print the resolved plan without credentials or downloads.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "Use free metadata cost queries to estimate a conservative parent-chain "
            "upper bound without downloading data."
        ),
    )
    parser.add_argument(
        "--cost-sample-sessions",
        type=int,
        default=5,
        help="Evenly spaced sessions used by --preflight-only.",
    )
    parser.add_argument(
        "--include-low-reliability",
        action="store_true",
        help="Include low-reliability IV observations in robust distributions.",
    )
    parser.add_argument(
        "--stock-only",
        action="store_true",
        help=(
            "Download and cache regular-session one-minute stock bars for the "
            "whole required history window, then stop before option processing."
        ),
    )
    parser.add_argument(
        "--spot-ticker-source",
        type=Path,
        help=(
            "Optional full-universe source used for shared stock requests. This "
            "lets partitioned option jobs reuse one full-universe stock cache."
        ),
    )
    return parser.parse_args(argv)


def default_completed_session(calendar: Any = XNYS) -> date:
    now = datetime.now(UTC)
    nearby = calendar.sessions_in_range(
        (now - timedelta(days=14)).date(),
        (now + timedelta(days=1)).date(),
    )
    completed = [
        session
        for session in nearby
        if calendar.session_close(session).to_pydatetime().astimezone(UTC) <= now
    ]
    if not completed:
        raise RuntimeError("Could not resolve a completed XNYS session")
    return completed[-1].date()


def history_window(
    end_session: date,
    months: int,
    *,
    max_fallback_sessions: int = DEFAULT_MAX_FALLBACK_SESSIONS,
    calendar: Any = XNYS,
) -> HistoryWindow:
    if months <= 0:
        raise ValueError("months must be positive")
    if max_fallback_sessions < 0:
        raise ValueError("max_fallback_sessions cannot be negative")
    start = (pd.Timestamp(end_session) - pd.DateOffset(months=months)).date()
    padding = max(14, max_fallback_sessions * 4 + 7)
    sessions = [
        item.date()
        for item in calendar.sessions_in_range(start - timedelta(days=padding), end_session)
    ]
    calibration = tuple(item for item in sessions if start <= item <= end_session)
    if not calibration or calibration[-1] != end_session:
        raise ValueError(f"{end_session} is not a completed XNYS session")
    first_index = sessions.index(calibration[0])
    required_start = max(0, first_index - max_fallback_sessions - 1)
    return HistoryWindow(
        calibration_sessions=calibration,
        required_sessions=tuple(sessions[required_start:]),
        holdout_session=end_session,
    )


def limit_history_window(
    window: HistoryWindow,
    session_limit: int | None,
    *,
    max_fallback_sessions: int,
) -> HistoryWindow:
    if session_limit is None:
        return window
    if session_limit <= 0:
        raise ValueError("session_limit must be positive")
    calibration = window.calibration_sessions[-session_limit:]
    first_index = window.required_sessions.index(calibration[0])
    required_start = max(0, first_index - max_fallback_sessions - 1)
    return HistoryWindow(
        calibration_sessions=calibration,
        required_sessions=window.required_sessions[required_start:],
        holdout_session=window.holdout_session,
    )


def bucket_marks(session: date) -> tuple[BucketMark, ...]:
    open_time = datetime.combine(session, time(9, 30), EASTERN)
    marks = []
    for index in range(13):
        start = open_time + timedelta(minutes=30 * index)
        mark = start + timedelta(minutes=29)
        marks.append(BucketMark(
            label=start.strftime("%H:%M"),
            start=start,
            mark=mark,
            end=mark + timedelta(minutes=1, seconds=1),
        ))
    return tuple(marks)


def option_expiry_for_observation(
    observation_session: date,
    *,
    calendar: Any = XNYS,
    min_calendar_dte: int = MIN_OPTION_CALENDAR_DTE,
) -> date:
    if min_calendar_dte < 0:
        raise ValueError("min_calendar_dte cannot be negative")
    days_to_friday = (4 - observation_session.weekday()) % 7
    nominal_friday = observation_session + timedelta(days=days_to_friday)
    # If the current weekly expiry is inside the minimum-DTE guardrail, roll
    # to the following weekly expiry.  Resolve each nominal Friday within its
    # own Monday-Friday week so holiday-shortened expiries remain correct.
    for _ in range(2):
        week_start = nominal_friday - timedelta(days=4)
        expiry_sessions = calendar.sessions_in_range(week_start, nominal_friday)
        if len(expiry_sessions):
            expiry = expiry_sessions[-1].date()
            if (expiry - observation_session).days >= min_calendar_dte:
                return expiry
        nominal_friday += timedelta(days=7)
    raise RuntimeError(f"Could not resolve expiry for {observation_session}")


def definition_catalog_session_for_observation(
    observation_session: date,
    *,
    calendar: Any = XNYS,
) -> date:
    """Return the one definition snapshot shared by an expiry cycle.

    With the two-day minimum, a normal Friday expiry first becomes the target
    on the preceding Thursday.  Its catalogue is therefore taken from the
    preceding session (normally Wednesday) and reused through the following
    Wednesday.  Searching the actual exchange calendar also handles holiday-
    shortened weeks without weekday-specific special cases.
    """
    target_expiry = option_expiry_for_observation(
        observation_session,
        calendar=calendar,
    )
    nearby = calendar.sessions_in_range(
        observation_session - timedelta(days=14),
        observation_session,
    )
    matching = [
        session.date()
        for session in nearby
        if option_expiry_for_observation(session.date(), calendar=calendar)
        == target_expiry
    ]
    if not matching:
        raise RuntimeError(
            f"Could not resolve definition cycle for {observation_session}"
        )
    return calendar.previous_session(pd.Timestamp(min(matching))).date()


def definition_catalog_sessions(
    observation_sessions: Sequence[date],
) -> tuple[date, ...]:
    return tuple(sorted({
        definition_catalog_session_for_observation(session)
        for session in observation_sessions
    }))


def tenor_class_for_observation(
    observation_session: date,
    expiry: date,
    *,
    expiry_status: str,
) -> str:
    calendar_dte = (expiry - observation_session).days
    if calendar_dte < MIN_OPTION_CALENDAR_DTE:
        return "below_minimum_dte"
    if calendar_dte <= 4:
        return "front_weekly"
    if expiry_status == "target_weekly" and calendar_dte <= 11:
        return "rolled_next_weekly"
    return "longer_dated_fallback"


def chunked(values: Sequence[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def _rows_at_path(payload: object, path: tuple[str, ...]) -> list[dict[str, Any]]:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return current if isinstance(current, list) else []


def load_universe_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    for candidate in (("today", "rows"), ("rows",), ("snapshot", "rows")):
        rows = _rows_at_path(payload, candidate)
        if rows:
            return [row for row in rows if isinstance(row, dict) and row.get("ticker")]
    raise ValueError(f"No ticker rows found in {path}")


def resolve_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    available = load_universe_rows(args.ticker_source) if args.ticker_source.exists() else []
    by_ticker = {
        normalize_symbol(str(row["ticker"])): row
        for row in available
        if row.get("ticker")
    }
    requested = []
    for value in args.tickers or []:
        requested.extend(piece.strip() for piece in value.split(",") if piece.strip())
    requested = list(dict.fromkeys(normalize_symbol(value) for value in requested))

    if args.mode == "individual":
        if len(requested) != 1:
            raise ValueError("individual mode requires exactly one --ticker")
        ticker = requested[0]
        return [by_ticker.get(ticker, {"ticker": ticker, "iv_chain": {"occ_root": ticker}})]

    selected = requested or list(by_ticker)
    if not selected:
        raise ValueError("batch mode requires --ticker values or a non-empty ticker source")
    return [by_ticker.get(ticker, {"ticker": ticker, "iv_chain": {"occ_root": ticker}}) for ticker in selected]


def robust_distribution(
    values: Iterable[float],
    *,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> dict[str, Any]:
    clean = np.asarray([float(value) for value in values if math.isfinite(float(value))])
    count = int(clean.size)
    if count == 0:
        return {"status": "insufficient_history", "n": 0}
    median = float(np.median(clean))
    mad = float(np.median(np.abs(clean - median)))
    robust_sigma = 1.4826 * mad
    status = "valid"
    if count < min_observations:
        status = "insufficient_history"
    elif not math.isfinite(robust_sigma) or robust_sigma <= 1e-9:
        status = "degenerate_history"
    return {
        "status": status,
        "n": count,
        "median": median,
        "mad": mad,
        "robust_sigma": robust_sigma,
    }


def rarity_score(value: float | None, distribution: dict[str, Any]) -> float | None:
    if value is None or distribution.get("status") != "valid":
        return None
    return (float(value) - float(distribution["median"])) / float(distribution["robust_sigma"])


def rarity_level(z_score: float | None) -> int:
    if z_score is None or not math.isfinite(z_score) or z_score < 1:
        return 0
    return min(3, int(math.floor(z_score + 1e-12)))


def _timestamp_column(frame: pd.DataFrame) -> str | None:
    for column in ("ts_recv", "ts_event", "index"):
        if column in frame.columns:
            return column
    return str(frame.columns[0]) if len(frame.columns) else None


def _timestamp_values(frame: pd.DataFrame) -> pd.Series:
    column = _timestamp_column(frame)
    if column is None:
        return pd.Series([], dtype="datetime64[ns, UTC]")
    return pd.to_datetime(frame[column], utc=True, errors="coerce")


def frame_window(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    timestamps = _timestamp_values(frame)
    start_utc = pd.Timestamp(start).tz_convert("UTC")
    end_utc = pd.Timestamp(end).tz_convert("UTC")
    return frame.loc[(timestamps >= start_utc) & (timestamps < end_utc)].copy()


def latest_spots(frame: pd.DataFrame, tickers: Iterable[str], as_of: datetime) -> dict[str, float]:
    if frame.empty or "close" not in frame.columns:
        return {}
    timestamps = _timestamp_values(frame)
    cutoff = pd.Timestamp(as_of).tz_convert("UTC")
    eligible = frame.loc[timestamps <= cutoff].copy()
    if eligible.empty:
        return {}
    timestamp_column = _timestamp_column(eligible)
    eligible["_normalized"] = eligible.get("symbol", "").astype(str).map(normalize_symbol)
    wanted = {normalize_symbol(ticker) for ticker in tickers}
    eligible = eligible[eligible["_normalized"].isin(wanted)]
    if eligible.empty:
        return {}
    eligible = eligible.sort_values(timestamp_column).drop_duplicates("_normalized", keep="last")
    result = {}
    for _, row in eligible.iterrows():
        try:
            price = float(row["close"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            result[str(row["_normalized"])] = price
    return result


def select_baseline_symbols(
    catalog: dict[str, Any],
    spots: dict[str, float],
    *,
    neighbor_count: int = 4,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    symbols: set[str] = set()
    selected_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for ticker, contracts in (catalog.get("contracts") or {}).items():
        spot = spots.get(normalize_symbol(ticker))
        if spot is None or not contracts:
            continue
        ordered = list(contracts)
        nearest = min(range(len(ordered)), key=lambda index: abs(float(ordered[index]["strike"]) - spot))
        indexes = range(max(0, nearest - neighbor_count), min(len(ordered), nearest + neighbor_count + 1))
        selected = [ordered[index] for index in indexes]
        selected_by_ticker[ticker] = selected
        for contract in selected:
            symbols.update((str(contract["call"]), str(contract["put"])))
    return sorted(symbols), selected_by_ticker


def contracts_by_expiry(
    definition_frame: pd.DataFrame,
    rows: Sequence[dict[str, Any]],
    *,
    min_expiry: date | None = None,
    max_expiry: date | None = None,
) -> dict[str, dict[date, list[dict[str, Any]]]]:
    ticker_by_root = {
        normalize_symbol(str((row.get("iv_chain") or {}).get("occ_root") or row["ticker"])):
        normalize_symbol(str(row["ticker"]))
        for row in rows
    }
    grouped: dict[str, dict[date, dict[tuple[str, float], dict[str, str]]]] = {}
    if "raw_symbol" not in definition_frame.columns:
        return {}
    raw_symbols = definition_frame["raw_symbol"].dropna().astype(str).str.rstrip("\x00")
    parsed = raw_symbols.str.extract(
        r"^(?P<root>.*?)(?P<expiry>\d{6})(?P<side>[CP])(?P<strike>\d{8})$"
    )
    parsed["raw_symbol"] = raw_symbols.to_numpy()
    parsed["normalized_root"] = (
        parsed["root"].str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    )
    parsed["ticker"] = parsed["normalized_root"].map(ticker_by_root)
    parsed["expiry"] = pd.to_datetime(
        parsed["expiry"], format="%y%m%d", errors="coerce"
    ).dt.date
    parsed["strike"] = pd.to_numeric(parsed["strike"], errors="coerce") / 1000
    parsed = parsed.dropna(subset=["ticker", "expiry", "strike"])
    if min_expiry is not None:
        parsed = parsed.loc[parsed["expiry"] >= min_expiry]
    if max_expiry is not None:
        parsed = parsed.loc[parsed["expiry"] <= max_expiry]
    for item in parsed.itertuples(index=False):
        key = (str(item.normalized_root), float(item.strike))
        grouped.setdefault(str(item.ticker), {}).setdefault(item.expiry, {}).setdefault(key, {})[
            str(item.side)
        ] = str(item.raw_symbol)

    output: dict[str, dict[date, list[dict[str, Any]]]] = {}
    for ticker, expiries in grouped.items():
        for expiry, contracts in expiries.items():
            paired = [
                {
                    "root": root,
                    "strike": strike,
                    "call": sides["C"],
                    "put": sides["P"],
                }
                for (root, strike), sides in contracts.items()
                if "C" in sides and "P" in sides
            ]
            if paired:
                output.setdefault(ticker, {})[expiry] = sorted(
                    paired, key=lambda item: float(item["strike"])
                )
    return output


def select_listed_expiry(
    listed: Iterable[date],
    *,
    reference_session: date,
    observation_session: date,
    max_calendar_dte: int = 60,
    min_calendar_dte: int = MIN_OPTION_CALENDAR_DTE,
) -> tuple[date, str] | None:
    eligible = sorted({
        expiry
        for expiry in listed
        if expiry >= observation_session
        and (expiry - observation_session).days >= min_calendar_dte
        and 0 < (expiry - reference_session).days <= max_calendar_dte
    })
    if not eligible:
        return None
    target = option_expiry_for_observation(observation_session)
    if target in eligible:
        return target, "target_weekly"
    return eligible[0], "nearest_listed_fallback"


def estimate_snapshot(
    ticker: str,
    spot: float,
    option_frame: pd.DataFrame,
    expiry: date,
    valuation_time: datetime,
    *,
    moneyness_targets: Sequence[float] | None = None,
) -> dict[str, Any] | None:
    expiry_close = datetime.combine(expiry, time(16), EASTERN)
    year_fraction = (expiry_close - valuation_time).total_seconds() / SECONDS_PER_YEAR
    if year_fraction <= 0:
        return None
    pairs_by_ticker, _ = prepare_option_pairs(option_frame, [{"ticker": ticker}], expiry)
    return estimate_option_iv(
        {"ticker": ticker, "anchor_price": spot},
        pairs_by_ticker.get(ticker, []),
        year_fraction,
        moneyness_targets=moneyness_targets,
    )


def prepare_snapshot_pairs(
    quotes: pd.DataFrame,
    rows: Sequence[dict[str, Any]],
    expiry_by_ticker: dict[str, date],
) -> dict[str, list[dict[str, Any]]]:
    """Index the latest quotes for every ticker in one linear frame scan."""
    required_columns = {"symbol", "bid_px_00", "ask_px_00"}
    if quotes.empty or not required_columns.issubset(quotes.columns):
        return {}
    timestamp_column = _timestamp_column(quotes)
    if timestamp_column is None:
        return {}
    latest = quotes.sort_values(timestamp_column).drop_duplicates("symbol", keep="last")
    ticker_by_root = {
        normalize_symbol(str((row.get("iv_chain") or {}).get("occ_root") or row["ticker"])):
        normalize_symbol(str(row["ticker"]))
        for row in rows
    }
    normalized_expiries = {
        normalize_symbol(ticker): expiry
        for ticker, expiry in expiry_by_ticker.items()
    }
    contracts: dict[tuple[str, float], dict[str, dict[str, Any]]] = {}
    for raw_symbol, raw_bid, raw_ask in zip(
        latest["symbol"],
        latest["bid_px_00"],
        latest["ask_px_00"],
    ):
        parsed = parse_occ_symbol(raw_symbol)
        if parsed is None:
            continue
        ticker = ticker_by_root.get(normalize_symbol(parsed["root"]))
        if ticker is None or normalized_expiries.get(ticker) != parsed["expiry"]:
            continue
        try:
            bid = float(raw_bid)
            ask = float(raw_ask)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(bid) or not np.isfinite(ask) or bid < 0 or ask <= 0 or ask < bid:
            continue
        contracts.setdefault((ticker, float(parsed["strike"])), {})[
            str(parsed["side"])
        ] = {
            "bid": bid,
            "ask": ask,
            "mid": 0.5 * (bid + ask),
            "spread": ask - bid,
            "raw_symbol": str(raw_symbol),
        }
    pairs_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for (ticker, strike), sides in contracts.items():
        if "C" in sides and "P" in sides:
            pairs_by_ticker.setdefault(ticker, []).append({
                "strike": strike,
                "call": sides["C"],
                "put": sides["P"],
            })
    return pairs_by_ticker


def estimate_snapshot_from_pairs(
    ticker: str,
    spot: float,
    pairs: Sequence[dict[str, Any]],
    expiry: date,
    valuation_time: datetime,
    *,
    moneyness_targets: Sequence[float] | None = None,
) -> dict[str, Any] | None:
    expiry_close = datetime.combine(expiry, time(16), EASTERN)
    year_fraction = (expiry_close - valuation_time).total_seconds() / SECONDS_PER_YEAR
    if year_fraction <= 0:
        return None
    return estimate_option_iv(
        {"ticker": ticker, "anchor_price": spot},
        list(pairs),
        year_fraction,
        moneyness_targets=moneyness_targets,
    )


def _canonical_request(request: dict[str, Any]) -> str:
    return json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)


class DownloadLedger:
    def __init__(
        self,
        cache_root: Path,
        max_cost: float,
        *,
        max_retries: int = DEFAULT_DOWNLOAD_RETRIES,
        retry_base_seconds: float = 2.0,
        prior_cost: float = 0.0,
    ) -> None:
        if max_cost < 0:
            raise ValueError("max_cost must be nonnegative")
        if prior_cost < 0 or prior_cost > max_cost:
            raise ValueError("prior_cost must be between zero and max_cost")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        try:
            from .shared_cache import SharedCache
        except ImportError:
            from shared_cache import SharedCache
        self.shared = SharedCache(cache_root.parent)
        self.cache_root = cache_root
        self.max_cost = float(max_cost)
        self.max_retries = 0 if self.shared.enabled else int(max_retries)
        self.retry_base_seconds = float(retry_base_seconds)
        self.prior_cost = float(prior_cost)
        self.estimated_new_cost = self.prior_cost
        self.cached_requests = 0
        self.downloaded_requests = 0
        self.records: list[dict[str, Any]] = []
        self._reserved_cost = 0.0
        self._lock = threading.Lock()
        self._path_locks: dict[str, threading.Lock] = {}
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _path_lock(self, path: Path) -> threading.Lock:
        with self._lock:
            return self._path_locks.setdefault(str(path), threading.Lock())

    def _path(self, category: str, request: dict[str, Any]) -> Path:
        digest = hashlib.sha256(_canonical_request(request).encode()).hexdigest()[:20]
        start = str(request.get("start", "unknown"))[:10]
        directory = self.cache_root / "raw" / category / start
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{digest}.dbn.zst"

    @staticmethod
    def _cost_request(request: dict[str, Any]) -> dict[str, Any]:
        allowed = {"dataset", "start", "end", "symbols", "schema", "stype_in", "limit"}
        return {key: value for key, value in request.items() if key in allowed}

    @staticmethod
    def _frame(store, category):
        if category != 'definitions' or not isinstance(store, db.DBNStore):
            return store.to_df().reset_index()
        # Definitions contain many wide/repeated records. Only OCC symbols are
        # needed for contract selection; never materialize the full catalogue.
        symbols = set()
        for chunk in store.to_df(count=10000, map_symbols=False):
            if 'raw_symbol' in chunk:
                symbols.update(chunk['raw_symbol'].dropna().astype(str))
        return pd.DataFrame({'raw_symbol': sorted(symbols)})

    def fetch_frame(
        self,
        client: Any,
        category: str,
        request: dict[str, Any],
    ) -> pd.DataFrame:
        path = self._path(category, request)
        fingerprint = path.stem.split(".")[0]
        # The per-path lock prevents two worker lanes from purchasing the same
        # exact content-addressed request while preserving concurrency for
        # distinct symbol chunks.
        with self._path_lock(path):
            self.shared.restore_file(path)
            if path.exists() and path.stat().st_size > 0:
                with self._lock:
                    self.cached_requests += 1
                    self.records.append({
                        "category": category,
                        "request_id": fingerprint,
                        "status": "cached",
                        "estimated_cost_usd": 0.0,
                        "path": str(path),
                    })
                return self._frame(db.DBNStore.from_file(path), category)

            if self.shared.enabled:
                try:
                    from .historical_cache import HistoricalCache
                except ImportError:
                    from historical_cache import HistoricalCache
                cached_client = client if isinstance(client, HistoricalCache) else HistoricalCache(
                    root=self.shared.root, client=client, shared=self.shared)
                before_cost, before_count = cached_client.download_cost, cached_client.download_count
                try:
                    return self._frame(cached_client.get_range(**request), category)
                except Exception as exc:
                    if (getattr(exc, "http_status", None) == 422
                            and "symbology_invalid_request" in str(exc)
                            and "None of the symbols could be resolved" in str(exc)):
                        return pd.DataFrame()
                    raise
                finally:
                    with self._lock:
                        self.estimated_new_cost += cached_client.download_cost - before_cost
                        self.downloaded_requests += cached_client.download_count - before_count
                        if cached_client.download_count == before_count:
                            self.cached_requests += 1
            estimated_cost = None
            for attempt in range(self.max_retries + 1):
                try:
                    estimated_cost = float(
                        client.metadata.get_cost(**self._cost_request(request))
                    )
                    break
                except Exception as exc:
                    if (
                        getattr(exc, "http_status", None) == 422
                        and "symbology_invalid_request" in str(exc)
                        and "None of the symbols could be resolved" in str(exc)
                    ):
                        print(
                            f"unavailable {category} {request.get('start')} "
                            f"symbols={len(request.get('symbols') or [])}: "
                            "no historical symbol mapping",
                            flush=True,
                        )
                        return pd.DataFrame()
                    if getattr(exc, "http_status", 500) in (400, 401, 402, 403, 404, 422):
                        raise
                    if attempt >= self.max_retries:
                        raise
                    delay = self.retry_base_seconds * (2 ** attempt)
                    print(
                        f"retry metadata {category} attempt={attempt + 1}/"
                        f"{self.max_retries} error={type(exc).__name__} delay={delay:.1f}s",
                        flush=True,
                    )
                    sleep(delay)
            assert estimated_cost is not None
            with self._lock:
                projected = (
                    self.estimated_new_cost + self._reserved_cost + estimated_cost
                )
                if projected > self.max_cost + 1e-9:
                    raise RuntimeError(
                        f"Request would raise reserved new cost to ${projected:.4f}, "
                        f"above the ${self.max_cost:.4f} hard cap"
                    )
                self._reserved_cost += estimated_cost
            print(
                f"download {category} {str(request.get('start'))[:16]} "
                f"symbols={len(request.get('symbols') or [])} cost=${estimated_cost:.6f}",
                flush=True,
            )
            if self.shared.enabled:
                try:
                    from .reconstruction_budget import reserve
                except ImportError:
                    from reconstruction_budget import reserve
                reserve(estimated_cost, request)
            partial_path = path.with_name(path.name + ".part")
            partial_path.unlink(missing_ok=True)
            try:
                for attempt in range(self.max_retries + 1):
                    partial_path.unlink(missing_ok=True)
                    try:
                        store = client.timeseries.get_range(**request, path=partial_path)
                        if partial_path.exists():
                            partial_path.replace(path)
                        break
                    except Exception as exc:
                        partial_path.unlink(missing_ok=True)
                        if attempt >= self.max_retries:
                            raise
                        delay = self.retry_base_seconds * (2 ** attempt)
                        print(
                            f"retry download {category} attempt={attempt + 1}/"
                            f"{self.max_retries} error={type(exc).__name__} delay={delay:.1f}s",
                            flush=True,
                        )
                        sleep(delay)
            except BaseException:
                partial_path.unlink(missing_ok=True)
                with self._lock:
                    self._reserved_cost -= estimated_cost
                raise
            with self._lock:
                self._reserved_cost -= estimated_cost
                self.estimated_new_cost += estimated_cost
                self.downloaded_requests += 1
                self.records.append({
                    "category": category,
                    "request_id": fingerprint,
                    "status": "downloaded",
                    "estimated_cost_usd": estimated_cost,
                    "path": str(path),
                })
            if path.exists():
                self.shared.put("history/" + path.resolve().relative_to(self.shared.root).as_posix(), path)
                return self._frame(db.DBNStore.from_file(path), category)
            return self._frame(store, category)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self.records)
            estimated_new_cost = self.estimated_new_cost
            reserved_cost = self._reserved_cost
            downloaded_requests = self.downloaded_requests
            cached_requests = self.cached_requests
        by_category: dict[str, float] = {}
        for row in records:
            by_category[row["category"]] = (
                by_category.get(row["category"], 0.0)
                + float(row["estimated_cost_usd"])
            )
        return {
            "estimated_new_cost_usd": estimated_new_cost,
            "prior_invocation_cost_usd": self.prior_cost,
            "reserved_cost_usd": reserved_cost,
            "hard_cap_usd": self.max_cost,
            "downloaded_requests": downloaded_requests,
            "cached_requests": cached_requests,
            "estimated_new_cost_by_category_usd": by_category,
        }


def initialize_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            ticker TEXT NOT NULL,
            session_date TEXT NOT NULL,
            bucket TEXT NOT NULL,
            observation_ts TEXT NOT NULL,
            requested_reference_session TEXT NOT NULL,
            reference_session TEXT NOT NULL,
            reference_status TEXT NOT NULL,
            fallback_sessions INTEGER NOT NULL,
            expiry TEXT NOT NULL,
            expiry_status TEXT NOT NULL DEFAULT 'target_weekly',
            observation_dte_days INTEGER NOT NULL DEFAULT 0,
            observation_tenor_class TEXT NOT NULL DEFAULT 'non_expiry',
            reference_iv_pct REAL NOT NULL,
            observed_iv_pct REAL NOT NULL,
            iv_expansion_pct REAL NOT NULL,
            iv_log_change_pct REAL NOT NULL,
            reference_reliability TEXT NOT NULL,
            observed_reliability TEXT NOT NULL,
            moneyness_match_max_error REAL,
            PRIMARY KEY (ticker, session_date, bucket)
        )
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(observations)")
    }
    if "observation_dte_days" not in columns:
        connection.execute(
            "ALTER TABLE observations ADD COLUMN observation_dte_days INTEGER NOT NULL DEFAULT 0"
        )
        connection.commit()
    if "expiry_status" not in columns:
        connection.execute(
            "ALTER TABLE observations ADD COLUMN expiry_status TEXT NOT NULL DEFAULT 'target_weekly'"
        )
        connection.commit()
    if "observation_tenor_class" not in columns:
        connection.execute(
            "ALTER TABLE observations ADD COLUMN observation_tenor_class TEXT NOT NULL DEFAULT 'non_expiry'"
        )
        connection.commit()
    return connection


def upsert_observation(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = (
        "ticker", "session_date", "bucket", "observation_ts",
        "requested_reference_session", "reference_session", "reference_status",
        "fallback_sessions", "expiry", "expiry_status", "observation_dte_days",
        "observation_tenor_class", "reference_iv_pct", "observed_iv_pct",
        "iv_expansion_pct", "iv_log_change_pct", "reference_reliability",
        "observed_reliability", "moneyness_match_max_error",
    )
    connection.execute(
        f"INSERT OR REPLACE INTO observations ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(row.get(column) for column in columns),
    )


def _definition_request(rows: Sequence[dict[str, Any]], session: date) -> dict[str, Any]:
    parents = sorted({
        f"{str((row.get('iv_chain') or {}).get('occ_root') or row['ticker']).strip()}.OPT"
        for row in rows
    })
    return {
        "dataset": "OPRA.PILLAR",
        "schema": "definition",
        "stype_in": "parent",
        "symbols": parents,
        "start": session.isoformat(),
        "end": (session + timedelta(days=1)).isoformat(),
    }


def _spot_request(rows: Sequence[dict[str, Any]], session: date) -> dict[str, Any]:
    return {
        "dataset": "EQUS.MINI",
        "schema": "ohlcv-1m",
        "stype_in": "raw_symbol",
        "symbols": sorted({str(row["ticker"]) for row in rows}),
        "start": datetime.combine(session, time(9, 30), EASTERN).isoformat(),
        "end": datetime.combine(session, time(16), EASTERN).isoformat(),
    }


def _option_request(symbols: Sequence[str], start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "dataset": "OPRA.PILLAR",
        "schema": "cbbo-1m",
        "stype_in": "raw_symbol",
        "symbols": list(symbols),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def _parent_option_request(
    rows: Sequence[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    return {
        "dataset": "OPRA.PILLAR",
        "schema": "cbbo-1m",
        "stype_in": "parent",
        "symbols": sorted({
            f"{str((row.get('iv_chain') or {}).get('occ_root') or row['ticker']).strip()}.OPT"
            for row in rows
        }),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def evenly_sampled_sessions(sessions: Sequence[date], count: int) -> list[date]:
    if count <= 0:
        raise ValueError("cost sample session count must be positive")
    if count >= len(sessions):
        return list(sessions)
    indexes = {
        round(index * (len(sessions) - 1) / (count - 1))
        for index in range(count)
    } if count > 1 else {len(sessions) // 2}
    return [sessions[index] for index in sorted(indexes)]


def conservative_cost_preflight(
    option_client: Any,
    spot_client: Any,
    rows: Sequence[dict[str, Any]],
    sessions: Sequence[date],
    *,
    sample_count: int,
) -> dict[str, Any]:
    """Estimate a no-download upper bound using full parents at bucket marks.

    The production downloader purchases exact listed contracts after stock-driven
    planning.  Parent-chain minute marks are intentionally used here as a
    conservative comparison that can be priced before any data is downloaded.
    """
    sampled = evenly_sampled_sessions(sessions, sample_count)
    catalog_sessions = definition_catalog_sessions(sessions)
    definition_costs = [
        float(option_client.metadata.get_cost(
            **DownloadLedger._cost_request(_definition_request(rows, session))
        ))
        for session in catalog_sessions
    ]
    samples = []
    for session in sampled:
        spot_cost = float(spot_client.metadata.get_cost(
            **DownloadLedger._cost_request(_spot_request(rows, session))
        ))
        option_cost = 0.0
        for mark in bucket_marks(session):
            option_cost += float(option_client.metadata.get_cost(
                **DownloadLedger._cost_request(
                    _parent_option_request(rows, mark.mark, mark.end)
                )
            ))
        samples.append({
            "session": session.isoformat(),
            "spot_cost_usd": spot_cost,
            "parent_chain_bucket_cost_usd": option_cost,
            "total_cost_usd": spot_cost + option_cost,
        })
    average_variable_cost = sum(
        row["total_cost_usd"] for row in samples
    ) / len(samples)
    definition_cost = sum(definition_costs)
    return {
        "method": "cycle-definitions-plus-full-parent-chain-upper-bound",
        "sample_count": len(samples),
        "samples": samples,
        "definition_catalogs": len(catalog_sessions),
        "definition_catalog_cost_usd": definition_cost,
        "average_variable_session_cost_usd": average_variable_cost,
        "estimated_six_month_cost_usd": (
            definition_cost + average_variable_cost * len(sessions)
        ),
        "note": (
            "Actual downloader cost is expected to be lower because it requests "
            "only stock-price-selected call/put contracts, reuses cycle definitions, "
            "and caches every response."
        ),
    }


def _concat_frames(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    materialized = [frame for frame in frames if not frame.empty]
    return pd.concat(materialized, ignore_index=True) if materialized else pd.DataFrame()


def _contracts_for_session(
    option_client: Any,
    ledger: DownloadLedger,
    rows: Sequence[dict[str, Any]],
    session: date,
    *,
    observation_session: date,
    contract_cache: dict[date, dict[str, dict[date, list[dict[str, Any]]]]] | None = None,
    available_at: date | None = None,
) -> dict[str, dict[date, list[dict[str, Any]]]]:
    # Dashboard chains describe one screening expiry, not the contracts listed
    # for a historical observation. In particular, Thursday's input can still
    # contain Friday contracts even though calibration has rolled a week ahead.
    # Use the dated, cache-backed definition catalogue for the expiry cycle.
    if available_at is not None and session > available_at:
        raise ValueError("Historical contract catalogue is newer than the reference session")
    if contract_cache is not None and session in contract_cache:
        return contract_cache[session]
    full_request = _definition_request(rows, session)
    full_path = ledger._path("definitions", full_request)
    # Keep existing all-universe caches. New catalogues use bounded parent
    # groups because OPRA's gateway can time out on a 416-parent response.
    # Recover the original request shape before deciding to split it. Otherwise
    # a missing local all-universe catalogue becomes different parent requests
    # that cannot reuse the already archived S3 catalogue.
    if not full_path.exists() and ledger.shared.enabled:
        ledger.shared.restore_file(full_path)
    groups = [list(rows)] if full_path.exists() else chunked(rows, 100)
    contracts = {}
    for group in groups:
        definitions = ledger.fetch_frame(
            option_client, "definitions", _definition_request(group, session)
        )
        contracts.update(contracts_by_expiry(
            definitions,
            group,
            min_expiry=observation_session,
            max_expiry=observation_session + timedelta(days=70),
        ))
    if contract_cache is not None:
        # Calibration sessions are chronological, so only the active expiry
        # cycle needs to stay resident. This bounds memory while avoiding a
        # DBN decode on every session in the same cycle.
        contract_cache.clear()
        contract_cache[session] = contracts
    return contracts


def _spot_frame(
    spot_client: Any,
    ledger: DownloadLedger,
    rows: Sequence[dict[str, Any]],
    session: date,
) -> pd.DataFrame:
    return ledger.fetch_frame(spot_client, "spot", _spot_request(rows, session))


def _option_frames(
    option_client: Any,
    ledger: DownloadLedger,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    chunk_size: int,
    category: str,
    download_workers: int = 1,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    requests = [
        _option_request(chunk, start, end)
        for chunk in chunked(list(symbols), chunk_size)
    ]
    worker_count = min(download_workers, len(requests))
    if worker_count <= 1:
        return _concat_frames(
            ledger.fetch_frame(option_client, category, request)
            for request in requests
        )
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=f"iv-{category}",
    ) as executor:
        futures = [
            executor.submit(ledger.fetch_frame, option_client, category, request)
            for request in requests
        ]
        # Preserve request/chunk order even though acquisition is concurrent.
        return _concat_frames(future.result() for future in futures)


def build_baselines(
    option_client: Any,
    spot_client: Any,
    ledger: DownloadLedger,
    rows: Sequence[dict[str, Any]],
    observation_session: date,
    required_sessions: Sequence[date],
    *,
    spot_rows: Sequence[dict[str, Any]] | None = None,
    max_fallback_sessions: int,
    option_symbol_chunk_size: int,
    download_workers: int = 1,
    contract_cache: dict[date, dict[str, dict[date, list[dict[str, Any]]]]] | None = None,
) -> dict[str, Baseline]:
    requested_reference = XNYS.previous_session(pd.Timestamp(observation_session)).date()
    cycle_catalog_session = definition_catalog_session_for_observation(
        observation_session
    )
    remaining = {normalize_symbol(str(row["ticker"])): row for row in rows}
    baselines: dict[str, Baseline] = {}
    session_set = set(required_sessions)

    reference = requested_reference
    for fallback_sessions in range(max_fallback_sessions + 1):
        if not remaining or reference not in session_set:
            break
        candidate_rows = list(remaining.values())
        close_mark = datetime.combine(reference, time(15, 59), EASTERN)
        # Reuse one point-in-time contract catalogue for the whole target-expiry
        # cycle.  If quote fallback crosses before the cycle catalogue date,
        # use a matching earlier definition snapshot for correctness.
        definition_session = min(reference, cycle_catalog_session)
        contracts = _contracts_for_session(
            option_client,
            ledger,
            candidate_rows,
            definition_session,
            observation_session=observation_session,
            available_at=reference,
            contract_cache=(
                contract_cache
                if definition_session == cycle_catalog_session
                else None
            ),
        )
        expiry_by_ticker: dict[str, tuple[date, str]] = {}
        selected_catalog: dict[str, Any] = {"contracts": {}}
        for ticker in remaining:
            selection = select_listed_expiry(
                (contracts.get(ticker) or {}).keys(),
                reference_session=reference,
                observation_session=observation_session,
            )
            if selection is None:
                continue
            expiry_by_ticker[ticker] = selection
            selected_catalog["contracts"][ticker] = contracts[ticker][selection[0]]
        print(
            f"baseline observation={observation_session} reference={reference} "
            f"target_expiry={option_expiry_for_observation(observation_session)} "
            f"remaining={len(remaining)} eligible_contracts={len(expiry_by_ticker)} "
            f"fallback_sessions={fallback_sessions}", flush=True,
        )
        if not expiry_by_ticker:
            reference = XNYS.previous_session(pd.Timestamp(reference)).date()
            continue
        # A fallback only needs prices for unresolved, eligible symbols. Asking
        # for the entire universe can purchase unrelated old history or trip a
        # budget guard before otherwise valid baselines can be used.
        required_spot_rows = [row for row in (spot_rows if spot_rows is not None else candidate_rows)
                              if normalize_symbol(str(row['ticker'])) in expiry_by_ticker]
        spot_frame = _spot_frame(spot_client, ledger, required_spot_rows, reference)
        spots = latest_spots(spot_frame, expiry_by_ticker, close_mark)
        baseline_symbols, selected = select_baseline_symbols(selected_catalog, spots)
        option_frame = _option_frames(
            option_client,
            ledger,
            baseline_symbols,
            close_mark - timedelta(minutes=DEFAULT_QUOTE_LOOKBACK_MINUTES),
            close_mark + timedelta(minutes=1, seconds=1),
            option_symbol_chunk_size,
            "baseline_options",
            download_workers,
        )
        quote_window = frame_window(
            option_frame,
            close_mark - timedelta(minutes=DEFAULT_QUOTE_LOOKBACK_MINUTES),
            close_mark + timedelta(minutes=1, seconds=1),
        )
        pairs_by_ticker = prepare_snapshot_pairs(
            quote_window,
            candidate_rows,
            {ticker: expiry for ticker, (expiry, _) in expiry_by_ticker.items()},
        )
        for ticker in list(remaining):
            spot = spots.get(ticker)
            if spot is None or ticker not in selected or ticker not in expiry_by_ticker:
                continue
            expiry, expiry_status = expiry_by_ticker[ticker]
            estimate = estimate_snapshot_from_pairs(
                ticker,
                spot,
                pairs_by_ticker.get(ticker, []),
                expiry,
                close_mark,
            )
            if estimate is None:
                continue
            baselines[ticker] = Baseline(
                ticker=ticker,
                requested_reference_session=requested_reference,
                reference_session=reference,
                fallback_sessions=fallback_sessions,
                expiry=expiry,
                expiry_status=expiry_status,
                spot=spot,
                estimate=estimate,
                contracts=list((selected_catalog.get("contracts") or {}).get(ticker) or []),
            )
            remaining.pop(ticker, None)
        if remaining:
            reference = XNYS.previous_session(pd.Timestamp(reference)).date()
    return baselines


def plan_observation_symbols(
    baselines: dict[str, Baseline],
    rows: Sequence[dict[str, Any]],
    spot_frame: pd.DataFrame,
    observation_session: date,
) -> tuple[list[str], dict[str, dict[str, float]]]:
    row_by_ticker = {normalize_symbol(str(row["ticker"])): row for row in rows}
    bucket_spots: dict[str, dict[str, float]] = {}
    symbols: set[str] = set()
    for mark in bucket_marks(observation_session):
        spots = latest_spots(spot_frame, baselines, mark.mark)
        bucket_spots[mark.label] = spots
        for ticker, baseline in baselines.items():
            spot = spots.get(ticker)
            if spot is None:
                continue
            source_row = row_by_ticker[ticker]
            reference_row = {
                **source_row,
                "ticker": ticker,
                "anchor_price": baseline.spot,
                "prior_close_price": baseline.spot,
                "iv_expansion_reference": {
                    "log_moneyness": baseline.estimate["iv_input_log_moneyness"],
                    "forward_price": baseline.estimate["forward_price"],
                },
            }
            catalog = {"contracts": {ticker: baseline.contracts}}
            planned, _ = plan_option_subscriptions(
                [reference_row],
                catalog,
                spot_prices={ticker: spot},
            )
            symbols.update(planned)
    return sorted(symbols), bucket_spots


def process_observation_session(
    connection: sqlite3.Connection,
    option_client: Any,
    spot_client: Any,
    ledger: DownloadLedger,
    rows: Sequence[dict[str, Any]],
    observation_session: date,
    required_sessions: Sequence[date],
    *,
    spot_rows: Sequence[dict[str, Any]] | None = None,
    max_fallback_sessions: int,
    option_symbol_chunk_size: int,
    quote_lookback_minutes: int,
    download_workers: int = 1,
    contract_cache: dict[date, dict[str, dict[date, list[dict[str, Any]]]]] | None = None,
) -> dict[str, int]:
    tickers = [normalize_symbol(str(row["ticker"])) for row in rows]
    baselines = build_baselines(
        option_client,
        spot_client,
        ledger,
        rows,
        observation_session,
        required_sessions,
        spot_rows=spot_rows,
        max_fallback_sessions=max_fallback_sessions,
        option_symbol_chunk_size=option_symbol_chunk_size,
        download_workers=download_workers,
        contract_cache=contract_cache,
    )
    if not baselines:
        raise RuntimeError(
            f"No valid IV baselines for observation {observation_session}; "
            f"target expiry {option_expiry_for_observation(observation_session)}. "
            "Refusing to publish an empty observation session."
        )
    spot_frame = _spot_frame(
        spot_client,
        ledger,
        spot_rows if spot_rows is not None else rows,
        observation_session,
    )
    symbols, bucket_spots = plan_observation_symbols(
        baselines,
        rows,
        spot_frame,
        observation_session,
    )
    day_start = datetime.combine(observation_session, time(9, 30), EASTERN)
    day_end = datetime.combine(observation_session, time(16), EASTERN)
    option_frame = _option_frames(
        option_client,
        ledger,
        symbols,
        day_start,
        day_end,
        option_symbol_chunk_size,
        "observation_options",
        download_workers,
    )

    observations = 0
    for mark in bucket_marks(observation_session):
        quote_window = frame_window(
            option_frame,
            mark.mark - timedelta(minutes=quote_lookback_minutes),
            mark.end,
        )
        pairs_by_ticker = prepare_snapshot_pairs(
            quote_window,
            rows,
            {ticker: baseline.expiry for ticker, baseline in baselines.items()},
        )
        for ticker, baseline in baselines.items():
            spot = bucket_spots[mark.label].get(ticker)
            if spot is None:
                continue
            estimate = estimate_snapshot_from_pairs(
                ticker,
                spot,
                pairs_by_ticker.get(ticker, []),
                baseline.expiry,
                mark.mark,
                moneyness_targets=baseline.estimate["iv_input_log_moneyness"],
            )
            if estimate is None:
                continue
            reference_iv = float(baseline.estimate["annualized_implied_vol_pct"])
            observed_iv = float(estimate["annualized_implied_vol_pct"])
            expansion = 100 * (observed_iv / reference_iv - 1)
            log_change = 100 * math.log(observed_iv / reference_iv)
            upsert_observation(connection, {
                "ticker": ticker,
                "session_date": observation_session.isoformat(),
                "bucket": mark.label,
                "observation_ts": mark.mark.isoformat(),
                "requested_reference_session": baseline.requested_reference_session.isoformat(),
                "reference_session": baseline.reference_session.isoformat(),
                "reference_status": "yesterday" if baseline.fallback_sessions == 0 else "fallback",
                "fallback_sessions": baseline.fallback_sessions,
                "expiry": baseline.expiry.isoformat(),
                "expiry_status": baseline.expiry_status,
                "observation_dte_days": (baseline.expiry - observation_session).days,
                "observation_tenor_class": tenor_class_for_observation(
                    observation_session,
                    baseline.expiry,
                    expiry_status=baseline.expiry_status,
                ),
                "reference_iv_pct": reference_iv,
                "observed_iv_pct": observed_iv,
                "iv_expansion_pct": expansion,
                "iv_log_change_pct": log_change,
                "reference_reliability": baseline.estimate["iv_reliability"],
                "observed_reliability": estimate["iv_reliability"],
                "moneyness_match_max_error": estimate.get("iv_moneyness_match_max_error"),
            })
            observations += 1
    connection.commit()
    return {
        "target_tickers": len(tickers),
        "baselines": len(baselines),
        "observations": observations,
    }


def build_statistics(
    connection: sqlite3.Connection,
    tickers: Sequence[str],
    *,
    holdout_session: date,
    min_observations: int,
    include_low_reliability: bool,
    first_session: date | None = None,
) -> list[dict[str, Any]]:
    first_session = first_session or (pd.Timestamp(holdout_session) - pd.DateOffset(months=6)).date()
    output = []
    for ticker in tickers:
        for mark in bucket_marks(holdout_session):
            rows = connection.execute(
                """
                SELECT session_date, iv_expansion_pct, iv_log_change_pct,
                       reference_status, reference_reliability, observed_reliability,
                       observation_dte_days, observation_tenor_class, expiry_status
                FROM observations
                WHERE ticker = ? AND bucket = ? AND session_date >= ? AND session_date <= ?
                ORDER BY session_date
                """,
                (ticker, mark.label, first_session.isoformat(), holdout_session.isoformat()),
            ).fetchall()
            holdout = next((row for row in rows if row[0] == holdout_session.isoformat()), None)
            # Emit every calibrated tenor regime.  The current holdout belongs
            # to only one regime, but the live scorer needs the other regime's
            # distribution when the next session has a different tenor class.
            tenor_classes = sorted({str(row[7]) for row in rows})
            for tenor_class in tenor_classes:
                all_calibration = [
                    row for row in rows
                    if row[0] != holdout_session.isoformat() and str(row[7]) == tenor_class
                ]
                reliable_calibration = [
                    row for row in all_calibration
                    if row[4] != "low" and row[5] != "low"
                ]
                calibration = all_calibration if include_low_reliability else reliable_calibration
                pct_distribution = robust_distribution(
                    (row[1] for row in calibration),
                    min_observations=min_observations,
                )
                log_distribution = robust_distribution(
                    (row[2] for row in calibration),
                    min_observations=min_observations,
                )
                all_pct_distribution = robust_distribution(
                    (row[1] for row in all_calibration),
                    min_observations=min_observations,
                )
                all_log_distribution = robust_distribution(
                    (row[2] for row in all_calibration),
                    min_observations=min_observations,
                )
                selected_holdout = holdout if holdout is not None and str(holdout[7]) == tenor_class else None
                holdout_is_reliable = bool(
                    selected_holdout
                    and selected_holdout[4] != "low"
                    and selected_holdout[5] != "low"
                )
                # A low-reliability current mark can still be informative, so
                # score it against the selected calibration distribution and
                # label the result provisional.  Unless explicitly requested
                # above, low-reliability history remains excluded from that
                # distribution and cannot widen or shift the clean baseline.
                holdout_z = rarity_score(
                    selected_holdout[2] if selected_holdout else None,
                    log_distribution,
                )
                output.append({
                    "ticker": ticker,
                    "bucket": mark.label,
                    "tenor_class": tenor_class,
                    "observation_dte_days": int(selected_holdout[6]) if selected_holdout else None,
                    "method": METHOD,
                    "calibration_excludes_session": holdout_session.isoformat(),
                    "expansion_pct_distribution": pct_distribution,
                    "log_iv_change_distribution": log_distribution,
                    "all_valid_expansion_pct_distribution": all_pct_distribution,
                    "all_valid_log_iv_change_distribution": all_log_distribution,
                    "holdout": ({
                        "session": holdout_session.isoformat(),
                        "iv_expansion_pct": selected_holdout[1],
                        "iv_log_change_pct": selected_holdout[2],
                        "iv_expansion_z": holdout_z,
                        "iv_expansion_level": rarity_level(holdout_z),
                        "score_status": (
                            ("scored" if holdout_is_reliable else "provisional_low_reliability")
                            if holdout_z is not None
                            else (
                                "low_reliability_unscored"
                                if not holdout_is_reliable
                                else log_distribution["status"]
                            )
                        ),
                        "reference_status": selected_holdout[3],
                        "reference_reliability": selected_holdout[4],
                        "observed_reliability": selected_holdout[5],
                        "expiry_status": selected_holdout[8],
                    } if selected_holdout else None),
                })
    return output


def write_statistics(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def plan_payload(args: argparse.Namespace, rows: Sequence[dict[str, Any]], window: HistoryWindow) -> dict[str, Any]:
    tickers = [normalize_symbol(str(row["ticker"])) for row in rows]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": args.mode,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "months": args.months,
        "first_calibration_session": window.calibration_sessions[0].isoformat(),
        "last_calibration_session": window.calibration_sessions[-1].isoformat(),
        "calibration_sessions": len(window.calibration_sessions),
        "required_sessions_with_fallback_padding": len(window.required_sessions),
        "buckets": [mark.label for mark in bucket_marks(window.holdout_session)],
        "holdout_session": window.holdout_session.isoformat(),
        "ticker_chunk_size": args.ticker_chunk_size,
        "option_symbol_chunk_size": args.option_symbol_chunk_size,
        "download_workers": args.download_workers,
        "download_retries": args.download_retries,
        "prior_cost_usd": args.prior_cost,
        "orchestration": "session_centric_full_universe",
        "stock_only": args.stock_only,
        "minimum_option_calendar_dte": MIN_OPTION_CALENDAR_DTE,
        "definition_catalog_strategy": "one_snapshot_per_target_expiry_cycle",
        "definition_catalogs": len(
            definition_catalog_sessions(window.calibration_sessions)
        ),
    }


def download_stock_history(
    spot_client: Any,
    ledger: DownloadLedger,
    rows: Sequence[dict[str, Any]],
    sessions: Sequence[date],
) -> dict[str, Any]:
    records = 0
    observed_tickers: set[str] = set()
    for session_number, session in enumerate(sessions, start=1):
        frame = _spot_frame(spot_client, ledger, rows, session)
        records += len(frame)
        if "symbol" in frame.columns:
            observed_tickers.update(
                normalize_symbol(str(value))
                for value in frame["symbol"].dropna().unique()
            )
        print(
            f"stock session {session_number}/{len(sessions)} {session}: "
            f"records={len(frame)} cost=${ledger.estimated_new_cost:.4f}",
            flush=True,
        )
    return {
        "sessions": len(sessions),
        "records": records,
        "requested_tickers": len(rows),
        "observed_tickers": len(observed_tickers),
        "missing_tickers": sorted({
            normalize_symbol(str(row["ticker"])) for row in rows
        } - observed_tickers),
        "cost": ledger.summary(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.min_observations <= 0:
        raise SystemExit("--min-observations must be positive")
    if args.download_workers <= 0:
        raise SystemExit("--download-workers must be positive")
    if args.download_retries < 0:
        raise SystemExit("--download-retries cannot be negative")
    if args.prior_cost < 0:
        raise SystemExit("--prior-cost cannot be negative")
    rows = resolve_rows(args)
    spot_rows = (
        load_universe_rows(args.spot_ticker_source)
        if args.spot_ticker_source is not None
        else rows
    )
    end_session = args.end_date or default_completed_session()
    window = history_window(
        end_session,
        args.months,
        max_fallback_sessions=args.max_fallback_sessions,
    )
    window = limit_history_window(
        window,
        args.session_limit,
        max_fallback_sessions=args.max_fallback_sessions,
    )
    plan = plan_payload(args, rows, window)
    if args.plan_only:
        print(json.dumps({**plan, "status": "plan_only"}, indent=2, sort_keys=True))
        return 0

    spot_key, option_key = resolve_historical_api_keys()
    try:
        from .historical_cache import HistoricalCache
    except ImportError:
        from historical_cache import HistoricalCache
    max_cost = args.max_cost if args.max_cost is not None else (
        DEFAULT_INDIVIDUAL_COST_LIMIT if args.mode == "individual" else DEFAULT_BATCH_COST_LIMIT
    )
    spot_client = HistoricalCache(spot_key, root=args.cache_root.parent)
    option_client = HistoricalCache(option_key, root=args.cache_root.parent)
    if not spot_client.shared.enabled:
        if not spot_key or (not args.stock_only and not option_key):
            raise SystemExit("Set the historical spot key and DATABENTO_HISTORICAL_OPTIONS_API_KEY")
        spot_client = db.Historical(spot_key)
        option_client = db.Historical(option_key) if option_key else None
    if args.preflight_only:
        preflight = conservative_cost_preflight(
            option_client,
            spot_client,
            rows,
            window.calibration_sessions,
            sample_count=args.cost_sample_sessions,
        )
        print(json.dumps({
            **plan,
            "status": "preflight_complete",
            "cost_preflight": preflight,
        }, indent=2, sort_keys=True))
        return 0

    ledger = DownloadLedger(
        args.cache_root,
        max_cost,
        max_retries=args.download_retries,
        prior_cost=args.prior_cost,
    )
    if args.stock_only:
        manifest = args.cache_root / "stock-history-manifest.json"
        previous_acquisition_cost = None
        if manifest.exists():
            try:
                previous_stock = json.loads(manifest.read_text()).get("stock") or {}
                previous_acquisition_cost = previous_stock.get("acquisition_cost")
                previous_run_cost = previous_stock.get("cost") or {}
                if (
                    previous_acquisition_cost is None
                    and int(previous_run_cost.get("downloaded_requests") or 0) > 0
                ):
                    previous_acquisition_cost = previous_run_cost
            except (OSError, ValueError, TypeError):
                previous_acquisition_cost = None
        result = download_stock_history(
            spot_client,
            ledger,
            spot_rows,
            window.required_sessions,
        )
        if result["cost"]["downloaded_requests"] > 0:
            result["acquisition_cost"] = result["cost"]
        elif previous_acquisition_cost is not None:
            result["acquisition_cost"] = previous_acquisition_cost
        payload = {
            **plan,
            "status": "stock_complete",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "stock": result,
        }
        write_statistics(manifest, payload)
        print(json.dumps({
            "status": "stock_complete",
            "manifest": str(manifest),
            **result,
        }, indent=2, sort_keys=True), flush=True)
        return 0

    connection = initialize_database(args.database)
    tickers = [normalize_symbol(str(row["ticker"])) for row in rows]
    committed_observations = {
        date.fromisoformat(str(session)): int(count)
        for session, count in connection.execute(
            "SELECT session_date, COUNT(*) FROM observations GROUP BY session_date"
        ).fetchall()
    }
    completed_sessions = 0
    total_observations = 0
    contract_cache: dict[date, dict[str, dict[date, list[dict[str, Any]]]]] = {}
    try:
        print(
            f"session-centric batch: tickers={len(rows)} "
            f"sessions={len(window.calibration_sessions)} "
            f"option_chunk={args.option_symbol_chunk_size} "
            f"workers={args.download_workers}",
            flush=True,
        )
        for session_number, session in enumerate(window.calibration_sessions, start=1):
            if session in committed_observations:
                observations = committed_observations[session]
                completed_sessions += 1
                total_observations += observations
                print(
                    f"session {session_number}/{len(window.calibration_sessions)} "
                    f"{session}: resume_skip observations={observations} "
                    f"cost=${ledger.estimated_new_cost:.4f}",
                    flush=True,
                )
                continue
            result = process_observation_session(
                connection,
                option_client,
                spot_client,
                ledger,
                rows,
                session,
                window.required_sessions,
                spot_rows=spot_rows,
                max_fallback_sessions=args.max_fallback_sessions,
                option_symbol_chunk_size=args.option_symbol_chunk_size,
                quote_lookback_minutes=args.quote_lookback_minutes,
                download_workers=args.download_workers,
                contract_cache=contract_cache,
            )
            completed_sessions += 1
            total_observations += result["observations"]
            print(
                f"session {session_number}/{len(window.calibration_sessions)} "
                f"{session}: baselines={result['baselines']} "
                f"observations={result['observations']} "
                f"cost=${ledger.estimated_new_cost:.4f}",
                flush=True,
            )
        statistics = build_statistics(
            connection,
            tickers,
            holdout_session=window.holdout_session,
            min_observations=args.min_observations,
            include_low_reliability=args.include_low_reliability,
            first_session=window.calibration_sessions[0],
        )
        payload = {
            **plan,
            "status": "complete",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "database": str(args.database),
            "statistics": statistics,
            "run": {
                "completed_sessions": completed_sessions,
                "observations_written": total_observations,
                "cost": ledger.summary(),
            },
        }
        write_statistics(args.stats_output, payload)
        print(json.dumps({
            "status": "complete",
            "stats_output": str(args.stats_output),
            "database": str(args.database),
            "observations": total_observations,
            "cost": ledger.summary(),
        }, indent=2, sort_keys=True), flush=True)
        return 0
    finally:
        write_statistics(args.stats_output.with_suffix(".progress.json"), {
            "completed_sessions": completed_sessions,
            "observations": total_observations,
            "cost": ledger.summary(),
            "generated_at_utc": datetime.now(UTC).isoformat(),
        })
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
