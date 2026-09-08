#!/usr/bin/env python3
"""Refresh the persisted dashboard state without starting the web server."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .dashboard_state import (
    DEFAULT_STATE_CACHE,
    DashboardStateManager,
    RefreshSettings,
    build_refresh_plan,
)


def is_reopening_refresh_due(plan, now=None):
    """Prepare only on the evening before a session following a calendar gap."""
    eastern = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(
        ZoneInfo("America/New_York")
    )
    upcoming = date.fromisoformat(plan.next_session)
    completed = date.fromisoformat(plan.completed_session)
    return (
        (upcoming - completed).days > 1
        and upcoming == eastern.date() + timedelta(days=1)
        and eastern.time().replace(tzinfo=None) >= time(19, 45)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-cache", type=Path, default=DEFAULT_STATE_CACHE)
    parser.add_argument("--reopening-only", action="store_true")
    parser.add_argument("--mode", choices=("auto", "force"), default="auto")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = RefreshSettings()
    plan = build_refresh_plan()
    if args.reopening_only and not is_reopening_refresh_due(plan):
        print("No reopening refresh due")
        return
    state = DashboardStateManager(
        cache_path=args.state_cache,
        settings=settings,
    ).ensure_state(plan, mode=args.mode)
    cache = state["cache"]
    if cache["source"] == "cache-fallback":
        raise RuntimeError(cache["last_refresh_error"] or "Dashboard refresh failed")
    print(json.dumps({
        "cache_status": cache["status"],
        "cache_source": cache["source"],
        "completed_session": plan.completed_session,
        "option_expiry": plan.option_expiry,
        "valid_iv_coverage": state["today"]["valid_iv_coverage"],
        "screening_policy_fingerprint": state["screening_policy_fingerprint"],
    }, indent=2))


if __name__ == "__main__":
    main()
