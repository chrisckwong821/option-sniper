"""Publish and restore the completed-session universe used by the bucket worker."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from .dashboard_state import (
    RefreshPlan,
    has_complete_prior_session_metrics,
    validate_bundle,
)
from .shared_cache import SharedCache, SharedCacheError, atomic_json


SCHEMA_VERSION = 1


def _validated_state(
    state: Any,
    completed_session: str,
    fingerprint: str,
) -> dict[str, Any]:
    state = validate_bundle(state)
    try:
        plan = RefreshPlan(**state["refresh_plan"])
    except (KeyError, TypeError) as exc:
        raise SharedCacheError("Universe input has an invalid refresh plan") from exc
    if (
        plan.completed_session != completed_session
        or state["snapshot"].get("data_date") != completed_session
        or state["today"].get("anchor_date") != completed_session
    ):
        raise SharedCacheError("Universe input has the wrong completed session")
    if state.get("screening_policy_fingerprint") != fingerprint:
        raise SharedCacheError("Universe input has the wrong screening policy")
    if not has_complete_prior_session_metrics(state, plan):
        raise SharedCacheError("Universe input has incomplete prior-session metrics")
    return state


def publish(root: Path, state: dict[str, Any]) -> str:
    """Publish one immutable, session-addressed universe input."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    plan = RefreshPlan(**state["refresh_plan"])
    fingerprint = state["screening_policy_fingerprint"]
    state = _validated_state(state, plan.completed_session, fingerprint)
    shared = SharedCache(root)
    if not shared.enabled:
        raise SharedCacheError("Universe publication requires S3")
    key = f"universe/{plan.completed_session}.json"
    with tempfile.TemporaryDirectory(dir=root) as folder:
        publication = Path(folder) / "universe.json"
        atomic_json(publication, {
            "schema_version": SCHEMA_VERSION,
            "completed_session": plan.completed_session,
            "screening_policy_fingerprint": fingerprint,
            "state": state,
        })
        shared.put(key, publication)
    return key


def restore(
    root: Path,
    completed_session: str,
    fingerprint: str,
) -> dict[str, Any]:
    """Install a validated universe without touching historical calibration."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    shared = SharedCache(root)
    if not shared.enabled:
        raise SharedCacheError("Universe restoration requires S3")
    with tempfile.TemporaryDirectory(dir=root) as folder:
        publication = Path(folder) / "universe.json"
        key = f"universe/{completed_session}.json"
        if not shared.get(key, publication):
            raise SharedCacheError(f"Shared universe is missing: {completed_session}")
        try:
            payload = json.loads(publication.read_text())
        except (OSError, ValueError, TypeError) as exc:
            raise SharedCacheError("Shared universe is invalid JSON") from exc
        if (
            payload.get("schema_version") != SCHEMA_VERSION
            or payload.get("completed_session") != completed_session
            or payload.get("screening_policy_fingerprint") != fingerprint
        ):
            raise SharedCacheError("Shared universe provenance mismatch")
        state = _validated_state(payload.get("state"), completed_session, fingerprint)
    atomic_json(root / "dashboard-state.json", state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-cache", type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.state_cache.read_text())
    key = publish(args.state_cache.parent, state)
    print(json.dumps({
        "status": "published",
        "completed_session": state["refresh_plan"]["completed_session"],
        "symbols": len(state["today"]["rows"]),
        "key": key,
    }))


if __name__ == "__main__":
    main()
