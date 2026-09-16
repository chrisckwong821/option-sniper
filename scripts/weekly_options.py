"""Import OCC's current weekly-product list and restrict the serving universe."""
import argparse
import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

SOURCE_URL = "https://marketdata.theocc.com/weekly-options?action=download"
SOURCE_PAGE = "https://www.theocc.com/market-data/market-data-reports/series-and-trading-data/weekly-and-quarterly-options"


def parse_report(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not rows[0]:
        raise ValueError("Empty OCC report")
    as_of = re.search(r"as of (\d{2}/\d{2}/\d{4})", rows[0][0])
    if not as_of:
        raise ValueError("Missing OCC report date")
    expiry = next((re.search(r"for (\d{2}/\d{2}/\d{4}) Expiration", r[1])
                   for r in rows if len(r) > 1 and "Weekly Options Summary for" in r[1]), None)
    if not expiry:
        raise ValueError("Missing current weekly expiration")
    header = next((i for i, r in enumerate(rows) if r[:3] == ["SYMBOL", "OPTION NAME", "EFFECTIVE DATE"]), None)
    if header is None:
        raise ValueError("Missing OCC product header")
    symbols = set()
    for row in rows[header + 1:]:
        if not row or not row[0].strip():
            continue
        symbol = row[0].strip().upper()
        if not re.fullmatch(r"[A-Z0-9. /-]+", symbol) or len(row) < 3:
            raise ValueError("Unexpected OCC product row")
        datetime.strptime(row[2].strip(), "%m/%d/%Y")
        symbols.add(symbol)
    if not symbols:
        raise ValueError("Empty OCC weekly list")
    return {
        "status": "available", "source_url": SOURCE_PAGE, "download_url": SOURCE_URL,
        "as_of": datetime.strptime(as_of[1], "%m/%d/%Y").date().isoformat(),
        "expiration": datetime.strptime(expiry[1], "%m/%d/%Y").date().isoformat(),
        "symbols": sorted(symbols),
    }


def load_report(path):
    try:
        data = json.loads(Path(path).read_text())
        if data.get("status") != "available" or not data.get("symbols"):
            raise ValueError("Invalid weekly list")
        if not isinstance(data["symbols"], list) or not all(isinstance(s, str) and s for s in data["symbols"]):
            raise ValueError("Invalid weekly symbols")
        for key in ("as_of", "expiration"):
            datetime.strptime(data[key], "%Y-%m-%d")
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "unavailable", "symbols": []}


def normalize_root(symbol):
    return re.sub(r"[. /-]", "", str(symbol).strip().upper())


def weekly_universe(bundle, report):
    """Filter before building stock/option subscriptions; never widen on failure."""
    if report.get("status") != "available" or not report.get("symbols"):
        raise ValueError("A valid OCC weekly list is required; run python -m scripts.weekly_options")
    symbols = {normalize_root(s) for s in report["symbols"]}
    result = {**bundle}
    counts = {}
    for key in ("today", "snapshot"):
        original = bundle[key]
        rows = [r for r in original["rows"] if normalize_root(
            (r.get("iv_chain") or {}).get("occ_root") or r["ticker"]) in symbols]
        counts[key] = {"input": len(original["rows"]), "retained": len(rows),
                       "excluded": len(original["rows"]) - len(rows)}
        result[key] = {**original, "rows": rows, "valid_iv_coverage": len(rows),
                       "reliability_mix": dict(Counter(r.get("iv_reliability", "unknown") for r in rows))}
    if not result["today"]["rows"]:
        raise ValueError("OCC weekly list matches no current stocks; refusing unrestricted subscriptions")
    result["weekly_options"] = {**report, "enforced": True, "coverage": counts}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Previously downloaded OCC CSV report")
    parser.add_argument("--output", type=Path, default=Path(".cache/sigma-tape/occ-weekly-options.json"))
    parser.add_argument("--ensure", action="store_true", help="Reuse an existing validated list; download only if unavailable")
    args = parser.parse_args()
    if args.ensure and not args.input:
        cached = load_report(args.output)
        if cached["status"] == "available":
            print(f"Using installed OCC report {cached['as_of']}: {len(cached['symbols'])} weekly products")
            return
    if args.input:
        raw = args.input.read_bytes()
    else:
        with urlopen(SOURCE_URL, timeout=30) as response:
            raw = response.read()
    data = parse_report(raw.decode("utf-8-sig"))
    data.update(retrieved_at=datetime.now(UTC).isoformat(), sha256=hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(args.output)
    print(f"OCC report {data['as_of']}: {len(data['symbols'])} weekly products")


if __name__ == "__main__":
    main()
