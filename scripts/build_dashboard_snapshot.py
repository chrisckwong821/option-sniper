#!/usr/bin/env python3
"""Build the saved-gainer dashboard payload from a top-mover screen."""

import argparse
import json
from collections import Counter
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", type=Path, required=True)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def build_payload(screen: dict, validation: dict | None = None) -> dict:
    validation = validation or {}
    comparisons = validation.get("comparisons") or []

    rows = []
    for result in screen["results"]:
        rows.append({
            "original_rank": result["rank"],
            "ticker": result["ticker"],
            "company_name": result["company_name"],
            "sector": result["sector"],
            "company_description": result.get("company_description")
            or f"Operates in the {result.get('industry') or result['sector']} industry.",
            "market_cap_usd": result["market_cap_usd"],
            "selection_anchor_price": result["anchor_price"],
            "live_anchor_price": result["end_close"],
            "last_price": result["end_close"],
            "gain_pct": result["weekly_return_pct"],
            "sigma_1d_pct": result["realized_horizon_implied_sigma_pct"],
            "z_score": result["realized_horizon_implied_z"],
            "iv_reliability": result["option_snapshot"]["iv_reliability"],
            "outside_one_sigma": result["realized_horizon_implied_z"] > 1,
        })

    reliability_mix = Counter(row["iv_reliability"] for row in rows)
    return {
        "schema_version": "1.0",
        "data_date": screen["requested_window"]["end_date"],
        "anchor_date": screen["requested_window"]["start_date"],
        "last_update_utc": (
            comparisons[0].get("last_bar_timestamp_utc")
            if comparisons
            else f"{screen['requested_window']['end_date']}T20:00:00+00:00"
        ),
        "price_source": screen["market_data"]["spot"]["dataset"],
        "iv_snapshot_time": screen["market_data"]["options"]["snapshot_observation_time"],
        "option_expiry": screen["market_data"]["options"]["expiry"],
        "live_calculation": {
            "version": "log-return-over-option-sigma-v1",
            "anchor_date": screen["requested_window"]["end_date"],
            "anchor_description": "Saved closing snapshot for the next trading session",
            "sigma_field": "sigma_1d_pct",
        },
        "valid_iv_coverage": screen["coverage"]["symbols_with_valid_option_iv"],
        "reliability_mix": dict(reliability_mix),
        "rows": rows,
    }


def main():
    args = parse_args()
    screen = json.loads(args.screen.read_text())
    validation = json.loads(args.validation.read_text()) if args.validation else None
    payload = build_payload(screen, validation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({
        "data_date": payload["data_date"],
        "row_count": len(payload["rows"]),
        "last_update_utc": payload["last_update_utc"],
    }, indent=2))


if __name__ == "__main__":
    main()
