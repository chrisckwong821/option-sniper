"""Discover listed option contracts and plan moneyness-aware subscriptions."""

from __future__ import annotations

import bisect
import json
import math
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from .nasdaq_universe_movers import normalize_symbol, parse_occ_symbol
except ImportError:  # Support direct execution with ``python scripts/...``.
    from nasdaq_universe_movers import normalize_symbol, parse_occ_symbol


CATALOG_SCHEMA_VERSION = "1.0"
DEFAULT_DEFINITION_MAX_COST = 1.0
DEFAULT_DEFINITION_CHUNK_SIZE = 50
DEFAULT_TARGET_NEIGHBORS = 1
DEFAULT_MAX_STRIKES_PER_TICKER = 15


def _as_date(value: date | str) -> date:
    return date.fromisoformat(value) if isinstance(value, str) else value


def _root_map(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    result = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        root = str((row.get("iv_chain") or {}).get("occ_root") or ticker)
        if ticker and root:
            result[normalize_symbol(root)] = ticker
    return result


def build_option_contract_catalog(
    records: Iterable[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    as_of_date: date | str,
    option_expiry: date | str,
) -> dict[str, Any]:
    """Build exact call/put contracts from Databento definition records."""
    as_of = _as_date(as_of_date)
    expiry = _as_date(option_expiry)
    ticker_by_root = _root_map(rows)
    grouped: dict[str, dict[tuple[str, float], dict[str, str]]] = {}
    definition_records = 0

    for record in records:
        raw_symbol = str(record.get("raw_symbol") or "").rstrip("\x00")
        parsed = parse_occ_symbol(raw_symbol)
        if parsed is None or parsed["expiry"] != expiry:
            continue
        ticker = ticker_by_root.get(normalize_symbol(parsed["root"]))
        if ticker is None:
            continue
        definition_records += 1
        key = (normalize_symbol(parsed["root"]), float(parsed["strike"]))
        grouped.setdefault(ticker, {}).setdefault(key, {})[parsed["side"]] = raw_symbol

    contracts: dict[str, list[dict[str, Any]]] = {}
    for ticker, ticker_contracts in grouped.items():
        paired = []
        for (root, strike), sides in ticker_contracts.items():
            if "C" not in sides or "P" not in sides:
                continue
            paired.append({
                "root": root,
                "strike": strike,
                "call": sides["C"],
                "put": sides["P"],
            })
        if paired:
            contracts[ticker] = sorted(paired, key=lambda item: item["strike"])

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "as_of_date": as_of.isoformat(),
        "option_expiry": expiry.isoformat(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "definition_records": definition_records,
        "contracts": contracts,
    }


def atomic_write_catalog(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temporary, path)


def load_option_contract_catalog(
    path: Path,
    *,
    option_expiry: date | str,
    minimum_as_of_date: date | str,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
        if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
            return None
        if payload.get("option_expiry") != _as_date(option_expiry).isoformat():
            return None
        if date.fromisoformat(payload["as_of_date"]) < _as_date(minimum_as_of_date):
            return None
        if not isinstance(payload.get("contracts"), dict):
            return None
        return payload
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def fetch_option_contract_catalog(
    client: Any,
    rows: list[dict[str, Any]],
    *,
    as_of_date: date | str,
    option_expiry: date | str,
    cache_path: Path,
    max_cost: float = DEFAULT_DEFINITION_MAX_COST,
    chunk_size: int = DEFAULT_DEFINITION_CHUNK_SIZE,
) -> dict[str, Any]:
    """Fetch one day's OPRA definitions for the requested option parents."""
    as_of = _as_date(as_of_date)
    expiry = _as_date(option_expiry)
    cached = load_option_contract_catalog(
        cache_path,
        option_expiry=expiry,
        minimum_as_of_date=as_of,
    )
    if cached is not None:
        return cached

    parents = sorted({
        f"{str(row.get('ticker') or '').strip()}.OPT"
        for row in rows
        if row.get("ticker")
    })
    request_base = {
        "dataset": "OPRA.PILLAR",
        "schema": "definition",
        "stype_in": "parent",
        "start": as_of.isoformat(),
        "end": (as_of + timedelta(days=1)).isoformat(),
    }
    chunks = [parents[index:index + chunk_size] for index in range(0, len(parents), chunk_size)]
    costs = [
        float(client.metadata.get_cost(**request_base, symbols=chunk))
        for chunk in chunks
    ]
    total_cost = sum(costs)
    if total_cost > max_cost:
        raise RuntimeError(
            f"Estimated option-definition cost ${total_cost:.4f} exceeds "
            f"the ${max_cost:.4f} limit"
        )

    records: list[dict[str, Any]] = []
    raw_record_count = 0
    for chunk in chunks:
        frame = client.timeseries.get_range(
            **request_base,
            symbols=chunk,
        ).to_df().reset_index()
        raw_record_count += len(frame)
        records.extend(frame.to_dict(orient="records"))

    payload = build_option_contract_catalog(
        records,
        rows,
        as_of_date=as_of,
        option_expiry=expiry,
    )
    payload["requested_parents"] = len(parents)
    payload["raw_definition_records"] = raw_record_count
    payload["estimated_cost_usd"] = total_cost
    atomic_write_catalog(cache_path, payload)
    return payload


def _price_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("price")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def plan_option_subscriptions(
    rows: list[dict[str, Any]],
    catalog: dict[str, Any],
    *,
    spot_prices: dict[str, Any] | None = None,
    neighbor_count: int = DEFAULT_TARGET_NEIGHBORS,
    max_strikes_per_ticker: int = DEFAULT_MAX_STRIKES_PER_TICKER,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Select listed strikes around the shifted reference moneyness targets."""
    prices = spot_prices or {}
    symbols: set[str] = set()
    plans: dict[str, dict[str, Any]] = {}

    for row in rows:
        ticker = str(row.get("ticker") or "")
        contracts = list((catalog.get("contracts") or {}).get(ticker) or [])
        reference = row.get("iv_expansion_reference") or {}
        try:
            targets = sorted(float(value) for value in reference.get("log_moneyness") or [])
        except (TypeError, ValueError):
            continue
        if not ticker or not contracts or not targets:
            continue

        current_spot = _price_value(prices.get(ticker)) or _price_value(
            row.get("last_price") or row.get("anchor_price")
        )
        reference_spot = _price_value(
            row.get("prior_close_price") or row.get("anchor_price")
        )
        reference_forward = _price_value(reference.get("forward_price"))
        if current_spot is None:
            continue
        carry_ratio = (
            reference_forward / reference_spot
            if reference_forward is not None and reference_spot is not None
            else 1.0
        )
        proxy_forward = current_spot * carry_ratio
        desired = [proxy_forward * math.exp(target) for target in targets]
        strikes = [float(contract["strike"]) for contract in contracts]
        selected_indices: set[int] = set()

        for target_strike in desired:
            insertion = bisect.bisect_left(strikes, target_strike)
            candidates = [
                index for index in (insertion - 1, insertion)
                if 0 <= index < len(strikes)
            ]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda index: abs(strikes[index] - target_strike))
            selected_indices.update(
                index
                for index in range(nearest - neighbor_count, nearest + neighbor_count + 1)
                if 0 <= index < len(strikes)
            )

        if len(selected_indices) > max_strikes_per_ticker:
            selected_indices = set(sorted(
                selected_indices,
                key=lambda index: min(
                    abs(math.log(strikes[index] / proxy_forward) - target)
                    for target in targets
                ),
            )[:max_strikes_per_ticker])

        selected = [contracts[index] for index in sorted(selected_indices)]
        for contract in selected:
            symbols.add(str(contract["call"]))
            symbols.add(str(contract["put"]))
        plans[ticker] = {
            "proxy_forward": proxy_forward,
            "target_strikes": desired,
            "selected_strikes": [float(contract["strike"]) for contract in selected],
            "listed_strikes": len(contracts),
        }

    return sorted(symbols), plans
