"""Live IV expansion tracking with moneyness-matched option strikes."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .iv_bucket_score import BucketScores
except ImportError:
    from iv_bucket_score import BucketScores

try:
    from .nasdaq_universe_movers import estimate_option_iv, normalize_symbol, parse_occ_symbol
except ImportError:  # Support direct script execution.
    from nasdaq_universe_movers import estimate_option_iv, normalize_symbol, parse_occ_symbol


EASTERN = ZoneInfo("America/New_York")
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
DEFAULT_QUOTE_MAX_AGE_SECONDS = 180
DEFAULT_QUOTE_PAIR_MAX_SKEW_SECONDS = 120
DEFAULT_FALLBACK_QUOTE_WINDOW_SECONDS = 15 * 60
DEFAULT_FALLBACK_SPOT_MAX_SKEW_SECONDS = 15 * 60
DEFAULT_SPOT_FUTURE_TOLERANCE_SECONDS = 5
MAX_SPOT_MINUTE_BUCKETS = 480
IV_EXPANSION_THRESHOLDS = (10.0, 20.0, 30.0)
IV_EXPANSION_METHOD = "moneyness-matched-black76-v1"
CACHE_SCHEMA_VERSION = "1.0"


def ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def option_market_is_open(value: datetime) -> bool:
    eastern = ensure_aware(value).astimezone(EASTERN)
    local_time = eastern.time().replace(tzinfo=None)
    return eastern.weekday() < 5 and time(9, 30) <= local_time < time(16)


def iv_expansion_level(value: float | None) -> int:
    if value is None or not math.isfinite(value):
        return 0
    return sum(value >= threshold - 1e-9 for threshold in IV_EXPANSION_THRESHOLDS)


def format_occ_symbol(root: str, expiry: date | str, side: str, strike: float) -> str:
    expiry_date = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    clean_root = str(root).strip()
    if not clean_root or len(clean_root) > 6:
        raise ValueError("OCC root must contain 1 through 6 characters")
    if side not in {"C", "P"}:
        raise ValueError("OCC option side must be C or P")
    strike_millis = round(float(strike) * 1000)
    if strike_millis < 0 or strike_millis > 99_999_999:
        raise ValueError("OCC strike is outside the supported range")
    return f"{clean_root:<6}{expiry_date:%y%m%d}{side}{strike_millis:08d}"


def option_contract_symbols(rows: list[dict[str, Any]]) -> list[str]:
    symbols = set()
    for row in rows:
        chain = row.get("iv_chain") or {}
        reference = row.get("iv_expansion_reference") or {}
        root = chain.get("occ_root")
        expiry = reference.get("option_expiry") or row.get("option_expiry")
        if not root or not expiry:
            continue
        for strike in chain.get("strikes") or []:
            try:
                symbols.add(format_occ_symbol(root, expiry, "C", float(strike)))
                symbols.add(format_occ_symbol(root, expiry, "P", float(strike)))
            except (TypeError, ValueError):
                continue
    return sorted(symbols)


class IvExpansionTracker:
    """Calculate and persist same-expiry IV expansion from live option quotes."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        option_expiry_close: str,
        anchor_date: str,
        cache_path: Path,
        *,
        cycle_id: str | None = None,
        quote_max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
        quote_pair_max_skew_seconds: int = DEFAULT_QUOTE_PAIR_MAX_SKEW_SECONDS,
        fallback_quote_window_seconds: int = DEFAULT_FALLBACK_QUOTE_WINDOW_SECONDS,
        fallback_spot_max_skew_seconds: int = DEFAULT_FALLBACK_SPOT_MAX_SKEW_SECONDS,
        spot_future_tolerance_seconds: int = DEFAULT_SPOT_FUTURE_TOLERANCE_SECONDS,
    ) -> None:
        self.option_expiry_close = ensure_aware(
            datetime.fromisoformat(option_expiry_close)
        )
        self.anchor_date = anchor_date
        self.cycle_id = cycle_id or anchor_date
        self.cache_path = cache_path
        self.quote_max_age_seconds = quote_max_age_seconds
        self.quote_pair_max_skew_seconds = quote_pair_max_skew_seconds
        self.fallback_quote_window_seconds = fallback_quote_window_seconds
        self.fallback_spot_max_skew_seconds = fallback_spot_max_skew_seconds
        self.spot_future_tolerance_seconds = spot_future_tolerance_seconds
        self.bucket_scores = BucketScores(cache_path.parent / "iv-expansion-statistics-session-batch.json")
        self.references: dict[str, dict[str, Any]] = {}
        self.ticker_by_root: dict[str, str] = {}
        self.spots: dict[str, dict[str, Any]] = {}
        self.spot_history: dict[
            str, dict[datetime, dict[str, dict[str, Any]]]
        ] = {}
        self.quotes: dict[str, dict[tuple[float, str], dict[str, Any]]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.cache_dirty = False

        for row in rows:
            ticker = str(row.get("ticker") or "")
            reference = row.get("iv_expansion_reference") or {}
            chain = row.get("iv_chain") or {}
            reference_iv = reference.get("annualized_implied_vol_pct")
            targets = reference.get("log_moneyness") or []
            root = chain.get("occ_root")
            try:
                reference_iv = float(reference_iv)
                targets = [float(value) for value in targets]
            except (TypeError, ValueError):
                continue
            if (
                not ticker
                or not math.isfinite(reference_iv)
                or reference_iv <= 0
                or not targets
                or reference.get("method") != IV_EXPANSION_METHOD
            ):
                continue
            self.references[ticker] = {
                "reference_iv_pct": reference_iv,
                "iv_reliability": reference.get("iv_reliability", row.get("iv_reliability")),
                "reference_forward": reference.get("forward_price"),
                "reference_strikes": [
                    float(value) for value in reference.get("strikes") or []
                ],
                "moneyness_targets": targets,
                "option_expiry": str(
                    reference.get("option_expiry") or row.get("option_expiry") or ""
                ),
                "occ_root": root,
                "requested_session": reference.get("requested_session"),
                "reference_session": reference.get("reference_session") or str(reference.get("observation_time") or "")[:10] or None,
                "reference_status": reference.get("reference_status", "yesterday"),
                "fallback_sessions": int(reference.get("fallback_sessions") or 0),
                "baseline_revision": int(reference.get("baseline_revision") or 1),
            }
            if root:
                self.ticker_by_root[normalize_symbol(root)] = ticker

    @property
    def state_key(self) -> str:
        return (
            f"{self.cycle_id}|{self.anchor_date}|"
            f"{self.option_expiry_close.isoformat()}|{IV_EXPANSION_METHOD}"
        )

    def update_spot(self, ticker: str, price: float, event_time: datetime) -> bool:
        if ticker not in self.references or not math.isfinite(price) or price <= 0:
            return False
        event_time = ensure_aware(event_time).astimezone(UTC)
        if option_market_is_open(event_time):
            self._remember_spot(ticker, float(price), event_time)
        previous = self.spots.get(ticker)
        if previous is None or event_time > previous["event_time"]:
            self.spots[ticker] = {"price": float(price), "event_time": event_time}
        if not option_market_is_open(event_time):
            return False
        return self._recalculate(ticker, event_time)

    def _remember_spot(
        self,
        ticker: str,
        price: float,
        event_time: datetime,
    ) -> None:
        minute = event_time.replace(second=0, microsecond=0)
        buckets = self.spot_history.setdefault(ticker, {})
        point = {"price": price, "event_time": event_time}
        bucket = buckets.setdefault(minute, {"first": point, "last": point})
        if event_time < bucket["first"]["event_time"]:
            bucket["first"] = point
        if event_time > bucket["last"]["event_time"]:
            bucket["last"] = point
        while len(buckets) > MAX_SPOT_MINUTE_BUCKETS:
            buckets.pop(min(buckets))

    def update_option_quote(
        self,
        raw_symbol: str,
        bid: float,
        ask: float,
        event_time: datetime,
    ) -> str | None:
        parsed = parse_occ_symbol(raw_symbol)
        if parsed is None:
            return None
        ticker = self.ticker_by_root.get(normalize_symbol(parsed["root"]))
        if ticker is None:
            return None
        reference = self.references[ticker]
        if str(parsed["expiry"]) != reference["option_expiry"]:
            return None
        if (
            not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid < 0
            or ask <= 0
            or ask < bid
        ):
            return None
        event_time = ensure_aware(event_time).astimezone(UTC)
        if not option_market_is_open(event_time):
            return None
        ticker_quotes = self.quotes.setdefault(ticker, {})
        key = (float(parsed["strike"]), parsed["side"])
        previous = ticker_quotes.get(key)
        if previous and event_time <= previous["event_time"]:
            return None
        ticker_quotes[key] = {
            "bid": float(bid),
            "ask": float(ask),
            "mid": 0.5 * (float(bid) + float(ask)),
            "spread": float(ask) - float(bid),
            "raw_symbol": raw_symbol,
            "event_time": event_time,
        }
        return ticker if self._recalculate(ticker, event_time) else None

    def update_mock(self, ticker: str, expansion_pct: float, event_time: datetime) -> bool:
        reference = self.references.get(ticker)
        if reference is None:
            return False
        event_time = ensure_aware(event_time).astimezone(UTC)
        spot = self.spots.get(ticker, {}).get("price")
        reference_forward = float(reference.get("reference_forward") or spot or 1)
        current_forward = float(spot or reference_forward)
        live_strikes = [
            round(current_forward * math.exp(target), 2)
            for target in reference["moneyness_targets"]
        ]
        result = {
            "ticker": ticker,
            "method": IV_EXPANSION_METHOD,
            "iv_expansion_pct": float(expansion_pct),
            "iv_expansion_level": iv_expansion_level(float(expansion_pct)),
            "reference_annualized_iv_pct": reference["reference_iv_pct"],
            "live_annualized_iv_pct": (
                reference["reference_iv_pct"] * (1 + float(expansion_pct) / 100)
            ),
            "reference_forward": reference.get("reference_forward"),
            "live_forward": current_forward,
            "reference_strikes": reference["reference_strikes"],
            "live_strikes": live_strikes,
            "target_log_moneyness": reference["moneyness_targets"],
            "live_log_moneyness": reference["moneyness_targets"],
            "moneyness_match_max_error": 0.0,
            "iv_input_strikes": len(live_strikes),
            "iv_reliability": "high",
            "last_valid_at": event_time.isoformat(),
            "source": "MOCK.OPTIONS",
            "requested_session": reference.get("requested_session"),
            "iv_observation_session": event_time.astimezone(EASTERN).date().isoformat(),
            "iv_reference_session": reference.get("reference_session"),
            "iv_reference_status": reference.get("reference_status", "yesterday"),
            "fallback_sessions": reference.get("fallback_sessions", 0),
            "baseline_revision": reference.get("baseline_revision", 1),
        }
        return self._store_result(ticker, result)

    def _quote_pairs(
        self,
        ticker: str,
        as_of: datetime,
        *,
        max_age_seconds: int | None,
    ) -> list[dict[str, Any]]:
        as_of = ensure_aware(as_of).astimezone(UTC)
        trading_day = as_of.astimezone(EASTERN).date()
        grouped: dict[float, dict[str, dict[str, Any]]] = {}
        for (strike, side), quote in self.quotes.get(ticker, {}).items():
            age = (as_of - quote["event_time"]).total_seconds()
            if age < 0 or (
                max_age_seconds is not None and age > max_age_seconds
            ):
                continue
            if quote["event_time"].astimezone(EASTERN).date() != trading_day:
                continue
            grouped.setdefault(strike, {})[side] = quote
        pairs = []
        for strike, sides in grouped.items():
            if "C" not in sides or "P" not in sides:
                continue
            skew = abs(
                (sides["C"]["event_time"] - sides["P"]["event_time"]).total_seconds()
            )
            if skew > self.quote_pair_max_skew_seconds:
                continue
            pair_time = max(
                sides["C"]["event_time"],
                sides["P"]["event_time"],
            )
            pairs.append({
                "strike": strike,
                "event_time": pair_time,
                "call": {
                    key: sides["C"][key]
                    for key in ("bid", "ask", "mid", "spread", "raw_symbol")
                },
                "put": {
                    key: sides["P"][key]
                    for key in ("bid", "ask", "mid", "spread", "raw_symbol")
                },
            })
        return pairs

    def _fresh_pairs(self, ticker: str, as_of: datetime) -> list[dict[str, Any]]:
        return self._quote_pairs(
            ticker,
            as_of,
            max_age_seconds=self.quote_max_age_seconds,
        )

    def _latest_same_day_pairs(
        self,
        ticker: str,
        as_of: datetime,
    ) -> tuple[list[dict[str, Any]], datetime | None]:
        pairs = self._quote_pairs(ticker, as_of, max_age_seconds=None)
        if not pairs:
            return [], None
        latest = max(pair["event_time"] for pair in pairs)
        coherent = [
            pair
            for pair in pairs
            if 0 <= (latest - pair["event_time"]).total_seconds()
            <= self.fallback_quote_window_seconds
        ]
        return coherent, latest

    def _nearest_spot(
        self,
        ticker: str,
        valuation_time: datetime,
        max_skew_seconds: int,
    ) -> dict[str, Any] | None:
        trading_day = valuation_time.astimezone(EASTERN).date()
        candidates = [
            point
            for bucket in self.spot_history.get(ticker, {}).values()
            for point in (bucket["first"], bucket["last"])
            if point["event_time"].astimezone(EASTERN).date() == trading_day
        ]
        if not candidates:
            return None
        prior = [
            point for point in candidates
            if point["event_time"] <= valuation_time
        ]
        if prior:
            nearest = max(prior, key=lambda point: point["event_time"])
            age = (valuation_time - nearest["event_time"]).total_seconds()
            return nearest if age <= max_skew_seconds else None

        # Permit only a few seconds of feed-ordering skew. A later close must
        # never be paired with an older option quote merely because it is the
        # nearest cached equity point.
        future = [
            point for point in candidates
            if 0 < (point["event_time"] - valuation_time).total_seconds()
            <= self.spot_future_tolerance_seconds
        ]
        return min(future, key=lambda point: point["event_time"], default=None)

    def _estimate_result(
        self,
        ticker: str,
        pairs: list[dict[str, Any]],
        valuation_time: datetime,
        *,
        calculation_source: str,
        spot_max_skew_seconds: int,
    ) -> dict[str, Any] | None:
        reference = self.references.get(ticker)
        spot = self._nearest_spot(
            ticker,
            valuation_time,
            spot_max_skew_seconds,
        )
        if spot is None or reference is None:
            return None
        year_fraction = (
            self.option_expiry_close - valuation_time
        ).total_seconds() / SECONDS_PER_YEAR
        if year_fraction <= 0:
            return None
        estimate = estimate_option_iv(
            {"ticker": ticker, "anchor_price": spot["price"]},
            pairs,
            year_fraction,
            moneyness_targets=reference["moneyness_targets"],
        )
        if estimate is None:
            return None
        live_iv = estimate["annualized_implied_vol_pct"]
        expansion_pct = 100 * (live_iv / reference["reference_iv_pct"] - 1)
        if not math.isfinite(expansion_pct):
            return None
        return {
            "ticker": ticker,
            "method": IV_EXPANSION_METHOD,
            "iv_expansion_pct": expansion_pct,
            "iv_expansion_level": iv_expansion_level(expansion_pct),
            "reference_annualized_iv_pct": reference["reference_iv_pct"],
            "live_annualized_iv_pct": live_iv,
            "reference_forward": reference.get("reference_forward"),
            "live_forward": estimate["forward_price"],
            "reference_strikes": reference["reference_strikes"],
            "live_strikes": estimate["iv_input_strike_values"],
            "target_log_moneyness": reference["moneyness_targets"],
            "live_log_moneyness": estimate["iv_input_log_moneyness"],
            "moneyness_match_max_error": estimate["iv_moneyness_match_max_error"],
            "iv_input_strikes": estimate["iv_input_strikes"],
            "iv_reliability": estimate["iv_reliability"],
            "last_valid_at": valuation_time.isoformat(),
            "spot_timestamp": spot["event_time"].isoformat(),
            "spot_quote_skew_seconds": abs(
                (spot["event_time"] - valuation_time).total_seconds()
            ),
            "spot_age_seconds": (
                valuation_time - spot["event_time"]
            ).total_seconds(),
            "calculation_source": calculation_source,
            "source": "OPRA.PILLAR",
            "requested_session": reference.get("requested_session"),
            "iv_observation_session": valuation_time.astimezone(EASTERN).date().isoformat(),
            "iv_reference_session": reference.get("reference_session"),
            "iv_reference_status": reference.get("reference_status", "yesterday"),
            "fallback_sessions": reference.get("fallback_sessions", 0),
            "baseline_revision": reference.get("baseline_revision", 1),
        }

    def _recalculate(self, ticker: str, event_time: datetime) -> bool:
        pairs = self._fresh_pairs(ticker, event_time)
        if pairs:
            valuation_time = max(pair["event_time"] for pair in pairs)
            result = self._estimate_result(
                ticker,
                pairs,
                valuation_time,
                calculation_source="live_quotes",
                spot_max_skew_seconds=self.quote_max_age_seconds,
            )
            if result is not None:
                return self._store_result(ticker, result)

        fallback_pairs, valuation_time = self._latest_same_day_pairs(
            ticker,
            event_time,
        )
        if not fallback_pairs or valuation_time is None:
            return False
        result = self._estimate_result(
            ticker,
            fallback_pairs,
            valuation_time,
            calculation_source="latest_same_day_quotes",
            spot_max_skew_seconds=self.fallback_spot_max_skew_seconds,
        )
        return result is not None and self._store_result(ticker, result)

    def _store_result(self, ticker: str, result: dict[str, Any]) -> bool:
        previous = self.results.get(ticker)
        if previous is not None and datetime.fromisoformat(
            result["last_valid_at"]
        ) < datetime.fromisoformat(previous["last_valid_at"]):
            return False
        changed = (
            previous is None
            or abs(
                float(previous["iv_expansion_pct"]) - float(result["iv_expansion_pct"])
            ) >= 0.005
            or previous.get("live_strikes") != result.get("live_strikes")
            or previous.get("iv_reliability") != result.get("iv_reliability")
        )
        self.results[ticker] = result
        self.cache_dirty = True
        return changed

    def public_results(
        self,
        tickers: list[str] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        selected = set(tickers) if tickers is not None else set(self.results)
        current = ensure_aware(now or datetime.now(UTC)).astimezone(UTC)
        output = []
        for ticker in sorted(selected):
            result = self.results.get(ticker)
            if result is None:
                continue
            result_time = datetime.fromisoformat(result["last_valid_at"])
            age = max(
                0.0,
                (current - result_time).total_seconds(),
            )
            market_open = option_market_is_open(current)
            same_trading_day = (
                result_time.astimezone(EASTERN).date()
                == current.astimezone(EASTERN).date()
            )
            live = (
                market_open
                and same_trading_day
                and age <= self.quote_max_age_seconds
            )
            status = (
                "live"
                if live
                else "last_quote"
                if market_open and same_trading_day
                else "close"
            )
            output.append({
                **result,
                "status": status,
                "iv_expansion_bucket_score": self.bucket_scores.score(result, self.references.get(ticker, {})),
                "is_persisted": not live,
                "age_seconds": round(age, 1),
            })
        return output

    def load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text())
            if payload.get("schema_version") == CACHE_SCHEMA_VERSION and payload.get("state_key") == self.state_key:
                restored = {}
                for result in payload.get("results", []):
                    ticker = result.get("ticker")
                    if ticker not in self.references:
                        continue
                    datetime.fromisoformat(result["last_valid_at"])
                    restored[ticker] = result
                self.results = restored
        except (OSError, ValueError, TypeError, KeyError):
            self.results = {}
        self.restore_completed_bucket_marks()
        self.save_cache()

    def restore_completed_bucket_marks(self) -> None:
        """Prefer newer, same-session/expiry completed research marks over old quotes."""
        database = self.cache_path.parent / "iv-expansion-history-session-batch.sqlite3"
        if not database.exists():
            return
        try:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                for ticker, reference in self.references.items():
                    previous = self.results.get(ticker, {})
                    if previous and previous.get("source") != "OPRA.PILLAR":
                        continue
                    prior_time = datetime.fromisoformat(previous["last_valid_at"]) if previous else None
                    session = prior_time.astimezone(EASTERN).date().isoformat() if prior_time else self.anchor_date
                    mark = connection.execute(
                        "SELECT * FROM observations WHERE ticker=? AND session_date=? "
                        "AND bucket='15:30' AND expiry=? AND reference_session=? "
                        "AND reference_status='yesterday'",
                        (ticker, session, reference["option_expiry"],
                         reference.get("reference_session") or self.anchor_date),
                    ).fetchone()
                    if mark is None or (prior_time and datetime.fromisoformat(mark["observation_ts"]) <= prior_time):
                        continue
                    self.results[ticker] = {
                        "ticker": ticker, "method": IV_EXPANSION_METHOD,
                        "iv_expansion_pct": mark["iv_expansion_pct"],
                        "iv_expansion_level": iv_expansion_level(mark["iv_expansion_pct"]),
                        "reference_annualized_iv_pct": mark["reference_iv_pct"],
                        "live_annualized_iv_pct": mark["observed_iv_pct"],
                        "last_valid_at": mark["observation_ts"],
                        "iv_observation_session": session,
                        "iv_reference_session": mark["reference_session"],
                        "iv_reference_status": mark["reference_status"],
                        "iv_reliability": mark["observed_reliability"],
                        "reference_reliability": mark["reference_reliability"],
                        "moneyness_match_max_error": mark["moneyness_match_max_error"],
                        "source": "OPRA.PILLAR", "calculation_source": "historical_bucket",
                        "baseline_revision": previous.get("baseline_revision", 1),
                    }
                    self.cache_dirty = True
        except (sqlite3.Error, ValueError, KeyError):
            return

    def save_cache(self) -> None:
        if not self.cache_dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "schema_version": CACHE_SCHEMA_VERSION,
            "state_key": self.state_key,
            "saved_at": datetime.now(UTC).isoformat(),
            "results": list(self.results.values()),
        }, indent=2) + "\n")
        os.replace(temporary, self.cache_path)
        self.cache_dirty = False
