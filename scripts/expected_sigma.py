"""Shared previous-close sigma, activated at trading-day pre-market.

Closing inputs and published snapshots are independent of dashboard/list caches.
No intraday option observation is allowed to replace a closing observation.
"""
from __future__ import annotations

import copy
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import exchange_calendars as xcals
import ijson
import pandas as pd

from .dashboard_state import atomic_write_json
from .shared_cache import SharedCache, SharedCacheError, digest
from .databento_config import resolve_live_option_api_key, resolve_historical_api_keys
from .nasdaq_universe_movers import estimate_option_iv, prepare_option_pairs, fetch_spot

EASTERN = ZoneInfo("America/New_York")
VERSION = "previous-close-premarket-min2dte-v2"
YEAR_SECONDS = 365 * 86400
PRIMARY_CLOSE_WINDOW_MINUTES = 1
FALLBACK_CLOSE_WINDOW_MINUTES = 15


def sigma_plan(now=None):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(now.date() - timedelta(days=14), now.date() + timedelta(days=14))
    active = max(s for s in sessions if datetime.combine(s.date(), datetime.min.time(), EASTERN).replace(hour=4) <= now)
    previous = calendar.previous_session(active)
    close = calendar.session_close(previous).to_pydatetime()
    end = calendar.session_close(active).to_pydatetime()
    from .iv_expansion_history import option_expiry_for_observation
    expiry = pd.Timestamp(option_expiry_for_observation(active.date(), calendar=calendar))
    return {
        "version": VERSION,
        "effective_session": str(active.date()),
        "iv_session": str(previous.date()),
        "iv_snapshot_time": close.isoformat(),
        "horizon_end": end.isoformat(),
        "year_fraction": (end - close).total_seconds() / YEAR_SECONDS,
        "option_expiry": str(expiry.date()),
        "expiry_close": calendar.session_close(expiry).to_pydatetime().isoformat(),
    }


def next_activation(now):
    active = sigma_plan(now)
    if now < datetime.fromisoformat(active["horizon_end"]):
        return None
    calendar = xcals.get_calendar("XNYS")
    session = calendar.next_session(active["effective_session"])
    return datetime.combine(session.date(), datetime.min.time(), EASTERN).replace(hour=4)


class _FiniteJSONReader:
    """Normalize Python JSON's non-finite literals in bounded binary chunks.

    Old worker caches can contain NaN bids/asks. Strict streaming parsers reject
    those even in discarded quotes. Treat invalid numeric values as null while
    leaving quoted strings (including escaped quotes) unchanged.
    """
    tokens = re.compile(rb'"|\\[\s\S]|(?<![\w])(?:-?Infinity|NaN)(?![\w])')

    def __init__(self, stream):
        self.chunks = self._chunks(stream)
        self.pending = b""

    def _chunks(self, stream):
        carry, in_string = b"", False
        while True:
            block = stream.read(65536)
            data = carry + block
            # Retain enough bytes to resolve literals/escapes across boundaries.
            end = max(0, len(data) - 10) if block else len(data)
            pieces, start = [], 0
            for match in self.tokens.finditer(data):
                if match.start() >= end:
                    break
                if match.end() > end:
                    end = match.start()
                    break
                token = match.group()
                if token == b'"':
                    in_string = not in_string
                elif not in_string and token[:1] != b'\\':
                    pieces.extend((data[start:match.start()], b"null"))
                    start = match.end()
            pieces.append(data[start:end])
            if end:
                yield b"".join(pieces)
            carry = data[end:]
            if not block:
                return

    def read(self, size=65536):
        if size == 0:
            return b""
        if not self.pending:
            self.pending = next(self.chunks, b"")
        result, self.pending = self.pending[:size], self.pending[size:]
        return result


def read_json(path, *, exclude_quotes=False):
    try:
        if exclude_quotes:
            # Serving processes need the worker's finished rows and spot anchors,
            # never its hundreds of thousands of raw reconstruction quotes.
            from ijson.common import ObjectBuilder
            builder = ObjectBuilder()
            with path.open("rb") as stream:
                for prefix, event, value in ijson.parse(_FiniteJSONReader(stream), use_float=True):
                    if ((prefix in ("", "inputs") and event == "map_key" and value == "quotes")
                            or prefix in ("quotes", "inputs.quotes")
                            or prefix.startswith(("quotes.", "inputs.quotes."))):
                        continue
                    builder.event(event, value)
            return builder.value if isinstance(builder.value, dict) else {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, ijson.JSONError):
        return {}


def validated_rows(saved, plan):
    if saved.get("plan") != plan:
        return {}
    rows = {}
    for ticker, row in saved.get("rows", {}).items():
        try:
            iv, sigma = float(row["annualized_implied_vol_pct"]), float(row["sigma_1d_pct"])
            if (math.isfinite(iv) and math.isfinite(sigma) and iv > 0 and sigma > 0
                    and math.isclose(sigma, iv * math.sqrt(plan["year_fraction"]), rel_tol=1e-9)):
                rows[ticker] = row
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def merge_closing_inputs(previous, incoming):
    """Union independent ticker coverage without dropping another writer's quotes."""
    def verified(value):
        return value.get("source") != "live_replay" or value.get("replay_verified")
    if not verified(previous):
        previous = {"spots": previous.get("spots", {})}
    if not verified(incoming):
        incoming = {"spots": incoming.get("spots", {})}
    quotes = {(q["symbol"], q["ts_recv"]): q
              for value in (previous, incoming) for q in value.get("quotes", [])}
    return {**previous, **incoming,
            "quotes": list(quotes.values()),
            "spots": {**previous.get("spots", {}), **incoming.get("spots", {})},
            "requested_tickers": sorted(set(previous.get("requested_tickers", [])) | set(incoming.get("requested_tickers", []))),
            "fallback_requested_tickers": sorted(
                set(previous.get("fallback_requested_tickers", []))
                | set(incoming.get("fallback_requested_tickers", []))
            )}


def latest_closing_estimates(quotes, candidates, option_expiry, close, window_minutes,
                             year_fraction):
    """Fit each ticker at its newest minute that yields a valid IV estimate."""
    if quotes.empty or "ts_recv" not in quotes.columns:
        return {}, {}
    frame = quotes.copy()
    frame["ts_recv"] = pd.to_datetime(frame["ts_recv"], utc=True, format="mixed")
    close_stamp = pd.Timestamp(close)
    frame = frame[
        (frame["ts_recv"] >= close_stamp - pd.Timedelta(minutes=window_minutes))
        & (frame["ts_recv"] <= close_stamp)
    ]
    if frame.empty:
        return {}, {}
    remaining = {row["ticker"]: row for row in candidates}
    selected, selected_times = {}, {}
    for stamp in sorted(frame["ts_recv"].unique(), reverse=True):
        if not remaining:
            break
        minute = frame[frame["ts_recv"] == stamp]
        pairs, _ = prepare_option_pairs(minute, list(remaining.values()), option_expiry)
        for ticker, candidate in list(remaining.items()):
            estimate = estimate_option_iv(candidate, pairs.get(ticker, []), year_fraction)
            if estimate:
                selected[ticker] = estimate
                selected_times[ticker] = pd.Timestamp(stamp).isoformat()
                remaining.pop(ticker, None)
    return selected, selected_times


def replay_quotes(key, tickers, close, timeout=60, *, contracts=None, expiry=None):
    """Bounded live replay of closing CBBOs; never accept post-close quotes."""
    client = db.Live(key=key, heartbeat_interval_s=5)
    mappings, quotes, errors = {}, {}, []
    complete = False
    start = close - timedelta(seconds=60)
    expiry_code = pd.Timestamp(expiry).strftime("%y%m%d") if expiry else None

    def receive(record):
        nonlocal complete
        if isinstance(record, db.SymbolMappingMsg):
            symbol = str(record.stype_out_symbol).rstrip("\x00")
            if expiry_code is None or symbol[-15:-9] == expiry_code:
                mappings[record.instrument_id] = symbol
            else:
                mappings.pop(record.instrument_id, None)
        elif isinstance(record, db.ErrorMsg):
            errors.append(str(record.msg))
            client.stop()
        elif isinstance(record, db.SystemMsg) and record.code == 3:
            complete = True
            client.stop()
        elif hasattr(record, "pretty_bid_px_00"):
            stamp = datetime.fromtimestamp(record.ts_recv / 1e9, UTC)
            if stamp > close:
                # Multiple subscription chunks can interleave. A post-close
                # record is not proof that every chunk finished its replay.
                return
            symbol = mappings.get(record.instrument_id)
            if symbol and start <= stamp <= close:
                quotes[symbol] = {"ts_recv": stamp.isoformat(), "symbol": symbol,
                                  "bid_px_00": float(record.pretty_bid_px_00),
                                  "ask_px_00": float(record.pretty_ask_px_00)}

    try:
        contracts = contracts or {}
        raw = [symbol for ticker in tickers for symbol in contracts.get(ticker, [])]
        parents = [f"{ticker}.OPT" for ticker in tickers if not contracts.get(ticker)]
        for stype, symbols in (("raw_symbol", raw), ("parent", parents)):
            for offset in range(0, len(symbols), 1000):
                client.subscribe(dataset="OPRA.PILLAR", schema="cbbo-1m", stype_in=stype,
                                 symbols=symbols[offset:offset + 1000], start=start)
        client.add_callback(receive)
        client.start()
        client.block_for_close(timeout=timeout)
    finally:
        client.terminate()
    if errors:
        raise RuntimeError("Option close replay failed: " + errors[0])
    if not complete:
        raise RuntimeError("Option close replay did not finish before its timeout")
    return list(quotes.values())


def historical_quotes(key, tickers, close, max_cost, *, expiry=None, contracts=None,
                      window_minutes=PRIMARY_CLOSE_WINDOW_MINUTES, cost_tracker=None):
    contracts = contracts or {}
    requests = []
    for offset in range(0, len(tickers), 20):
        chunk = tickers[offset:offset + 20]
        raw = [symbol for ticker in chunk for symbol in contracts.get(ticker, [])]
        parents = [f"{ticker}.OPT" for ticker in chunk if not contracts.get(ticker)]
        for stype, symbols in (("raw_symbol", raw), ("parent", parents)):
            if symbols:
                requests.append(dict(dataset="OPRA.PILLAR", schema="cbbo-1m", stype_in=stype,
                                     symbols=symbols, start=close - timedelta(minutes=window_minutes),
                                     end=close + timedelta(seconds=1)))
    # Preflight the total before downloading any chunk. A whole-universe parent
    # query can time out even for a single closing minute.
    with ThreadPoolExecutor(max_workers=4) as pool:
        costs = list(pool.map(lambda request: float(db.Historical(key).metadata.get_cost(**request)), requests))
    total = sum(costs)
    spent = sum(cost_tracker or [])
    if not math.isfinite(total) or total + spent > max_cost:
        raise RuntimeError("Closing option quote request exceeds configured cost limit")
    if cost_tracker is not None:
        cost_tracker.append(total)

    def download(request):
        frame = db.Historical(key).timeseries.get_range(**request).to_df().reset_index()
        if frame.empty:
            return []
        if expiry:
            code = pd.Timestamp(expiry).strftime("%y%m%d")
            frame = frame[frame["symbol"].str[-15:-9] == code]
        frame = frame[["ts_recv", "symbol", "bid_px_00", "ask_px_00"]]
        return json.loads(frame.to_json(orient="records", date_format="iso"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        chunks = list(pool.map(download, requests))
    return [quote for chunk in chunks for quote in chunk]


class ExpectedSigma:
    def __init__(self, root: Path, *, max_option_cost=5.0, max_spot_cost=1.0,
                 serving_only=False):
        self.root = root / "expected-sigma-min2dte-v2"
        self.max_option_cost = max_option_cost
        self.max_spot_cost = max_spot_cost
        self.serving_only = serving_only
        self._shared_signatures = {}
        self._closing_inputs = None
        self.error = None
        self.shared = SharedCache(root)

    def sync_shared(self, plan):
        """Restore a complete publication before any paid data request.

        Read errors are not cache misses. Keep serving local state and let the
        background loop retry without accidentally charging for a replacement.
        """
        remote = self.root / "remote.json"
        if not self.shared.get("expected-sigma-min2dte-v2/" + plan["effective_session"] + ".json", remote):
            return
        path = self.root / (plan["effective_session"] + ".json")
        inputs_path = self.root / ("close-" + plan["iv_session"] + ".json")
        def local_signature():
            return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                         for p in (path, inputs_path))
        remote_digest = digest(remote) if self.serving_only else None
        if (self.serving_only and self._shared_signatures.get(plan["effective_session"])
                == (remote_digest, local_signature())):
            return
        saved = read_json(remote, exclude_quotes=self.serving_only)
        if saved.get("plan") != plan or not isinstance(saved.get("inputs"), dict):
            raise SharedCacheError("Invalid shared expected-sigma provenance")
        local = read_json(path)
        rows = {**validated_rows(local, plan), **validated_rows(saved, plan)}
        inputs = read_json(inputs_path, exclude_quotes=self.serving_only)
        incoming = saved["inputs"]
        atomic_write_json(inputs_path, merge_closing_inputs(inputs, incoming))
        # Publish readiness last: readers must see closing anchors before fresh sigma.
        atomic_write_json(path, {"plan": plan, "rows": rows, "source": saved.get("source")})
        if self.serving_only:
            self._shared_signatures[plan["effective_session"]] = (remote_digest, local_signature())

    def publish(self, plan):
        if self.serving_only:
            raise RuntimeError("Serving instances cannot publish expected sigma")
        saved = read_json(self.root / (plan["effective_session"] + ".json"))
        if not validated_rows(saved, plan):
            return
        publication = {"plan": plan, "rows": validated_rows(saved, plan), "source": saved.get("source"),
                       "inputs": read_json(self.root / ("close-" + plan["iv_session"] + ".json"))}
        path = self.root / "publication.json"
        atomic_write_json(path, publication)
        def merge(previous):
            if previous and previous.get("plan") != plan:
                raise SharedCacheError("Invalid shared expected-sigma provenance")
            return {**publication, "rows": {**validated_rows(previous, plan), **publication["rows"]},
                    "inputs": merge_closing_inputs(previous.get("inputs", {}), publication["inputs"])}
        self.shared.update_json("expected-sigma-min2dte-v2/" + plan["effective_session"] + ".json", merge)

    def refresh(self, bundle, now=None, *, effective_at=None, regular_prices=None):
        """Prepare the active session. Called off the server event loop."""
        if self.serving_only:
            raise RuntimeError("Serving instances cannot reconstruct expected sigma")
        now = now or datetime.now(UTC)
        plan = sigma_plan(effective_at or now)
        tickers = sorted({r["ticker"] for view in ("today", "snapshot") for r in bundle[view]["rows"]})
        from .iv_expansion import format_occ_symbol
        contracts = {}
        for view in ("snapshot", "today"):
            for row in bundle[view]["rows"]:
                chain, reference = row.get("iv_chain") or {}, row.get("iv_expansion_reference") or {}
                if chain.get("occ_root") and reference.get("option_expiry") == plan["option_expiry"]:
                    contracts[row["ticker"]] = [format_occ_symbol(chain["occ_root"], plan["option_expiry"], side, strike)
                                                for strike in chain.get("strikes", []) for side in ("C", "P")]
        path = self.root / (plan["effective_session"] + ".json")
        self.sync_shared(plan)
        saved = read_json(path)
        cached_rows = validated_rows(saved, plan)
        if all(t in cached_rows for t in tickers):
            self.publish(plan)
            self.error = None
            return
        # Partial current coverage shares the same explicitly stale fallback
        # across fresh instances, instead of inheriting different list caches.
        activation = datetime.combine(datetime.fromisoformat(plan["effective_session"]).date(),
                                      datetime.min.time(), EASTERN).replace(hour=4)
        previous_plan = sigma_plan(activation - timedelta(seconds=1))
        previous_path = self.root / (previous_plan["effective_session"] + ".json")
        if not previous_path.exists():
            self.sync_shared(previous_plan)
        close = datetime.fromisoformat(plan["iv_snapshot_time"])
        if close > now:
            raise RuntimeError("Cannot prepare IV before the reference session closes")
        inputs_path = self.root / ("close-" + plan["iv_session"] + ".json")
        inputs = read_json(inputs_path)
        spots = inputs.get("spots", {})
        for ticker, point in (regular_prices or {}).items():
            stamp = datetime.fromisoformat(point["ts_event"])
            if (point.get("trading_date") == plan["iv_session"]
                    and close - timedelta(minutes=1) <= stamp <= close
                    and math.isfinite(float(point["price"])) and float(point["price"]) > 0):
                spots.setdefault(ticker, float(point["price"]))
        for view in ("today", "snapshot"):
            payload = bundle[view]
            for row in payload["rows"]:
                if payload.get("data_date") == plan["iv_session"]:
                    value = row.get("last_price")
                    if value and math.isfinite(float(value)) and float(value) > 0:
                        spots.setdefault(row["ticker"], float(value))
        spot_key, historical_key = resolve_historical_api_keys()
        option_costs = []
        atomic_write_json(inputs_path, {**inputs, "spots": spots})
        quotes = inputs.get("quotes", [])
        covered = set(inputs.get("requested_tickers", []))
        if inputs.get("source") == "live_replay" and not inputs.get("replay_verified"):
            covered = set()
        missing_quotes = sorted(set(tickers) - covered)
        if missing_quotes:
            live_key = resolve_live_option_api_key()
            quotes = []
            replay_error = None
            if live_key and now - (close - timedelta(minutes=1)) < timedelta(hours=24):
                try:
                    quotes = replay_quotes(live_key, missing_quotes, close, contracts=contracts,
                                           expiry=plan["option_expiry"])
                    source = "live_replay"
                except Exception as exc:
                    replay_error = exc
            if not quotes:
                if not historical_key:
                    raise RuntimeError("Closing option quotes unavailable; historical fallback key is missing") from replay_error
                quotes = historical_quotes(historical_key, missing_quotes, close, self.max_option_cost,
                                           expiry=plan["option_expiry"], contracts=contracts,
                                           cost_tracker=option_costs)
                source = "historical"
            if not quotes:
                raise RuntimeError("No closing option quotes returned")
            inputs = merge_closing_inputs(inputs, {"spots": spots, "quotes": quotes,
                "requested_tickers": missing_quotes, "source": source, "replay_verified": source == "live_replay"})
            quotes = inputs["quotes"]
            atomic_write_json(inputs_path, inputs)
        # Save OPRA replay first, even if the equity daily bars are not yet
        # available. A delayed spot response must not lose the replay window.
        missing = [t for t in tickers if t not in spots]
        if missing:
            if not spot_key and not any(t in spots for t in tickers):
                raise RuntimeError("Previous closing stock prices unavailable; spot API key is missing")
            if spot_key:
                bars, _, _ = fetch_spot(db.Historical(spot_key), missing, plan["iv_session"],
                                       str(close.date() + timedelta(days=1)), "EQUS.SUMMARY", self.max_spot_cost)
                for _, row in bars.iterrows():
                    if str(row["date"]) == plan["iv_session"] and float(row["close"]) > 0:
                        spots[row["symbol"]] = float(row["close"])
                atomic_write_json(inputs_path, {**inputs, "spots": spots})
        candidates = [{"ticker": t, "anchor_price": spots[t]} for t in tickers if t in spots]
        expiry = datetime.fromisoformat(plan["expiry_close"])
        expiry_date = pd.Timestamp(plan["option_expiry"]).date()
        years = (expiry - close).total_seconds() / YEAR_SECONDS
        pending = [candidate for candidate in candidates if candidate["ticker"] not in cached_rows]
        primary_estimates, quote_times = latest_closing_estimates(
            pd.DataFrame(quotes), pending, expiry_date, close,
            PRIMARY_CLOSE_WINDOW_MINUTES, years,
        )
        estimates = {
            ticker: (estimate, quote_times.get(ticker))
            for ticker, estimate in primary_estimates.items()
        }

        unresolved = [candidate for candidate in pending if candidate["ticker"] not in estimates]
        fallback_covered = set(inputs.get("fallback_requested_tickers", []))
        fallback_missing = sorted(
            candidate["ticker"] for candidate in unresolved
            if candidate["ticker"] not in fallback_covered
        )
        if fallback_missing and historical_key:
            fallback_contracts = {ticker: contracts.get(ticker, []) for ticker in fallback_missing}
            fallback_quotes = historical_quotes(
                historical_key, fallback_missing, close, self.max_option_cost,
                expiry=plan["option_expiry"], contracts=fallback_contracts,
                window_minutes=FALLBACK_CLOSE_WINDOW_MINUTES, cost_tracker=option_costs,
            )
            inputs = merge_closing_inputs(inputs, {
                "spots": spots,
                "quotes": fallback_quotes,
                "requested_tickers": [],
                "fallback_requested_tickers": fallback_missing,
                "source": "historical",
            })
            quotes = inputs["quotes"]
            atomic_write_json(inputs_path, inputs)

        if unresolved:
            fallback_estimates, fallback_times = latest_closing_estimates(
                pd.DataFrame(quotes), unresolved, expiry_date, close,
                FALLBACK_CLOSE_WINDOW_MINUTES, years,
            )
            for ticker, estimate in fallback_estimates.items():
                estimates[ticker] = (estimate, fallback_times.get(ticker))

        rows = dict(cached_rows)
        for candidate in pending:
            fitted = estimates.get(candidate["ticker"])
            if fitted:
                estimate, quote_time = fitted
                annualized = estimate["annualized_implied_vol_pct"]
                rows[candidate["ticker"]] = {"annualized_implied_vol_pct": annualized,
                    "sigma_1d_pct": annualized * math.sqrt(plan["year_fraction"]),
                    "iv_reliability": estimate["iv_reliability"],
                    "iv_quote_time": quote_time,
                    "iv_quote_age_minutes": (
                        close - datetime.fromisoformat(quote_time)
                    ).total_seconds() / 60 if quote_time else None}
        atomic_write_json(path, {"plan": plan, "rows": rows, "source": inputs.get("source")})
        self.publish(plan)
        self.error = None if all(t in rows for t in tickers) else f"Closing IV unavailable for {len(set(tickers) - set(rows))} symbols"

    def apply(self, bundle, now=None):
        """Overlay one denominator per ticker without altering either list."""
        plan = sigma_plan(now)
        saved = read_json(self.root / (plan["effective_session"] + ".json"))
        current_rows = validated_rows(saved, plan)
        result = copy.deepcopy(bundle)
        inputs_path = self.root / ("close-" + plan["iv_session"] + ".json")
        stamp = (str(inputs_path), inputs_path.stat().st_mtime_ns, inputs_path.stat().st_size) if inputs_path.exists() else None
        if self.serving_only and self._closing_inputs and self._closing_inputs[0] == stamp:
            closing_inputs = self._closing_inputs[1]
        else:
            closing_inputs = read_json(inputs_path, exclude_quotes=self.serving_only)
            if self.serving_only:
                self._closing_inputs = (stamp, closing_inputs)
        closing_spots = closing_inputs.get("spots", {})
        # A pending rollover keeps the last validated shared denominator. Old
        # list caches are a last-resort migration fallback, explicitly stale.
        fallback = {}
        for view in ("snapshot", "today"):
            payload = bundle[view]
            for row in payload["rows"]:
                fallback[row["ticker"]] = {k: row.get(k, payload.get(k)) for k in (
                    "sigma_1d_pct", "annualized_implied_vol_pct", "iv_snapshot_time", "option_expiry", "iv_reliability")}
        needed = set(fallback) - set(current_rows)
        restored = set()
        for path in sorted(self.root.glob("????-??-??.json"), reverse=True) if needed else []:
            if path.stem >= plan["effective_session"]:
                continue
            previous = read_json(path)
            old_plan = previous.get("plan", {})
            if old_plan.get("version") != VERSION:
                continue
            for ticker, row in validated_rows(previous, old_plan).items():
                if ticker in restored:
                    continue
                fallback[ticker] = {**row, "iv_snapshot_time": old_plan["iv_snapshot_time"],
                                    "option_expiry": old_plan["option_expiry"]}
                restored.add(ticker)
            if needed <= restored:
                break
        missing = set()
        for view in ("today", "snapshot"):
            payload = result[view]
            for row in payload["rows"]:
                sigma = current_rows.get(row["ticker"])
                if sigma:
                    row.update(sigma)
                    row.update(iv_snapshot_time=plan["iv_snapshot_time"], option_expiry=plan["option_expiry"],
                               expected_sigma_status="fresh", expected_sigma_session=plan["effective_session"])
                else:
                    missing.add(row["ticker"])
                    row.update(fallback[row["ticker"]])
                    row["expected_sigma_status"] = "awaiting_previous_close"
                if row.get("sigma_1d_pct"):
                    log_return = row.get("log_return_pct")
                    if log_return is None:
                        log_return = 100 * math.log1p(float(row.get("gain_pct", 0)) / 100)
                    row["z_score"] = float(log_return) / row["sigma_1d_pct"]
                    row["outside_one_sigma"] = abs(row["z_score"]) > 1
                    if row.get("prior_session_gain_pct") is not None:
                        row["prior_session_z_score"] = 100 * math.log1p(row["prior_session_gain_pct"] / 100) / row["sigma_1d_pct"]
                row["expected_sigma_target_iv_session"] = plan["iv_session"]
                close_price = closing_spots.get(row["ticker"])
                if isinstance(close_price, (int, float)) and math.isfinite(close_price) and close_price > 0:
                    row["return_anchor_price"] = close_price
                    row["return_anchor_date"] = plan["iv_session"]
                    row["return_anchor_timestamp"] = plan["iv_snapshot_time"]
                    row["live_anchor_price"] = close_price
            payload["expected_sigma"] = {**plan, "status": "awaiting_previous_close" if any(
                r["expected_sigma_status"] != "fresh" for r in payload["rows"]) else "fresh"}
            if payload["expected_sigma"]["status"] == "fresh":
                payload["iv_snapshot_time"] = plan["iv_snapshot_time"]
                payload["option_expiry"] = plan["option_expiry"]
        result["expected_sigma"] = {**plan, "status": "awaiting_previous_close" if missing else "fresh",
                                    "missing_symbols": sorted(missing), "last_error": self.error}
        result.setdefault("cache", {})["target_completed_session"] = plan["iv_session"]
        if result["today"].get("anchor_date") != plan["iv_session"]:
            result["cache"]["status"] = "stale"
        return result
