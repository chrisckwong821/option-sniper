"""Resolve Databento credentials by data role without exposing their values."""

from __future__ import annotations

import os
from collections.abc import Mapping


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def resolve_live_api_key(environ: Mapping[str, str] | None = None) -> str | None:
    env = _environment(environ)
    return env.get("DATABENTO_LIVE_API_KEY") or env.get("DATABENTO_API_KEY")


def resolve_live_option_api_key(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = _environment(environ)
    return (
        env.get("DATABENTO_LIVE_OPTIONS_API_KEY")
        or env.get("DATABENTO_LIVE_OPTION_API_KEY")
        or env.get("DATABENTO_OPTIONS_API_KEY")
        or env.get("DATABENTO_OPTION_API_KEY")
        or env.get("DATABENTO_LIVE_API_KEY")
        or env.get("DATABENTO_API_KEY")
    )


def resolve_historical_api_keys(
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    env = _environment(environ)
    legacy_key = env.get("DATABENTO_API_KEY")
    spot_key = (
        env.get("DATABENTO_SPOT_API_KEY")
        or env.get("DATABENTO_LIVE_API_KEY")
        or legacy_key
    )
    option_key = (
        env.get("DATABENTO_HISTORICAL_OPTIONS_API_KEY")
        or env.get("DATABENTO_HISTORICAL_OPTION_API_KEY")
        or env.get("DATABENTO_OPTIONS_API_KEY")
        or env.get("DATABENTO_OPTION_API_KEY")
        or legacy_key
    )
    return spot_key, option_key
