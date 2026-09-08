#!/usr/bin/env python3
"""Build the next-session live universe from an existing valid-IV screen."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", type=Path, required=True)
    parser.add_argument("--company-basics", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def build_payload(screen: dict, company_basics: dict | None = None) -> dict:
    descriptions = (company_basics or {}).get("companies", {})
    rows = []
    for candidate in screen["candidate_manifest"]:
        if candidate.get("option_status") != "valid_iv":
            continue
        ticker = candidate["ticker"]
        company = descriptions.get(ticker, {})
        description = company.get("company_description")
        if not description:
            description = f"Operates in the {candidate['industry']} industry."
        rows.append({
            "ticker": ticker,
            "company_name": candidate["company_name"],
            "sector": candidate["sector"],
            "industry": candidate["industry"],
            "company_description": description,
            "market_cap_usd": candidate["market_cap_usd"],
            "anchor_price": candidate["end_close"],
            "prior_close_price": candidate["anchor_price"],
            "live_anchor_price": candidate["end_close"],
            "last_price": candidate["end_close"],
            "gain_pct": candidate["weekly_return_pct"],
            "log_return_pct": candidate["weekly_log_return_pct"],
            "z_score": candidate["realized_horizon_implied_z"],
            "sigma_1d_pct": candidate["realized_horizon_implied_sigma_pct"],
            "annualized_implied_vol_pct": candidate["annualized_implied_vol_pct"],
            "iv_reliability": candidate["iv_reliability"],
            "iv_expansion_reference": {
                "method": "moneyness-matched-black76-v1",
                "annualized_implied_vol_pct": candidate["annualized_implied_vol_pct"],
                "spot_price": candidate["anchor_price"],
                "forward_price": candidate.get("iv_reference_forward_price"),
                "strikes": candidate.get("iv_reference_strikes", []),
                "log_moneyness": candidate.get("iv_reference_log_moneyness", []),
                "option_expiry": screen["market_data"]["options"]["expiry"],
                "observation_time": screen["market_data"]["options"]["snapshot_observation_time"],
                "requested_session": screen["requested_window"]["end_date"],
                "reference_session": screen["requested_window"]["start_date"],
                "reference_status": "fallback",
                "fallback_sessions": 1,
                "baseline_revision": 1,
            },
            "iv_chain": {
                "occ_root": candidate.get("iv_occ_root"),
                "strikes": candidate.get("iv_chain_strikes", []),
            },
            "outside_one_sigma": abs(candidate["realized_horizon_implied_z"]) > 1,
            "prior_session_date": screen["requested_window"]["start_date"],
            "prior_session_gain_pct": candidate["weekly_return_pct"],
            "prior_session_z_score": candidate["realized_horizon_implied_z"],
        })

    rows.sort(key=lambda row: row["ticker"])
    reliability_mix = Counter(row["iv_reliability"] for row in rows)
    return {
        "schema_version": "1.0",
        "view": "today_movers",
        "data_date": screen["requested_window"]["end_date"],
        "anchor_date": screen["requested_window"]["end_date"],
        "prior_session_date": screen["requested_window"]["start_date"],
        "last_update_utc": f"{screen['requested_window']['end_date']}T20:00:00+00:00",
        "price_source": screen["market_data"]["spot"]["dataset"],
        "iv_snapshot_time": screen["market_data"]["options"]["snapshot_observation_time"],
        "option_expiry": screen["market_data"]["options"]["expiry"],
        "valid_iv_coverage": len(rows),
        "reliability_mix": dict(reliability_mix),
        "live_calculation": {
            "version": "log-return-over-option-sigma-v1",
            "anchor_date": screen["requested_window"]["end_date"],
            "anchor_description": "Prior regular-session close",
            "sigma_field": "sigma_1d_pct",
            "ranking": "absolute live z-score",
        },
        "rows": rows,
    }


def main() -> None:
    args = parse_args()
    screen = json.loads(args.screen.read_text())
    company_basics = (
        json.loads(args.company_basics.read_text()) if args.company_basics else None
    )
    payload = build_payload(screen, company_basics)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({
        "anchor_date": payload["anchor_date"],
        "row_count": len(payload["rows"]),
        "reliability_mix": payload["reliability_mix"],
    }, indent=2))


if __name__ == "__main__":
    main()
