#!/usr/bin/env python3
"""Refresh the persisted dashboard state without starting the web server."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dashboard_state import (
    DEFAULT_STATE_CACHE,
    DashboardStateManager,
    RefreshSettings,
    build_refresh_plan,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-cache", type=Path, default=DEFAULT_STATE_CACHE)
    parser.add_argument("--mode", choices=("auto", "force"), default="auto")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = RefreshSettings()
    plan = build_refresh_plan()
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
