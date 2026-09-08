#!/usr/bin/env python3
"""Refresh persisted IV expansion values from a completed trading session."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import databento as db
import exchange_calendars as xcals
import pandas as pd

try:
    from .databento_config import resolve_historical_api_keys
    from .iv_expansion import IvExpansionTracker
    from .nasdaq_universe_movers import estimate_option_iv, prepare_option_pairs
    from .option_contract_catalog import (
        fetch_option_contract_catalog,
        plan_option_subscriptions,
    )
except ImportError:  # Support direct execution with ``python scripts/...``.
    from databento_config import resolve_historical_api_keys
    from iv_expansion import IvExpansionTracker
    from nasdaq_universe_movers import estimate_option_iv, prepare_option_pairs
    from option_contract_catalog import (
        fetch_option_contract_catalog,
        plan_option_subscriptions,
    )


EASTERN = ZoneInfo("America/New_York")
XNYS = xcals.get_calendar("XNYS")
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
DEFAULT_CACHE_ROOT = Path(".cache/sigma-tape")
DEFAULT_STATE_CACHE = DEFAULT_CACHE_ROOT / "dashboard-state.json"
DEFAULT_PRICE_CACHE = DEFAULT_CACHE_ROOT / "live-prices.json"
DEFAULT_IV_CACHE = DEFAULT_CACHE_ROOT / "live-iv-expansion.json"
DEFAULT_CATALOG_CACHE = DEFAULT_CACHE_ROOT / "option-contracts.json"
DEFAULT_REPORT = DEFAULT_CACHE_ROOT / "iv-expansion-backfill-report.json"


@dataclass(frozen=True)
class ReplaySelection:
    requested_session: str
    observation_session: str
    reference_session: str
    reference_status: str
    fallback_sessions: int
    start: str
    end: str


def targeted_catalog_cache_path(
    base_path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    as_of_date: str,
) -> Path:
    """Keep a targeted definition refresh from replacing the full-universe cache."""
    tickers = sorted(str(row.get("ticker") or "").upper() for row in rows)
    digest = hashlib.sha256("\n".join(tickers).encode()).hexdigest()[:12]
    return base_path.with_name(
        f"{base_path.stem}.{as_of_date}.{digest}{base_path.suffix}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute positive-Z IV expansion values from exact listed option "
            "contracts and timestamp-aligned historical equity bars."
        )
    )
    parser.add_argument("--state-cache", type=Path, default=DEFAULT_STATE_CACHE)
    parser.add_argument("--price-cache", type=Path, default=DEFAULT_PRICE_CACHE)
    parser.add_argument("--iv-cache", type=Path, default=DEFAULT_IV_CACHE)
    parser.add_argument("--catalog-cache", type=Path, default=DEFAULT_CATALOG_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--ticker",
        action="append",
        dest="tickers",
        help="Limit the replay to one ticker; repeat for more tickers.",
    )
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--option-chunk-size", type=int, default=1_500)
    parser.add_argument("--max-definition-cost", type=float, default=2.0)
    parser.add_argument("--max-option-cost", type=float, default=5.0)
    parser.add_argument("--max-spot-cost", type=float, default=1.0)
    parser.add_argument(
        "--max-fallback-sessions",
        type=int,
        default=5,
        help="Maximum completed trading sessions to walk back when yesterday is unavailable.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Recompute selected tickers even when an IV-expansion result is cached.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def select_rows(
    rows: Iterable[dict[str, Any]],
    requested_tickers: Iterable[str] | None,
    completed_tickers: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    requested = {
        str(ticker).strip().upper()
        for ticker in requested_tickers or []
        if str(ticker).strip()
    }
    completed = {str(ticker).upper() for ticker in completed_tickers or []}
    selected = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if requested and ticker not in requested:
            continue
        try:
            positive_z = float(row.get("z_score")) > 0
        except (TypeError, ValueError):
            positive_z = False
        if (
            positive_z
            and row.get("iv_expansion_reference")
            and ticker not in completed
        ):
            selected.append(row)
    return selected


def prices_by_ticker(payload: dict[str, Any]) -> dict[str, float]:
    result = {}
    for point in payload.get("prices") or []:
        try:
            ticker = str(point["ticker"])
            price = float(point["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            result[ticker] = price
    return result


def replay_bounds(completed_session: str, window_minutes: int) -> tuple[str, str]:
    if window_minutes <= 0 or window_minutes > 390:
        raise ValueError("--window-minutes must be between 1 and 390")
    close = pd.Timestamp(f"{completed_session} 16:00:01", tz=str(EASTERN))
    start = close - pd.Timedelta(minutes=window_minutes, seconds=1)
    return start.isoformat(), close.isoformat()


def resolve_replay_selection(
    requested_session: str,
    option_available_end: datetime | None,
    spot_available_end: datetime | None,
    window_minutes: int,
    max_fallback_sessions: int,
    calendar: Any = XNYS,
) -> ReplaySelection | None:
    """Choose yesterday when complete, otherwise the newest complete prior session."""
    if max_fallback_sessions < 0:
        raise ValueError("--max-fallback-sessions cannot be negative")
    if option_available_end is None or spot_available_end is None:
        return None

    requested_date = date.fromisoformat(requested_session)
    sessions = list(calendar.sessions_in_range(
        requested_date - timedelta(days=max(14, max_fallback_sessions * 4 + 7)),
        requested_date,
    ))
    session_dates = [session.date() for session in sessions]
    if requested_date not in session_dates:
        return None
    requested_index = session_dates.index(requested_date)

    for fallback_sessions in range(max_fallback_sessions + 1):
        candidate_index = requested_index - fallback_sessions
        if candidate_index <= 0:
            break
        candidate = session_dates[candidate_index]
        start, end = replay_bounds(candidate.isoformat(), window_minutes)
        request_end = pd.Timestamp(end).to_pydatetime().astimezone(UTC)
        if request_end > option_available_end or request_end > spot_available_end:
            continue
        reference = session_dates[candidate_index - 1]
        return ReplaySelection(
            requested_session=requested_session,
            observation_session=candidate.isoformat(),
            reference_session=reference.isoformat(),
            reference_status="yesterday" if fallback_sessions == 0 else "fallback",
            fallback_sessions=fallback_sessions,
            start=start,
            end=end,
        )
    return None


def chunked(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("Chunk size must be positive")
    return [values[index:index + size] for index in range(0, len(values), size)]


def _frame(result: Any) -> pd.DataFrame:
    frame = result.to_df().reset_index()
    return frame.loc[:, ~frame.columns.duplicated()]


def _timestamp(row: pd.Series, candidates: tuple[str, ...]) -> datetime | None:
    for column in candidates:
        if column not in row or pd.isna(row[column]):
            continue
        try:
            return pd.Timestamp(row[column]).to_pydatetime().astimezone(UTC)
        except (TypeError, ValueError):
            continue
    return None


def spot_events(frame: pd.DataFrame) -> list[tuple[datetime, int, str, float]]:
    events = []
    for _, row in frame.iterrows():
        timestamp = _timestamp(row, ("ts_event", "ts_recv", "index"))
        try:
            ticker = str(row["symbol"])
            price = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp is not None and ticker and math.isfinite(price) and price > 0:
            events.append((timestamp, 0, ticker, price))
    return events


def option_events(
    frame: pd.DataFrame,
) -> list[tuple[datetime, int, str, float, float]]:
    events = []
    for _, row in frame.iterrows():
        timestamp = _timestamp(row, ("ts_recv", "ts_event", "index"))
        try:
            symbol = str(row["symbol"]).rstrip("\x00")
            bid = float(row["bid_px_00"])
            ask = float(row["ask_px_00"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            timestamp is not None
            and symbol
            and math.isfinite(bid)
            and math.isfinite(ask)
            and bid >= 0
            and ask > 0
            and ask >= bid
        ):
            events.append((timestamp, 1, symbol, bid, ask))
    return events


def quote_diagnostics(
    frame: pd.DataFrame,
    selected_symbols: list[str],
) -> dict[str, Any]:
    selected = set(selected_symbols)
    if frame.empty or "symbol" not in frame:
        return {"requested_contracts": len(selected), "quoted_contracts": 0, "records": 0}
    symbols = {
        str(value).rstrip("\x00")
        for value in frame["symbol"].dropna().tolist()
    }
    return {
        "requested_contracts": len(selected),
        "quoted_contracts": len(symbols & selected),
        "records": len(frame),
    }


def schema_available_end(payload: dict[str, Any], schema: str) -> datetime | None:
    value = ((payload.get("schema") or {}).get(schema) or {}).get("end")
    if value is None:
        value = payload.get("end")
    if value is None:
        return None
    return pd.Timestamp(value).to_pydatetime().astimezone(UTC)


def latest_spot_prices(frame: pd.DataFrame) -> dict[str, float]:
    result: dict[str, tuple[datetime, float]] = {}
    for timestamp, _, ticker, price in spot_events(frame):
        previous = result.get(ticker)
        if previous is None or timestamp > previous[0]:
            result[ticker] = (timestamp, price)
    return {ticker: point[1] for ticker, point in result.items()}


def state_spot_prices(
    rows: list[dict[str, Any]],
    session: str,
    refresh: dict[str, Any],
) -> dict[str, float]:
    """Use persisted closes as a bounded fallback when spot history is unavailable."""
    prices: dict[str, float] = {}
    for row in rows:
        if session == str(refresh.get("completed_session")):
            value = row.get("anchor_price") or row.get("live_anchor_price")
        elif session == str(refresh.get("prior_session")):
            value = row.get("prior_close_price")
        else:
            reference = row.get("iv_expansion_reference") or {}
            value = reference.get("spot_price") or reference.get("forward_price")
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            prices[str(row["ticker"])] = price
    return prices


def spot_frame_from_prices(prices: dict[str, float], session: str) -> pd.DataFrame:
    timestamp = pd.Timestamp(f"{session} 15:59:00", tz=str(EASTERN))
    return pd.DataFrame([
        {"ts_event": timestamp, "symbol": ticker, "close": price}
        for ticker, price in sorted(prices.items())
    ])


def session_close_utc(session: str, calendar: Any = XNYS) -> datetime:
    label = pd.Timestamp(session)
    return calendar.session_close(label).to_pydatetime().astimezone(UTC)


def build_reference_rows(
    rows: list[dict[str, Any]],
    spot_frame: pd.DataFrame,
    option_frame: pd.DataFrame,
    *,
    option_expiry: str,
    option_expiry_close: str,
    selection: ReplaySelection,
) -> list[dict[str, Any]]:
    """Rebuild a comparable IV baseline before a fallback observation session."""
    expiry = date.fromisoformat(option_expiry)
    pairs_by_ticker, _ = prepare_option_pairs(option_frame, rows, expiry)
    spots = latest_spot_prices(spot_frame)
    valuation_time = session_close_utc(selection.reference_session)
    expiry_time = datetime.fromisoformat(option_expiry_close).astimezone(UTC)
    year_fraction = (expiry_time - valuation_time).total_seconds() / SECONDS_PER_YEAR
    if year_fraction <= 0:
        return []

    rebuilt = []
    for row in rows:
        ticker = str(row.get("ticker") or "")
        spot = spots.get(ticker)
        if spot is None:
            continue
        estimate = estimate_option_iv(
            {"ticker": ticker, "anchor_price": spot},
            pairs_by_ticker.get(ticker, []),
            year_fraction,
        )
        if estimate is None:
            continue
        rebuilt.append({
            **row,
            "iv_expansion_reference": {
                "method": "moneyness-matched-black76-v1",
                "annualized_implied_vol_pct": estimate["annualized_implied_vol_pct"],
                "forward_price": estimate["forward_price"],
                "strikes": estimate["iv_input_strike_values"],
                "log_moneyness": estimate["iv_input_log_moneyness"],
                "option_expiry": option_expiry,
                "observation_time": valuation_time.isoformat(),
                "requested_session": selection.requested_session,
                "reference_session": selection.reference_session,
                "reference_status": selection.reference_status,
                "fallback_sessions": selection.fallback_sessions,
                "baseline_revision": 1,
            },
            "iv_chain": {
                **(row.get("iv_chain") or {}),
                "strikes": estimate.get("iv_chain_strikes", []),
            },
        })
    return rebuilt


def merge_option_plans(
    rows: list[dict[str, Any]],
    catalog: dict[str, Any],
    spot_sets: Iterable[dict[str, float]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    symbols: set[str] = set()
    combined_plans: dict[str, dict[str, Any]] = {}
    for spots in spot_sets:
        planned_symbols, plans = plan_option_subscriptions(
            rows,
            catalog,
            spot_prices=spots,
        )
        symbols.update(planned_symbols)
        for ticker, plan in plans.items():
            combined = combined_plans.setdefault(ticker, {
                "selected_strikes": set(),
                "listed_strikes": plan.get("listed_strikes"),
            })
            combined["selected_strikes"].update(plan.get("selected_strikes") or [])
    rendered = {
        ticker: {
            **plan,
            "selected_strikes": sorted(plan["selected_strikes"]),
        }
        for ticker, plan in combined_plans.items()
    }
    return sorted(symbols), rendered


def main() -> int:
    args = parse_args()
    state = load_json(args.state_cache)
    universe_rows = list((state.get("today") or {}).get("rows") or [])
    refresh = state["refresh_plan"]
    completed_session = str(refresh["completed_session"])
    base_tracker = IvExpansionTracker(
        universe_rows,
        option_expiry_close=refresh["option_expiry_close"],
        anchor_date=(state.get("today") or {}).get("anchor_date", completed_session),
        cache_path=args.iv_cache,
        cycle_id=state.get("generated_at_utc"),
    )
    base_tracker.load_cache()
    completed_tickers = set() if args.overwrite else set(base_tracker.results)
    rows = select_rows(universe_rows, args.tickers, completed_tickers)
    if not rows:
        report = {
            "schema_version": "1.1",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "status": "complete",
            "requested_session": completed_session,
            "target_tickers": 0,
            "previously_cached_results": len(base_tracker.results),
            "cache_modified": False,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0

    spot_key, option_key = resolve_historical_api_keys()
    if not option_key:
        raise SystemExit(
            "Set DATABENTO_HISTORICAL_OPTIONS_API_KEY before replay"
        )

    spot_client = db.Historical(spot_key) if spot_key else None
    option_client = db.Historical(option_key)
    option_available_end = schema_available_end(
        option_client.metadata.get_dataset_range("OPRA.PILLAR"),
        "cbbo-1m",
    )
    spot_available_end = None
    spot_availability_error = None
    if spot_client is not None:
        try:
            spot_available_end = schema_available_end(
                spot_client.metadata.get_dataset_range("EQUS.MINI"),
                "ohlcv-1m",
            )
        except Exception as exc:
            spot_availability_error = f"{type(exc).__name__}: {exc}"
    selection = resolve_replay_selection(
        completed_session,
        option_available_end,
        spot_available_end or option_available_end,
        args.window_minutes,
        args.max_fallback_sessions,
    )
    if selection is None:
        report = {
            "schema_version": "1.1",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "status": "waiting_for_historical_options",
            "requested_session": completed_session,
            "option_available_end": (
                option_available_end.isoformat() if option_available_end else None
            ),
            "spot_available_end": (
                spot_available_end.isoformat() if spot_available_end else None
            ),
            "target_tickers": len(rows),
            "cache_modified": False,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 3

    print(
        "Selected IV replay: "
        f"requested={selection.requested_session} "
        f"observation={selection.observation_session} "
        f"reference={selection.reference_session} "
        f"status={selection.reference_status} "
        f"targets={len(rows)}",
        flush=True,
    )

    observation_spot_request = {
        "dataset": "EQUS.MINI",
        "schema": "ohlcv-1m",
        "stype_in": "raw_symbol",
        "symbols": [str(row["ticker"]) for row in rows],
        "start": selection.start,
        "end": selection.end,
    }
    reference_start, reference_end = replay_bounds(
        selection.reference_session,
        args.window_minutes,
    )
    reference_spot_request = {
        **observation_spot_request,
        "start": reference_start,
        "end": reference_end,
    }
    spot_cost = 0.0
    spot_source = "dashboard_close_fallback"
    spot_error = spot_availability_error
    observation_spot_frame = spot_frame_from_prices(
        state_spot_prices(rows, selection.observation_session, refresh),
        selection.observation_session,
    )
    reference_spot_frame = spot_frame_from_prices(
        state_spot_prices(rows, selection.reference_session, refresh),
        selection.reference_session,
    )
    if spot_client is not None and spot_available_end is not None:
        try:
            observation_spot_cost = float(
                spot_client.metadata.get_cost(**observation_spot_request)
            )
            reference_spot_cost = float(
                spot_client.metadata.get_cost(**reference_spot_request)
            )
            candidate_spot_cost = observation_spot_cost + reference_spot_cost
            if candidate_spot_cost > args.max_spot_cost:
                raise RuntimeError(
                    f"Estimated equity cost ${candidate_spot_cost:.4f} exceeds "
                    f"${args.max_spot_cost:.4f}"
                )
            observation_spot_frame = _frame(
                spot_client.timeseries.get_range(**observation_spot_request)
            )
            reference_spot_frame = _frame(
                spot_client.timeseries.get_range(**reference_spot_request)
            )
            spot_cost = candidate_spot_cost
            spot_source = "EQUS.MINI"
            spot_error = None
        except Exception as exc:
            spot_error = f"{type(exc).__name__}: {exc}"

    targeted_catalog_cache = targeted_catalog_cache_path(
        args.catalog_cache,
        rows,
        as_of_date=selection.observation_session,
    )
    print(
        f"Fetching option definitions for {len(rows)} target ticker(s)...",
        flush=True,
    )
    catalog = fetch_option_contract_catalog(
        option_client,
        rows,
        as_of_date=selection.observation_session,
        option_expiry=refresh["option_expiry"],
        cache_path=targeted_catalog_cache,
        max_cost=args.max_definition_cost,
    )
    print(
        "Option definitions ready: "
        f"covered={len((catalog.get('contracts') or {}))} "
        f"records={catalog.get('definition_records')}",
        flush=True,
    )
    option_symbols, plans = merge_option_plans(
        rows,
        catalog,
        (
            latest_spot_prices(reference_spot_frame),
            latest_spot_prices(observation_spot_frame),
        ),
    )
    planned_tickers = sorted(plans)
    if not option_symbols or not planned_tickers:
        raise SystemExit("No definition-backed same-expiry contracts were planned")

    def option_requests(start: str, end: str) -> list[dict[str, Any]]:
        return [
        {
            "dataset": "OPRA.PILLAR",
            "schema": "cbbo-1m",
            "stype_in": "raw_symbol",
            "symbols": symbols,
            "start": start,
            "end": end,
        }
        for symbols in chunked(option_symbols, args.option_chunk_size)
        ]

    observation_option_requests = option_requests(selection.start, selection.end)
    reference_option_requests = option_requests(reference_start, reference_end)
    option_costs = [
        float(option_client.metadata.get_cost(**request))
        for request in [*observation_option_requests, *reference_option_requests]
    ]
    option_cost = sum(option_costs)
    if option_cost > args.max_option_cost:
        raise RuntimeError(
            f"Estimated option cost ${option_cost:.4f} exceeds ${args.max_option_cost:.4f}"
        )

    print(
        "Fetching replay data: "
        f"contracts={len(option_symbols)} estimated_cost=${option_cost + spot_cost:.4f}",
        flush=True,
    )

    observation_option_frames = [
        _frame(option_client.timeseries.get_range(**request))
        for request in observation_option_requests
    ]
    reference_option_frames = [
        _frame(option_client.timeseries.get_range(**request))
        for request in reference_option_requests
    ]
    observation_option_frame = (
        pd.concat(observation_option_frames, ignore_index=True)
        if observation_option_frames
        else pd.DataFrame()
    )
    reference_option_frame = (
        pd.concat(reference_option_frames, ignore_index=True)
        if reference_option_frames
        else pd.DataFrame()
    )

    reference_rows = build_reference_rows(
        rows,
        reference_spot_frame,
        reference_option_frame,
        option_expiry=refresh["option_expiry"],
        option_expiry_close=refresh["option_expiry_close"],
        selection=selection,
    )
    rebuilt_by_ticker = {str(row["ticker"]): row for row in reference_rows}
    selected_tickers = {str(row["ticker"]) for row in rows}
    tracker_rows = []
    for row in universe_rows:
        ticker = str(row.get("ticker") or "")
        if ticker not in selected_tickers:
            tracker_rows.append(row)
        elif ticker in rebuilt_by_ticker:
            tracker_rows.append(rebuilt_by_ticker[ticker])
        else:
            tracker_rows.append({**row, "iv_expansion_reference": None})

    tracker = IvExpansionTracker(
        tracker_rows,
        option_expiry_close=refresh["option_expiry_close"],
        anchor_date=(state.get("today") or {}).get("anchor_date", completed_session),
        cache_path=args.iv_cache,
        cycle_id=state.get("generated_at_utc"),
    )
    tracker.load_cache()
    before = set(tracker.results)
    target_tickers = selected_tickers
    if args.overwrite:
        for ticker in target_tickers:
            tracker.results.pop(ticker, None)
        tracker.cache_dirty = True

    events: list[tuple[Any, ...]] = [
        *spot_events(observation_spot_frame),
        *option_events(observation_option_frame),
    ]
    events.sort(key=lambda event: (event[0], event[1]))
    for event in events:
        if event[1] == 0:
            tracker.update_spot(event[2], event[3], event[0])
        else:
            tracker.update_option_quote(event[2], event[3], event[4], event[0])

    after = set(tracker.results)
    refreshed = sorted(target_tickers & after)
    for ticker in refreshed:
        tracker.results[ticker].update({
            "requested_session": selection.requested_session,
            "iv_observation_session": selection.observation_session,
            "iv_reference_session": selection.reference_session,
            "iv_reference_status": selection.reference_status,
            "fallback_sessions": selection.fallback_sessions,
        })
        tracker.cache_dirty = True

    if tracker.cache_dirty and args.iv_cache.exists():
        backup = args.iv_cache.with_suffix(
            args.iv_cache.suffix + ".before-definition-backfill"
        )
        shutil.copy2(args.iv_cache, backup)
    else:
        backup = None
    tracker.save_cache()

    after = set(tracker.results)
    missing = sorted(target_tickers - after)
    report: dict[str, Any] = {
        "schema_version": "1.1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "complete" if not missing else "partial",
        "requested_session": selection.requested_session,
        "observation_session": selection.observation_session,
        "reference_session": selection.reference_session,
        "reference_status": selection.reference_status,
        "fallback_sessions": selection.fallback_sessions,
        "window": {"start": selection.start, "end": selection.end},
        "cost_usd": {
            "definitions": float(catalog.get("estimated_cost_usd") or 0),
            "equity": spot_cost,
            "options": option_cost,
            "total": float(catalog.get("estimated_cost_usd") or 0) + spot_cost + option_cost,
        },
        "target_tickers": len(target_tickers),
        "previously_cached_targets": len(target_tickers & before),
        "refreshed_targets": len(refreshed),
        "missing_targets": len(missing),
        "missing": missing,
        "catalog": {
            "covered_tickers": len((catalog.get("contracts") or {})),
            "definition_records": catalog.get("definition_records"),
        },
        "records": {
            "observation_equity": len(observation_spot_frame),
            "reference_equity": len(reference_spot_frame),
            "observation_options": len(observation_option_frame),
            "reference_options": len(reference_option_frame),
        },
        "spot_source": spot_source,
        "spot_fallback_reason": spot_error,
        "backup": str(backup) if backup else None,
    }
    if "TSEM" in target_tickers:
        tsem_symbols = [
            symbol for symbol in option_symbols
            if symbol.startswith("TSEM")
        ]
        report["TSEM"] = {
            "plan": plans.get("TSEM"),
            "quotes": quote_diagnostics(observation_option_frame, tsem_symbols),
            "result": tracker.results.get("TSEM"),
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
