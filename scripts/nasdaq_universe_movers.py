#!/usr/bin/env python3
"""Screen liquid Nasdaq stocks by option-implied movement."""

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

import databento as db
import exchange_calendars as xcals
import numpy as np
import pandas as pd
from databento.common.error import BentoServerError

try:
    from .databento_config import resolve_historical_api_keys
except ImportError:  # Support direct execution with `python scripts/...`.
    from databento_config import resolve_historical_api_keys


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--candidate-csv-output", type=Path)
    parser.add_argument("--window-start", type=pd.Timestamp, required=True)
    parser.add_argument("--window-end", type=pd.Timestamp, required=True)
    parser.add_argument("--market-cap-min", type=float, default=10_000_000_000)
    parser.add_argument("--daily-notional-min", type=float, default=25_000_000)
    parser.add_argument(
        "--liquidity-manifest",
        type=Path,
        help="Optional per-symbol session notionals used instead of spot-feed volume.",
    )
    parser.add_argument("--candidate-target", type=int, default=300)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--spot-dataset",
        choices=("EQUS.SUMMARY", "EQUS.MINI"),
        default="EQUS.SUMMARY",
    )
    parser.add_argument("--max-spot-cost", type=float, default=1.0)
    parser.add_argument("--max-option-cost", type=float, default=5.0)
    parser.add_argument("--option-cost-estimate", type=float)
    parser.add_argument("--option-chunk-size", type=int, default=100)
    parser.add_argument(
        "--option-cache-dir",
        type=Path,
    )
    parser.add_argument("--option-expiry", type=pd.Timestamp)
    parser.add_argument("--option-expiry-close")
    parser.add_argument("--option-snapshot-start")
    parser.add_argument("--option-snapshot-end")
    parser.add_argument("--option-valuation-time")
    parser.add_argument("--endpoint-time")
    parser.add_argument(
        "--return-anchor-field",
        choices=("open", "close"),
        default="open",
    )
    parser.add_argument(
        "--ranking-horizon",
        choices=("option_expiry", "realized_window"),
        default="option_expiry",
    )
    parser.add_argument(
        "--direction",
        choices=("all", "gainers", "losers"),
        default="all",
    )
    return parser.parse_args()


def is_comparable_stock(name):
    lowered = name.lower()
    excluded = (
        "warrant",
        "contingent value right",
        "subscription right",
        "preferred stock",
        "preferred share",
        "preferred unit",
        "tangible equity unit",
        "mandatory convertible",
    )
    if any(fragment in lowered for fragment in excluded):
        return False
    if re.search(r"\bunits?\b", lowered):
        return False
    if "depositary share" in lowered and "american depositary share" not in lowered:
        return False
    return True


def load_universe(path, market_cap_min):
    payload = json.loads(path.read_text())
    rows = payload["data"]["rows"]
    eligible = []
    exclusions = {"market_cap": 0, "instrument_type": 0, "invalid_market_cap": 0}
    for row in rows:
        try:
            market_cap = float(row.get("marketCap") or 0)
        except (TypeError, ValueError):
            exclusions["invalid_market_cap"] += 1
            continue
        if market_cap <= market_cap_min:
            exclusions["market_cap"] += 1
            continue
        if not is_comparable_stock(row["name"]):
            exclusions["instrument_type"] += 1
            continue
        eligible.append({
            "symbol": row["symbol"],
            "name": row["name"],
            "market_cap": market_cap,
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "country": row.get("country"),
        })
    return eligible, exclusions, len(rows)


def fetch_spot(client, symbols, start, end, dataset, max_cost):
    schema = "ohlcv-1m" if dataset == "EQUS.MINI" else "ohlcv-1d"
    request = {
        "dataset": dataset,
        "symbols": symbols,
        "stype_in": "raw_symbol",
        "schema": schema,
        "start": start,
        "end": end,
    }
    cost = float(client.metadata.get_cost(**request))
    print(json.dumps({
        "stage": "spot_cost_preflight",
        "dataset": dataset,
        "schema": schema,
        "symbol_count": len(symbols),
        "estimated_cost_usd": cost,
        "maximum_cost_usd": max_cost,
    }), flush=True)
    if cost > max_cost:
        raise RuntimeError(
            f"Estimated spot cost ${cost:.4f} exceeds --max-spot-cost ${max_cost:.4f}"
        )
    bars = client.timeseries.get_range(**request).to_df().reset_index()
    timestamp_col = bars.columns[0]
    timestamps = pd.to_datetime(bars[timestamp_col], utc=True)
    if dataset == "EQUS.MINI":
        eastern = timestamps.dt.tz_convert("America/New_York")
        seconds = eastern.dt.hour * 3600 + eastern.dt.minute * 60 + eastern.dt.second
        bars = bars.loc[(seconds >= 9.5 * 3600) & (seconds < 16 * 3600)].copy()
        bars["date"] = eastern.loc[bars.index].dt.date
        bars["_timestamp"] = timestamps.loc[bars.index]
        bars = bars.sort_values(["symbol", "_timestamp"])
        bars = bars.groupby(["symbol", "date"], as_index=False).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
    else:
        bars["date"] = timestamps.dt.date
    return bars, cost, schema


def build_spot_candidates(
    history,
    universe,
    daily_notional_min,
    candidate_target,
    window_start,
    week_dates,
    anchor_label,
    anchor_field,
    liquidity_manifest=None,
):
    metadata = {row["symbol"]: row for row in universe}
    qualified = []
    coverage_exclusions = {
        "missing_required_session": 0,
        "missing_liquidity_manifest": 0,
        "invalid_close": 0,
        "liquidity": 0,
    }

    for symbol, frame in history.groupby("symbol", sort=False):
        if symbol not in metadata:
            continue
        frame = frame.sort_values("date").drop_duplicates("date", keep="last").copy()
        by_date = frame.set_index("date")
        required_dates = week_dates
        if any(trade_date not in by_date.index for trade_date in required_dates):
            coverage_exclusions["missing_required_session"] += 1
            continue
        closes = by_date.loc[required_dates, "close"].astype(float)
        anchor_price = float(by_date.loc[window_start, anchor_field])
        if (
            not np.isfinite(closes).all()
            or (closes <= 0).any()
            or not np.isfinite(anchor_price)
            or anchor_price <= 0
        ):
            coverage_exclusions["invalid_close"] += 1
            continue

        weekly = by_date.loc[week_dates].copy()
        if liquidity_manifest is None:
            weekly["notional"] = weekly["close"].astype(float) * weekly["volume"].astype(float)
        else:
            liquidity = liquidity_manifest.get("symbols", {}).get(symbol)
            if liquidity is None:
                coverage_exclusions["missing_liquidity_manifest"] += 1
                continue
            session_notionals = liquidity.get("session_notionals_usd", {})
            if any(str(trade_date) not in session_notionals for trade_date in week_dates):
                coverage_exclusions["missing_liquidity_manifest"] += 1
                continue
            weekly["notional"] = [
                float(session_notionals[str(trade_date)]) for trade_date in week_dates
            ]
        min_notional = float(weekly["notional"].min())
        if not np.isfinite(min_notional) or min_notional <= daily_notional_min:
            coverage_exclusions["liquidity"] += 1
            continue

        path = []
        for trade_date in week_dates:
            close = float(by_date.loc[trade_date, "close"])
            path.append({
                "date": str(trade_date),
                "close": close,
                "cumulative_log_return_pct": 100 * math.log(close / anchor_price),
                "daily_notional_usd": float(weekly.loc[trade_date, "notional"]),
            })

        meta = metadata[symbol]
        qualified.append({
            "ticker": symbol,
            "company_name": meta["name"],
            "sector": meta["sector"],
            "industry": meta["industry"],
            "market_cap_usd": meta["market_cap"],
            "return_anchor": f"{window_start} {anchor_label}",
            "anchor_price": anchor_price,
            "end_close": path[-1]["close"],
            "weekly_return_pct": 100 * (path[-1]["close"] / anchor_price - 1),
            "weekly_log_return_pct": path[-1]["cumulative_log_return_pct"],
            "minimum_daily_notional_usd": min_notional,
            "average_daily_notional_usd": float(weekly["notional"].mean()),
            "path": path,
        })

    qualified.sort(
        key=lambda row: (row["minimum_daily_notional_usd"], row["market_cap_usd"]),
        reverse=True,
    )
    capped = len(qualified) > candidate_target
    selected = qualified[:candidate_target]
    effective_notional = daily_notional_min
    if capped and selected:
        effective_notional = selected[-1]["minimum_daily_notional_usd"]
    return qualified, selected, effective_notional, capped, coverage_exclusions


def fetch_option_pairs(
    client,
    candidates,
    snapshot_start,
    snapshot_end,
    option_expiry,
    max_cost,
    known_cost,
    chunk_size,
    cache_dir,
):
    parents = [f"{row['ticker']}.OPT" for row in candidates]
    request_base = {
        "dataset": "OPRA.PILLAR",
        "stype_in": "parent",
        "schema": "cbbo-1m",
        "start": snapshot_start,
        "end": snapshot_end,
    }
    chunk_count = math.ceil(len(candidates) / chunk_size)
    chunks = [candidates[index::chunk_count] for index in range(chunk_count)]
    if known_cost is not None:
        cost = known_cost
        print(json.dumps({
            "stage": "option_cost_preflight_reused",
            "parent_symbol_count": len(parents),
            "estimated_cost_usd": cost,
        }), flush=True)
        cost_source = "explicit_prior_preflight"
    else:
        chunk_costs = []
        for chunk_number, chunk_candidates in enumerate(chunks, start=1):
            chunk_parents = [f"{row['ticker']}.OPT" for row in chunk_candidates]
            chunk_cost = float(client.metadata.get_cost(
                **request_base,
                symbols=chunk_parents,
            ))
            chunk_costs.append(chunk_cost)
            print(json.dumps({
                "stage": "option_chunk_cost_preflight",
                "chunk": chunk_number,
                "chunk_count": chunk_count,
                "parent_symbol_count": len(chunk_parents),
                "estimated_cost_usd": chunk_cost,
            }), flush=True)
        cost = sum(chunk_costs)
        cost_source = "databento_chunked_get_cost"
    print(json.dumps({
        "stage": "option_cost_preflight",
        "parent_symbol_count": len(parents),
        "estimated_cost_usd": cost,
        "maximum_cost_usd": max_cost,
    }), flush=True)
    if cost > max_cost:
        raise RuntimeError(
            f"Estimated OPRA cost ${cost:.4f} exceeds --max-option-cost ${max_cost:.4f}"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    all_pairs = {}
    diagnostics = {"unparsed_records": 0, "wrong_expiry_records": 0, "invalid_quote_records": 0}
    total_records = 0

    def load_chunk(chunk_candidates, chunk_label):
        chunk_parents = [f"{row['ticker']}.OPT" for row in chunk_candidates]
        cache_key = hashlib.sha256(json.dumps({
            "dataset": "OPRA.PILLAR",
            "schema": "cbbo-1m",
            "start": snapshot_start,
            "end": snapshot_end,
            "expiry": str(option_expiry),
            "parents": chunk_parents,
        }, sort_keys=True).encode()).hexdigest()[:16]
        cache_path = cache_dir / f"chunk-{cache_key}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text())
            return (
                payload["pairs_by_ticker"],
                payload["diagnostics"],
                payload["record_count"],
                "cache",
            )

        chunk_request = {**request_base, "symbols": chunk_parents}
        try:
            part = client.timeseries.get_range(**chunk_request).to_df().reset_index()
        except BentoServerError as exc:
            if "504" not in str(exc) or len(chunk_candidates) <= 1:
                raise
            midpoint = math.ceil(len(chunk_candidates) / 2)
            print(json.dumps({
                "stage": "option_chunk_split_after_504",
                "chunk": chunk_label,
                "parent_symbol_count": len(chunk_parents),
                "child_sizes": [midpoint, len(chunk_candidates) - midpoint],
            }), flush=True)
            left = load_chunk(chunk_candidates[:midpoint], f"{chunk_label}.1")
            right = load_chunk(chunk_candidates[midpoint:], f"{chunk_label}.2")
            chunk_pairs = {**left[0], **right[0]}
            chunk_diagnostics = {
                key: left[1][key] + right[1][key]
                for key in diagnostics
            }
            record_count = left[2] + right[2]
            source = "databento_split"
        else:
            chunk_pairs, chunk_diagnostics = prepare_option_pairs(
                part,
                chunk_candidates,
                option_expiry,
            )
            record_count = len(part)
            source = "databento"

        cache_path.write_text(json.dumps({
            "request": chunk_request,
            "record_count": record_count,
            "diagnostics": chunk_diagnostics,
            "pairs_by_ticker": chunk_pairs,
        }) + "\n")
        return chunk_pairs, chunk_diagnostics, record_count, source

    for chunk_number, chunk_candidates in enumerate(chunks, start=1):
        chunk_pairs, chunk_diagnostics, record_count, source = load_chunk(
            chunk_candidates,
            str(chunk_number),
        )
        chunk_parents = [f"{row['ticker']}.OPT" for row in chunk_candidates]
        all_pairs.update(chunk_pairs)
        total_records += record_count
        for key in diagnostics:
            diagnostics[key] += chunk_diagnostics[key]
        print(json.dumps({
            "stage": "option_chunk_complete",
            "chunk": chunk_number,
            "chunk_count": chunk_count,
            "parent_symbol_count": len(chunk_parents),
            "record_count": record_count,
            "source": source,
        }), flush=True)
    return all_pairs, diagnostics, total_records, cost, cost_source, parents


def parse_occ_symbol(symbol):
    raw = str(symbol)
    if len(raw) < 16:
        return None
    tail = raw[-15:]
    if tail[6] not in {"C", "P"} or not (tail[:6] + tail[7:]).isdigit():
        return None
    try:
        expiry = pd.to_datetime(tail[:6], format="%y%m%d").date()
    except ValueError:
        return None
    return {
        "root": raw[:-15].strip(),
        "expiry": expiry,
        "side": tail[6],
        "strike": int(tail[7:]) / 1000,
    }


def normal_cdf(value):
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def black76_straddle(forward, strike, total_vol):
    if total_vol <= 0:
        return abs(forward - strike)
    d1 = (math.log(forward / strike) + 0.5 * total_vol * total_vol) / total_vol
    d2 = d1 - total_vol
    call = forward * normal_cdf(d1) - strike * normal_cdf(d2)
    put = strike * normal_cdf(-d2) - forward * normal_cdf(-d1)
    return call + put


def implied_total_vol(forward, strike, straddle_mid):
    intrinsic = abs(forward - strike)
    if forward <= 0 or strike <= 0 or straddle_mid <= intrinsic or straddle_mid >= forward + strike:
        return None
    low, high = 1e-7, 5.0
    if black76_straddle(forward, strike, high) < straddle_mid:
        return None
    for _ in range(80):
        middle = 0.5 * (low + high)
        if black76_straddle(forward, strike, middle) < straddle_mid:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def weighted_median(values, weights):
    ordered = sorted(zip(values, weights), key=lambda item: item[0])
    midpoint = 0.5 * sum(weight for _, weight in ordered)
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= midpoint:
            return value
    return ordered[-1][0]


def normalize_symbol(symbol):
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


def quote_mid(row):
    bid = float(row["bid_px_00"])
    ask = float(row["ask_px_00"])
    if not np.isfinite(bid) or not np.isfinite(ask) or bid < 0 or ask <= 0 or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "mid": 0.5 * (bid + ask), "spread": ask - bid}


def prepare_option_pairs(quotes, candidates, option_expiry):
    if quotes.empty or "symbol" not in quotes.columns:
        return {}, {"unparsed_records": len(quotes), "wrong_expiry_records": 0, "invalid_quote_records": 0}
    timestamp_col = quotes.columns[0]
    latest = quotes.sort_values(timestamp_col).drop_duplicates("symbol", keep="last")
    ticker_by_root = {normalize_symbol(row["ticker"]): row["ticker"] for row in candidates}
    contracts = {}
    diagnostics = {"unparsed_records": 0, "wrong_expiry_records": 0, "invalid_quote_records": 0}

    for _, row in latest.iterrows():
        parsed = parse_occ_symbol(row["symbol"])
        if parsed is None:
            diagnostics["unparsed_records"] += 1
            continue
        if parsed["expiry"] != option_expiry:
            diagnostics["wrong_expiry_records"] += 1
            continue
        ticker = ticker_by_root.get(normalize_symbol(parsed["root"]))
        if ticker is None:
            diagnostics["unparsed_records"] += 1
            continue
        quote = quote_mid(row)
        if quote is None:
            diagnostics["invalid_quote_records"] += 1
            continue
        key = (ticker, parsed["strike"])
        contracts.setdefault(key, {})[parsed["side"]] = {
            **quote,
            "raw_symbol": str(row["symbol"]),
        }

    pairs_by_ticker = {}
    for (ticker, strike), sides in contracts.items():
        if "C" in sides and "P" in sides:
            pairs_by_ticker.setdefault(ticker, []).append({
                "strike": strike,
                "call": sides["C"],
                "put": sides["P"],
            })
    return pairs_by_ticker, diagnostics


def estimate_option_iv(candidate, pairs, year_fraction):
    spot = candidate["anchor_price"]
    near_spot = [pair for pair in pairs if 0.8 * spot <= pair["strike"] <= 1.2 * spot]
    near_spot.sort(key=lambda pair: abs(math.log(pair["strike"] / spot)))
    forward_inputs = []
    for pair in near_spot[:9]:
        forward = pair["strike"] + pair["call"]["mid"] - pair["put"]["mid"]
        total_spread = pair["call"]["spread"] + pair["put"]["spread"]
        if 0.7 * spot <= forward <= 1.3 * spot:
            forward_inputs.append((forward, 1 / max(total_spread, 0.01)))
    if not forward_inputs:
        return None
    forward = weighted_median(
        [item[0] for item in forward_inputs],
        [item[1] for item in forward_inputs],
    )

    near_forward = sorted(near_spot, key=lambda pair: abs(math.log(pair["strike"] / forward)))
    estimates = []
    for pair in near_forward[:5]:
        if abs(math.log(pair["strike"] / forward)) > 0.075:
            continue
        straddle_mid = pair["call"]["mid"] + pair["put"]["mid"]
        total_vol = implied_total_vol(forward, pair["strike"], straddle_mid)
        if total_vol is None or not (0.001 <= total_vol <= 2.0):
            continue
        total_spread = pair["call"]["spread"] + pair["put"]["spread"]
        estimates.append({
            "strike": pair["strike"],
            "total_vol": total_vol,
            "weight": 1 / max(total_spread, 0.01),
            "call": pair["call"],
            "put": pair["put"],
            "straddle_mid": straddle_mid,
            "relative_total_spread": total_spread / straddle_mid if straddle_mid > 0 else math.inf,
        })
    if not estimates:
        return None

    total_vol = weighted_median(
        [item["total_vol"] for item in estimates],
        [item["weight"] for item in estimates],
    )
    atm = min(estimates, key=lambda item: abs(math.log(item["strike"] / forward)))
    both_sides_bid = atm["call"]["bid"] > 0 and atm["put"]["bid"] > 0
    if len(estimates) >= 3 and both_sides_bid and atm["relative_total_spread"] <= 0.25:
        quality = "high"
    elif len(estimates) >= 2 and both_sides_bid and atm["relative_total_spread"] <= 0.75:
        quality = "medium"
    else:
        quality = "low"

    return {
        "forward_price": forward,
        "forward_vs_spot_pct": 100 * (forward / spot - 1),
        "forward_input_strikes": len(forward_inputs),
        "iv_input_strikes": len(estimates),
        "annualized_implied_vol_pct": 100 * total_vol / math.sqrt(year_fraction),
        "full_horizon_implied_sigma_pct": 100 * total_vol,
        "atm_strike": atm["strike"],
        "atm_call_bid_ask": [atm["call"]["bid"], atm["call"]["ask"]],
        "atm_put_bid_ask": [atm["put"]["bid"], atm["put"]["ask"]],
        "atm_straddle_mid": atm["straddle_mid"],
        "atm_relative_total_spread": atm["relative_total_spread"],
        "iv_reliability": quality,
    }


def rank_by_implied_move(
    candidates,
    pairs_by_ticker,
    option_expiry,
    option_expiry_close,
    valuation_time,
    endpoint_time,
    limit,
    ranking_horizon,
    direction,
):
    cutoff = pd.Timestamp(valuation_time)
    expiry_close = pd.Timestamp(option_expiry_close)
    endpoint = pd.Timestamp(endpoint_time)
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("America/New_York")
    if endpoint.tzinfo is None:
        endpoint = endpoint.tz_localize("America/New_York")
    option_year_fraction = (expiry_close - cutoff).total_seconds() / (365 * 24 * 60 * 60)
    realized_year_fraction = (endpoint - cutoff).total_seconds() / (365 * 24 * 60 * 60)
    if option_year_fraction <= 0:
        raise ValueError("Option expiry must be after the option snapshot cutoff")
    if realized_year_fraction <= 0:
        raise ValueError("Realized endpoint must be after the option snapshot cutoff")

    ranked = []
    manifest = []
    for candidate in candidates:
        estimate = estimate_option_iv(
            candidate,
            pairs_by_ticker.get(candidate["ticker"], []),
            option_year_fraction,
        )
        manifest_row = {key: value for key, value in candidate.items() if key != "path"}
        if estimate is None:
            manifest.append({**manifest_row, "option_status": "no_valid_iv"})
            continue

        full_horizon_sigma = estimate["full_horizon_implied_sigma_pct"] / 100
        annualized_iv = estimate["annualized_implied_vol_pct"] / 100
        realized_horizon_sigma = annualized_iv * math.sqrt(realized_year_fraction)
        path = []
        for point in candidate["path"]:
            point_close = (
                endpoint
                if pd.Timestamp(point["date"]).date() == endpoint.date()
                else pd.Timestamp(
                    f"{point['date']} 16:00:00", tz="America/New_York"
                )
            )
            point_year_fraction = max(
                0.0,
                (point_close - cutoff).total_seconds() / (365 * 24 * 60 * 60),
            )
            point_sigma = annualized_iv * math.sqrt(point_year_fraction)
            point_return = point["cumulative_log_return_pct"] / 100
            path.append({
                **point,
                "option_full_horizon_implied_sigma_pct": estimate["full_horizon_implied_sigma_pct"],
                "full_horizon_implied_z": point_return / full_horizon_sigma,
                "elapsed_implied_sigma_pct": 100 * point_sigma,
                "elapsed_horizon_implied_z": point_return / point_sigma if point_sigma else 0.0,
            })
        peak = max(path, key=lambda item: abs(item["full_horizon_implied_z"]))
        endpoint_point = path[-1]
        result = {
            **manifest_row,
            "option_snapshot": estimate,
            "option_expiry": str(option_expiry),
            "option_year_fraction_act_365": option_year_fraction,
            "realized_year_fraction_act_365": realized_year_fraction,
            "realized_horizon_implied_sigma_pct": 100 * realized_horizon_sigma,
            "realized_horizon_implied_z": endpoint_point["elapsed_horizon_implied_z"],
            "weekly_implied_z": endpoint_point["full_horizon_implied_z"],
            "max_abs_implied_path_z": abs(peak["full_horizon_implied_z"]),
            "max_implied_path_z": peak["full_horizon_implied_z"],
            "max_implied_path_date": peak["date"],
            "max_implied_path_return_pct": 100 * (peak["close"] / candidate["anchor_price"] - 1),
            "path": path,
        }
        selected_z = (
            result["weekly_implied_z"]
            if ranking_horizon == "option_expiry"
            else result["realized_horizon_implied_z"]
        )
        result["ranking_implied_z"] = selected_z
        result["outside_one_implied_sigma"] = abs(selected_z) > 1
        ranked.append(result)
        manifest.append({
            **manifest_row,
            "option_status": "valid_iv",
            "full_horizon_implied_sigma_pct": estimate["full_horizon_implied_sigma_pct"],
            "annualized_implied_vol_pct": estimate["annualized_implied_vol_pct"],
            "iv_reliability": estimate["iv_reliability"],
            "max_abs_implied_path_z": result["max_abs_implied_path_z"],
            "weekly_implied_z": result["weekly_implied_z"],
            "realized_horizon_implied_sigma_pct": result["realized_horizon_implied_sigma_pct"],
            "realized_horizon_implied_z": result["realized_horizon_implied_z"],
            "ranking_implied_z": result["ranking_implied_z"],
        })

    if direction == "gainers":
        eligible = [row for row in ranked if row["weekly_log_return_pct"] > 0]
        eligible.sort(key=lambda row: row["ranking_implied_z"], reverse=True)
    elif direction == "losers":
        eligible = [row for row in ranked if row["weekly_log_return_pct"] < 0]
        eligible.sort(key=lambda row: row["ranking_implied_z"])
    else:
        eligible = sorted(ranked, key=lambda row: abs(row["ranking_implied_z"]), reverse=True)
    top = eligible[:limit]
    for rank, row in enumerate(top, start=1):
        row["rank"] = rank
        row["status"] = "screened" if row["outside_one_implied_sigma"] else "passed_within_iv"
    manifest.sort(key=lambda row: abs(row.get("ranking_implied_z", -1)), reverse=True)
    return ranked, top, manifest, option_year_fraction, realized_year_fraction


def write_csv(path, rows):
    flat = []
    for row in rows:
        record = {key: value for key, value in row.items() if key not in {"path", "option_snapshot"}}
        if "option_snapshot" in row:
            record.update({f"option_{key}": value for key, value in row["option_snapshot"].items()})
        flat.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(flat).to_csv(path, index=False)


def resolve_run_config(args):
    window_start = args.window_start.date()
    window_end = args.window_end.date()
    if window_end < window_start:
        raise ValueError("--window-end must not precede --window-start")
    week_dates = [
        timestamp.date()
        for timestamp in xcals.get_calendar("XNYS").sessions_in_range(
            window_start, window_end
        )
    ]
    if not week_dates:
        raise ValueError("The requested window contains no business dates")

    market_open = pd.Timestamp(f"{window_start} 09:30:00", tz="America/New_York")
    default_valuation_time = market_open + pd.Timedelta(minutes=1)
    option_expiry = args.option_expiry.date() if args.option_expiry is not None else window_end
    if args.spot_dataset == "EQUS.MINI":
        spot_start = pd.Timestamp(
            f"{window_start} 09:30:00", tz="America/New_York"
        ).isoformat()
        spot_end = pd.Timestamp(
            f"{window_end} 16:00:00", tz="America/New_York"
        ).isoformat()
        anchor_label = (
            "last blended regular-session trade"
            if args.return_anchor_field == "close"
            else "first blended regular-session trade"
        )
    else:
        spot_start = str(window_start)
        spot_end = str((pd.Timestamp(window_end) + pd.Timedelta(days=1)).date())
        anchor_label = f"official {args.return_anchor_field}"
    return {
        "window_start": window_start,
        "window_end": window_end,
        "week_dates": week_dates,
        "spot_start": spot_start,
        "spot_end": spot_end,
        "anchor_label": anchor_label,
        "anchor_field": args.return_anchor_field,
        "option_expiry": option_expiry,
        "option_expiry_close": args.option_expiry_close or pd.Timestamp(
            f"{option_expiry} 16:00:00", tz="America/New_York"
        ).isoformat(),
        "option_snapshot_start": args.option_snapshot_start or market_open.isoformat(),
        "option_snapshot_end": args.option_snapshot_end or (
            default_valuation_time + pd.Timedelta(seconds=1)
        ).isoformat(),
        "option_valuation_time": args.option_valuation_time or default_valuation_time.isoformat(),
        "endpoint_time": args.endpoint_time or pd.Timestamp(
            f"{window_end} 16:00:00", tz="America/New_York"
        ).isoformat(),
        "option_cache_dir": args.option_cache_dir or Path(
            f"/private/tmp/sigma-tape-opra-{window_start}-open"
        ),
    }


def create_historical_clients(environ=None):
    spot_api_key, option_api_key = resolve_historical_api_keys(environ)
    if not spot_api_key or not option_api_key:
        raise RuntimeError(
            "Set DATABENTO_LIVE_API_KEY and DATABENTO_OPTIONS_API_KEY "
            "(legacy DATABENTO_API_KEY remains supported)"
        )
    return db.Historical(spot_api_key), db.Historical(option_api_key)


def main():
    args = parse_args()
    config = resolve_run_config(args)
    option_expiry = config["option_expiry"]
    universe, exclusions, raw_count = load_universe(args.universe, args.market_cap_min)
    liquidity_manifest = (
        json.loads(args.liquidity_manifest.read_text())
        if args.liquidity_manifest
        else None
    )
    spot_client, option_client = create_historical_clients()

    history, spot_cost, spot_schema = fetch_spot(
        spot_client,
        [row["symbol"] for row in universe],
        config["spot_start"],
        config["spot_end"],
        args.spot_dataset,
        args.max_spot_cost,
    )
    qualified, candidates, effective_notional, capped, coverage_exclusions = build_spot_candidates(
        history,
        universe,
        args.daily_notional_min,
        args.candidate_target,
        config["window_start"],
        config["week_dates"],
        config["anchor_label"],
        config["anchor_field"],
        liquidity_manifest,
    )
    pairs_by_ticker, option_diagnostics, option_quote_records, option_cost, option_cost_source, parents = fetch_option_pairs(
        option_client,
        candidates,
        config["option_snapshot_start"],
        config["option_snapshot_end"],
        option_expiry,
        args.max_option_cost,
        args.option_cost_estimate,
        args.option_chunk_size,
        config["option_cache_dir"],
    )
    ranked, top, manifest, option_year_fraction, realized_year_fraction = rank_by_implied_move(
        candidates,
        pairs_by_ticker,
        option_expiry,
        config["option_expiry_close"],
        config["option_valuation_time"],
        config["endpoint_time"],
        args.limit,
        args.ranking_horizon,
        args.direction,
    )

    result = {
        "schema_version": "2.0",
        "analysis_as_of": str(pd.Timestamp.now(tz="UTC").date()),
        "screen_key": (
            f"NASDAQ-{config['window_start']}-{config['window_end']}-{config['anchor_field'].upper()}-OPTION-IV"
        ),
        "requested_window": {
            "start_date": str(config["window_start"]),
            "end_date": str(config["window_end"]),
        },
        "universe": {
            "source": "Nasdaq public stock screener",
            "raw_listing_count": raw_count,
            "market_cap_min_usd": args.market_cap_min,
            "instrument_scope": "Common stocks, ordinary shares, and ADRs; excludes preferreds, warrants, rights, and units.",
            "eligible_after_market_cap_and_type": len(universe),
            "excluded_counts": exclusions,
        },
        "pre_option_filter": {
            "requested_minimum_notional_each_session_usd": args.daily_notional_min,
            "candidate_target": args.candidate_target,
            "symbols_passing_fixed_floors": len(qualified),
            "symbols_sent_to_options": len(candidates),
            "target_cap_applied": capped,
            "effective_minimum_notional_usd": effective_notional,
            "cap_tie_break": "Minimum daily notional descending, then market capitalization descending.",
            "notional_formula": (
                liquidity_manifest.get("notional_formula")
                if liquidity_manifest
                else (
                    "Databento EQUS.SUMMARY close * consolidated volume"
                    if args.spot_dataset == "EQUS.SUMMARY"
                    else "Databento EQUS.MINI last regular-session close * blended regular-session volume"
                )
            ),
            "liquidity_manifest": str(args.liquidity_manifest) if args.liquidity_manifest else None,
            "sessions": [str(trade_date) for trade_date in config["week_dates"]],
            "coverage_exclusions": coverage_exclusions,
        },
        "ranking_method": {
            "governing_volatility": f"Forward-ATM option-implied volatility at {config['option_valuation_time']}",
            "option_model": "Undiscounted Black-76 on the option-implied forward; paired call-put mids across up to five near-forward strikes.",
            "year_fraction": "ACT/365 from option snapshot cutoff to expiration close",
            "option_year_fraction": option_year_fraction,
            "realized_year_fraction": realized_year_fraction,
            "return_type": (
                f"Cumulative log return from the {config['window_start']} "
                f"{config['anchor_label']} "
                "to each daily close"
            ),
            "return_anchor_label": config["anchor_label"],
            "path_score": "Both full-option-horizon and elapsed-time option-implied z-scores are retained.",
            "rank_field": (
                "endpoint full-option-horizon implied z-score"
                if args.ranking_horizon == "option_expiry"
                else "endpoint elapsed-time option-implied z-score"
            ),
            "direction": args.direction,
            "path_interpretation": "Elapsed-time sigma scales annualized snapshot IV by sqrt(ACT/365); this assumes variance accrues uniformly through the realized window.",
            "historical_volatility_role": "Not used in candidate ranking or sigma calculation.",
            "iv_reliability": {
                "high": "At least three IV strikes, positive call and put bids, and ATM total spread / straddle mid <= 25%.",
                "medium": "At least two IV strikes, positive call and put bids, and ATM total spread / straddle mid <= 75%.",
                "low": "Anything else that still produces a mathematically valid IV; review before use.",
            },
        },
        "market_data": {
            "provider": "Databento",
            "spot": {
                "dataset": args.spot_dataset,
                "schema": spot_schema,
                "start": config["spot_start"],
                "end_exclusive": config["spot_end"],
                "session_filter": (
                    "09:30:00 <= America/New_York bar start < 16:00:00; locally aggregated to one row per session"
                    if args.spot_dataset == "EQUS.MINI"
                    else None
                ),
                "estimated_download_cost_usd": spot_cost,
            },
            "options": {
                "dataset": "OPRA.PILLAR",
                "schema": "cbbo-1m",
                "snapshot_start": config["option_snapshot_start"],
                "snapshot_end_exclusive": config["option_snapshot_end"],
                "snapshot_observation_time": config["option_valuation_time"],
                "expiry": str(option_expiry),
                "parent_symbol_count": len(parents),
                "estimated_download_cost_usd": option_cost,
                "cost_estimate_source": option_cost_source,
            },
            "total_estimated_download_cost_usd": spot_cost + option_cost,
        },
        "coverage": {
            "symbols_with_spot_records": int(history["symbol"].nunique()),
            "option_quote_records": option_quote_records,
            "symbols_with_paired_expiry_quotes": len(pairs_by_ticker),
            "symbols_with_valid_option_iv": len(ranked),
            "symbols_without_valid_option_iv": [
                row["ticker"] for row in manifest if row["option_status"] == "no_valid_iv"
            ],
            "option_record_diagnostics": option_diagnostics,
            "result_count": len(top),
        },
        "candidate_manifest": manifest,
        "results": top,
        "caveats": [
            "Market capitalization is point-in-time metadata from Nasdaq's public screener, while returns, liquidity, and option quotes are from Databento.",
            *(
                ["EQUS.MINI spot values are derived from a subset of US equity venues. The opening price is the first observed blended regular-session trade, the close is the last observed trade before 16:00 ET, and volume is not full consolidated volume."]
                if args.spot_dataset == "EQUS.MINI"
                else []
            ),
            f"The broad screen uses a one-minute consolidated option snapshot ending at {config['option_valuation_time']}; IV Reliability fields should be reviewed before treating a sigma as executable.",
            "Databento get_cost can overestimate requests shorter than ten minutes; the stored value is a preflight estimate, while billing is based on bytes delivered.",
            "The elapsed-time score is horizon matched but assumes snapshot IV accrues uniformly; scheduled-event variance may be concentrated rather than smooth.",
            "The full-option-horizon score is also retained as a conservative comparison that does not allocate variance across days.",
            "Stocks without a valid option estimate are excluded rather than substituted with historical volatility.",
            "Risk-adjusted movement is a screening statistic, not an estimate of tradable alpha or investment merit.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.csv_output:
        write_csv(args.csv_output, top)
    if args.candidate_csv_output:
        write_csv(args.candidate_csv_output, manifest)
    print(json.dumps({
        "raw_listings": raw_count,
        "eligible_symbols": len(universe),
        "fixed_floor_symbols": len(qualified),
        "option_candidates": len(candidates),
        "effective_minimum_daily_notional_usd": effective_notional,
        "spot_download_cost_usd": spot_cost,
        "option_download_cost_usd": option_cost,
        "option_quote_records": option_quote_records,
        "symbols_with_valid_option_iv": len(ranked),
        "top": [{key: value for key, value in row.items() if key != "path"} for row in top],
    }, indent=2))


if __name__ == "__main__":
    main()
