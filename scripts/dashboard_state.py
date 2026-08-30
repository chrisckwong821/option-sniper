#!/usr/bin/env python3
"""Build, validate, and atomically cache Option Sniper dashboard state."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import exchange_calendars as xcals

try:
    from .build_dashboard_snapshot import build_payload as build_snapshot_payload
    from .build_iv_universe_snapshot import build_payload as build_universe_payload
    from .databento_config import resolve_historical_api_keys
except ImportError:  # Support direct execution with `python scripts/...`.
    from build_dashboard_snapshot import build_payload as build_snapshot_payload
    from build_iv_universe_snapshot import build_payload as build_universe_payload
    from databento_config import resolve_historical_api_keys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = ROOT / ".cache" / "sigma-tape"
DEFAULT_STATE_CACHE = DEFAULT_CACHE_ROOT / "dashboard-state.json"
SCREENING_POLICY_VERSION = "mcap5b-nearest-weekly-7d-v1"
NASDAQ_SCREENER_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=10000&offset=0&download=true"
)


@dataclass(frozen=True)
class RefreshPlan:
    completed_session: str
    prior_session: str
    next_session: str
    option_expiry: str
    option_expiry_close: str
    completed_session_close_utc: str
    option_snapshot_start: str
    option_snapshot_end: str
    option_valuation_time: str


@dataclass(frozen=True)
class RefreshSettings:
    market_cap_min: float = 5_000_000_000
    daily_notional_min: float | None = None
    candidate_target: int | None = None
    max_expiry_dte_days: int = 7
    result_limit: int = 20
    spot_dataset: str = "EQUS.SUMMARY"
    max_spot_cost: float = 1.0
    max_option_cost: float = 5.0


def screening_policy(settings: RefreshSettings | None = None) -> dict[str, Any]:
    config = settings or RefreshSettings()
    return {
        "version": SCREENING_POLICY_VERSION,
        "market_cap_min_usd": config.market_cap_min,
        "daily_notional_min_usd": config.daily_notional_min,
        "candidate_target": config.candidate_target,
        "expiry": {
            "rule": "nearest weekly expiration after the completed session",
            "minimum_calendar_dte": 1,
            "maximum_calendar_dte": config.max_expiry_dte_days,
            "maximum_is_inclusive": True,
        },
        "requires_mathematically_valid_option_iv": True,
        "risk_free_rate_annual_decimal": 0.04,
    }


def screening_policy_fingerprint(settings: RefreshSettings | None = None) -> str:
    encoded = json.dumps(
        screening_policy(settings),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_expiry_horizon(plan: RefreshPlan, settings: RefreshSettings) -> None:
    completed = date.fromisoformat(plan.completed_session)
    expiry = date.fromisoformat(plan.option_expiry)
    calendar_dte = (expiry - completed).days
    if not 1 <= calendar_dte <= settings.max_expiry_dte_days:
        raise ValueError(
            "Nearest weekly option expiration must be 1 through "
            f"{settings.max_expiry_dte_days} calendar days after the completed session; "
            f"received {calendar_dte}"
        )


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _session_date(value: Any) -> date:
    return value.date() if hasattr(value, "date") else value


def build_refresh_plan(
    now: datetime | None = None,
    calendar: Any | None = None,
) -> RefreshPlan:
    """Resolve the latest completed Nasdaq session and its IV/return anchors."""
    current = _as_utc(now or utc_now())
    market_calendar = calendar or xcals.get_calendar("XNYS")
    nearby = market_calendar.sessions_in_range(
        (current - timedelta(days=14)).date(),
        (current + timedelta(days=7)).date(),
    )
    completed = [
        session
        for session in nearby
        if _as_utc(market_calendar.session_close(session).to_pydatetime()) <= current
    ]
    if not completed:
        raise RuntimeError("Could not resolve a completed Nasdaq session")

    completed_session = completed[-1]
    prior_session = market_calendar.previous_session(completed_session)
    next_session = market_calendar.next_session(completed_session)
    completed_close = _as_utc(
        market_calendar.session_close(completed_session).to_pydatetime()
    )
    prior_close = market_calendar.session_close(prior_session).to_pydatetime()
    prior_close_et = prior_close.astimezone(market_calendar.tz)

    next_date = _session_date(next_session)
    days_to_friday = (4 - next_date.weekday()) % 7
    nominal_expiry = next_date + timedelta(days=days_to_friday)
    expiry_sessions = market_calendar.sessions_in_range(next_date, nominal_expiry)
    if not len(expiry_sessions):
        raise RuntimeError("Could not resolve an option expiration session")
    expiry_session = expiry_sessions[-1]
    expiry = _session_date(expiry_session)
    expiry_close = market_calendar.session_close(expiry_session).to_pydatetime()

    snapshot_start = prior_close_et - timedelta(minutes=1)
    snapshot_end = prior_close_et + timedelta(seconds=1)
    return RefreshPlan(
        completed_session=str(_session_date(completed_session)),
        prior_session=str(_session_date(prior_session)),
        next_session=str(next_date),
        option_expiry=str(expiry),
        option_expiry_close=expiry_close.astimezone(market_calendar.tz).isoformat(),
        completed_session_close_utc=completed_close.isoformat(),
        option_snapshot_start=snapshot_start.isoformat(),
        option_snapshot_end=snapshot_end.isoformat(),
        option_valuation_time=prior_close_et.isoformat(),
    )


def _validate_payload(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError(f"{label} payload is missing rows")
    if not payload["rows"]:
        raise ValueError(f"{label} payload has no rows")
    for row in payload["rows"]:
        if not row.get("ticker"):
            raise ValueError(f"{label} contains a row without a ticker")
    return payload


def validate_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict) or bundle.get("schema_version") != "1.0":
        raise ValueError("Unsupported dashboard state schema")
    _validate_payload(bundle.get("snapshot"), "snapshot")
    _validate_payload(bundle.get("today"), "today")
    return bundle


def hydrate_completed_session_metrics(bundle: dict[str, Any]) -> dict[str, Any]:
    """Upgrade cached live-universe rows created before completed moves were shown."""
    for row in bundle["today"]["rows"]:
        live_anchor = row.get("live_anchor_price") or row.get("anchor_price")
        if live_anchor:
            row.setdefault("live_anchor_price", live_anchor)

        prior_gain = row.get("prior_session_gain_pct")
        prior_z = row.get("prior_session_z_score")
        if prior_gain is None or prior_z is None:
            continue

        gain = float(prior_gain)
        z_score = float(prior_z)
        if live_anchor and gain > -100:
            row.setdefault("prior_close_price", float(live_anchor) / (1 + gain / 100))
        if float(row.get("gain_pct") or 0) == 0 and float(row.get("z_score") or 0) == 0:
            row["gain_pct"] = gain
            row["log_return_pct"] = 100 * math.log1p(gain / 100)
            row["z_score"] = z_score
            row["outside_one_sigma"] = abs(z_score) > 1
    return bundle


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temporary, path)


def fetch_nasdaq_universe(output: Path) -> dict[str, Any]:
    request = urllib.request.Request(
        NASDAQ_SCREENER_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/127 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    rows = ((payload.get("data") or {}).get("rows"))
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Nasdaq screener returned no rows")
    atomic_write_json(output, payload)
    return payload


def refresh_dashboard_state(
    plan: RefreshPlan,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    settings: RefreshSettings | None = None,
) -> dict[str, Any]:
    """Run one bounded historical screen and return both dashboard views."""
    spot_key, option_key = resolve_historical_api_keys()
    if not spot_key or not option_key:
        raise RuntimeError(
            "Historical refresh requires DATABENTO_LIVE_API_KEY and "
            "DATABENTO_OPTIONS_API_KEY"
        )

    config = settings or RefreshSettings()
    validate_expiry_horizon(plan, config)
    work = cache_root / "refresh" / plan.completed_session
    universe_path = work / "nasdaq-universe.json"
    screen_path = work / "screen.json"
    option_cache = work / "option-quotes"
    fetch_nasdaq_universe(universe_path)

    command = [
        sys.executable,
        str(ROOT / "scripts" / "nasdaq_universe_movers.py"),
        "--universe", str(universe_path),
        "--output", str(screen_path),
        "--window-start", plan.prior_session,
        "--window-end", plan.completed_session,
        "--market-cap-min", str(config.market_cap_min),
        "--limit", str(config.result_limit),
        "--spot-dataset", config.spot_dataset,
        "--max-spot-cost", str(config.max_spot_cost),
        "--max-option-cost", str(config.max_option_cost),
        "--option-cache-dir", str(option_cache),
        "--option-expiry", plan.option_expiry,
        "--option-expiry-close", plan.option_expiry_close,
        "--option-snapshot-start", plan.option_snapshot_start,
        "--option-snapshot-end", plan.option_snapshot_end,
        "--option-valuation-time", plan.option_valuation_time,
        "--endpoint-time", plan.completed_session_close_utc,
        "--return-anchor-field", "close",
        "--ranking-horizon", "realized_window",
        "--direction", "gainers",
    ]
    if config.daily_notional_min is not None:
        command.extend(["--daily-notional-min", str(config.daily_notional_min)])
    if config.candidate_target is not None:
        command.extend(["--candidate-target", str(config.candidate_target)])
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise RuntimeError(f"Historical screen failed: {detail}")

    screen = json.loads(screen_path.read_text())
    snapshot = build_snapshot_payload(screen)
    today = build_universe_payload(screen)
    snapshot["last_update_utc"] = plan.completed_session_close_utc
    today["last_update_utc"] = plan.completed_session_close_utc
    return validate_bundle({
        "schema_version": "1.0",
        "generated_at_utc": utc_now().isoformat(),
        "refresh_plan": asdict(plan),
        "screening_policy": screening_policy(config),
        "screening_policy_fingerprint": screening_policy_fingerprint(config),
        "snapshot": snapshot,
        "today": today,
    })


def build_mock_bundle(
    plan: RefreshPlan,
    symbol_count: int = 30,
    settings: RefreshSettings | None = None,
) -> dict[str, Any]:
    if symbol_count < 20:
        raise ValueError("Mock symbol count must be at least 20")
    sectors = ["Technology", "Industrials", "Consumer", "Financials", "Healthcare"]
    reliabilities = ["high", "high", "medium", "low"]
    today_rows = []
    for index in range(symbol_count):
        number = index + 1
        price = 45.0 + number * 4.25
        sigma = 1.4 + (index % 7) * 0.32
        prior_gain = 1.0 + index * 0.27
        prior_z = 0.4 + index * 0.08
        prior_close = price / (1 + prior_gain / 100)
        today_rows.append({
            "ticker": f"DEMO{number:02d}",
            "company_name": f"Demo Company {number:02d}",
            "sector": sectors[index % len(sectors)],
            "industry": "Synthetic test data",
            "company_description": "Deterministic development-only market data.",
            "market_cap_usd": 2_500_000_000 + index * 1_250_000_000,
            "anchor_price": price,
            "prior_close_price": prior_close,
            "live_anchor_price": price,
            "last_price": price,
            "gain_pct": prior_gain,
            "log_return_pct": 100 * math.log(price / prior_close),
            "z_score": prior_z,
            "sigma_1d_pct": sigma,
            "annualized_implied_vol_pct": sigma * (365 ** 0.5),
            "iv_reliability": reliabilities[index % len(reliabilities)],
            "outside_one_sigma": abs(prior_z) > 1,
            "prior_session_gain_pct": prior_gain,
            "prior_session_z_score": prior_z,
        })

    reliability_mix = {
        level: sum(row["iv_reliability"] == level for row in today_rows)
        for level in ("high", "medium", "low")
    }
    shared = {
        "schema_version": "1.0",
        "data_date": plan.completed_session,
        "anchor_date": plan.completed_session,
        "last_update_utc": plan.completed_session_close_utc,
        "price_source": "MOCK.SNAPSHOT",
        "iv_snapshot_time": plan.option_valuation_time,
        "option_expiry": plan.option_expiry,
    }
    today = {
        **shared,
        "view": "today_movers",
        "valid_iv_coverage": len(today_rows),
        "reliability_mix": reliability_mix,
        "live_calculation": {
            "version": "log-return-over-option-sigma-v1",
            "anchor_date": plan.completed_session,
            "anchor_description": "Prior regular-session close",
            "sigma_field": "sigma_1d_pct",
            "ranking": "absolute live z-score",
        },
        "rows": today_rows,
    }
    snapshot_rows = []
    for index, source in enumerate(reversed(today_rows[-20:]), start=1):
        gain = source["prior_session_gain_pct"]
        snapshot_rows.append({
            **source,
            "original_rank": index,
            "selection_anchor_price": source["anchor_price"] / (1 + gain / 100),
            "live_anchor_price": source["anchor_price"],
            "gain_pct": gain,
            "z_score": source["prior_session_z_score"],
            "outside_one_sigma": source["prior_session_z_score"] > 1,
        })
    snapshot = {
        **shared,
        "anchor_date": plan.prior_session,
        "valid_iv_coverage": len(today_rows),
        "reliability_mix": reliability_mix,
        "live_calculation": {
            "version": "log-return-over-option-sigma-v1",
            "anchor_date": plan.completed_session,
            "anchor_description": "Saved closing snapshot for the next trading session",
            "sigma_field": "sigma_1d_pct",
        },
        "rows": snapshot_rows,
    }
    return validate_bundle({
        "schema_version": "1.0",
        "generated_at_utc": utc_now().isoformat(),
        "refresh_plan": asdict(plan),
        "screening_policy": screening_policy(settings),
        "screening_policy_fingerprint": screening_policy_fingerprint(settings),
        "snapshot": snapshot,
        "today": today,
    })


class DashboardStateManager:
    def __init__(
        self,
        cache_path: Path = DEFAULT_STATE_CACHE,
        refresh: Callable[[RefreshPlan], dict[str, Any]] | None = None,
        settings: RefreshSettings | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.settings = settings or RefreshSettings()
        self.expected_policy_fingerprint = screening_policy_fingerprint(self.settings)
        self.refresh = refresh or (
            lambda plan: refresh_dashboard_state(plan, cache_path.parent, self.settings)
        )

    def load_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.exists():
            return None
        try:
            return hydrate_completed_session_metrics(
                validate_bundle(json.loads(self.cache_path.read_text()))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _with_runtime_cache(
        self,
        bundle: dict[str, Any],
        plan: RefreshPlan,
        status: str,
        source: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        result = dict(bundle)
        result["cache"] = {
            "status": status,
            "source": source,
            "target_completed_session": plan.completed_session,
            "screening_policy_fingerprint": bundle.get("screening_policy_fingerprint"),
            "expected_screening_policy_fingerprint": self.expected_policy_fingerprint,
            "last_refresh_error": error,
        }
        return result

    def ensure_state(
        self,
        plan: RefreshPlan,
        mode: str = "auto",
        mock: bool = False,
        mock_symbol_count: int = 30,
    ) -> dict[str, Any]:
        if mock:
            return self._with_runtime_cache(
                build_mock_bundle(plan, mock_symbol_count, self.settings),
                plan,
                "mock",
                "generated-mock",
            )

        cached = self.load_cache()
        fresh = bool(
            cached
            and cached["snapshot"].get("data_date") == plan.completed_session
            and cached["today"].get("anchor_date") == plan.completed_session
            and cached.get("screening_policy_fingerprint")
            == self.expected_policy_fingerprint
        )
        if fresh and mode != "force":
            return self._with_runtime_cache(cached, plan, "fresh", "cache")
        if mode == "never":
            if cached:
                return self._with_runtime_cache(cached, plan, "stale", "cache")
            raise RuntimeError("Dashboard state cache is missing and refresh is disabled")

        try:
            validate_expiry_horizon(plan, self.settings)
            refreshed = hydrate_completed_session_metrics(validate_bundle(self.refresh(plan)))
            if refreshed["snapshot"].get("data_date") != plan.completed_session:
                raise ValueError("Refresh did not produce the target completed session")
            if (
                refreshed.get("screening_policy_fingerprint")
                != self.expected_policy_fingerprint
            ):
                raise ValueError("Refresh did not use the expected screening policy")
            atomic_write_json(self.cache_path, refreshed)
            return self._with_runtime_cache(refreshed, plan, "fresh", "refresh")
        except Exception as exc:
            if cached:
                return self._with_runtime_cache(
                    cached,
                    plan,
                    "stale",
                    "cache-fallback",
                    f"{type(exc).__name__}: {exc}",
                )
            raise RuntimeError(f"Dashboard state refresh failed: {exc}") from exc
