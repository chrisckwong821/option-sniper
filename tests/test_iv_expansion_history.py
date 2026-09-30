import argparse
import math
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.iv_expansion_history import (
    DownloadLedger,
    _contracts_for_session,
    _option_frames,
    _option_request,
    _spot_request,
    bucket_marks,
    build_statistics,
    contracts_by_expiry,
    definition_catalog_session_for_observation,
    definition_catalog_sessions,
    evenly_sampled_sessions,
    history_window,
    initialize_database,
    limit_history_window,
    option_expiry_for_observation,
    prepare_snapshot_pairs,
    rarity_level,
    rarity_score,
    resolve_rows,
    robust_distribution,
    select_baseline_symbols,
    select_listed_expiry,
    tenor_class_for_observation,
    upsert_observation,
)


class IvExpansionHistoryTests(unittest.TestCase):
    def test_bucket_batches_match_full_day_iv_calculation(self):
        from scripts.iv_expansion_history import Baseline, process_observation_session, frame_window
        session = date(2026, 9, 10)
        marks = bucket_marks(session)
        call, put = 'AAA   260918C00100000', 'AAA   260918P00100000'
        baseline = Baseline('AAA', date(2026,9,9), date(2026,9,9), 0,
            date(2026,9,18), 'target_weekly', 100,
            {'annualized_implied_vol_pct':30, 'iv_input_log_moneyness':[0.0],
             'forward_price':100, 'iv_reliability':'high'},
            [{'strike':100,'call':call,'put':put}])
        rows = [{'ticker':'AAA', 'iv_chain':{'occ_root':'AAA','strikes':[100]}}]
        stocks = pd.DataFrame([{'ts_event':m.mark,'symbol':'AAA','close':100} for m in marks])
        quotes = pd.DataFrame([
            {'ts_recv':m.mark, 'symbol':symbol, 'bid_px_00':2+i/10, 'ask_px_00':2.2+i/10}
            for i,m in enumerate(marks) for symbol in (call,put)])
        # The old full-day request excluded 16:00; the last bucket must still do so.
        close = marks[-1].mark + pd.Timedelta(minutes=1)
        poison = pd.DataFrame([{'ts_recv':close,'symbol':s,'bid_px_00':20,'ask_px_00':21}
                               for s in (call,put)])
        source = pd.concat([quotes,poison],ignore_index=True)
        results = []
        with tempfile.TemporaryDirectory() as directory:
            for batched in (False,True):
                def frames(_client,_ledger,_symbols,start,end,*args):
                    return frame_window(source,start,end) if batched else quotes.copy()
                connection = initialize_database(Path(directory)/f'{batched}.sqlite3')
                with patch('scripts.iv_expansion_history.build_baselines',return_value={'AAA':baseline}), \
                     patch('scripts.iv_expansion_history._spot_frame',return_value=stocks), \
                     patch('scripts.iv_expansion_history._option_frames',side_effect=frames) as fetch:
                    result = process_observation_session(connection,None,None,None,rows,session,[date(2026,9,9)],
                        max_fallback_sessions=0,option_symbol_chunk_size=100,quote_lookback_minutes=15)
                self.assertEqual(result['observations'],13)
                self.assertEqual(fetch.call_count,13)
                for request in fetch.call_args_list:
                    start,end = request.args[3:5]
                    self.assertLessEqual((end-start).total_seconds(),961)
                    self.assertLessEqual(end,close)
                results.append(connection.execute('SELECT * FROM observations ORDER BY bucket').fetchall())
                connection.close()
        self.assertEqual(results[0],results[1])

    def test_missing_full_catalogue_is_restored_before_request_partitioning(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            rows = [{"ticker": f"T{i}"} for i in range(101)]
            ledger = Mock()
            path = Path(directory) / "full.dbn.zst"
            ledger._path.return_value = path
            ledger.shared.enabled = True
            ledger.shared.restore_file.side_effect = lambda p: p.touch()
            ledger.fetch_frame.return_value = pd.DataFrame()
            result = _contracts_for_session(Mock(), ledger, rows, date(2026, 8, 28), observation_session=date(2026, 8, 31))
            self.assertEqual(result, {})
            ledger.shared.restore_file.assert_called_once_with(path)
            ledger.fetch_frame.assert_called_once()
            self.assertEqual(len(ledger.fetch_frame.call_args.args[2]["symbols"]), 101)

    def test_definition_gateway_timeout_splits_without_losing_required_parents(self):
        from databento.common.error import BentoServerError
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            rows = [{"ticker": f"T{i}"} for i in range(5)]
            ledger = Mock()
            ledger._path.return_value = Path(directory) / "missing.dbn.zst"
            ledger.shared.enabled = False
            sizes = []

            def fetch(_client, _category, request):
                sizes.append(len(request["symbols"]))
                if len(request["symbols"]) > 2:
                    raise BentoServerError(504)
                return pd.DataFrame()

            ledger.fetch_frame.side_effect = fetch
            with patch('scripts.iv_expansion_history.contracts_by_expiry',
                       side_effect=lambda _frame, group, **_kwargs: {
                           row["ticker"]: {} for row in group
                       }):
                result = _contracts_for_session(
                    Mock(), ledger, rows, date(2026, 9, 23),
                    observation_session=date(2026, 9, 29),
                )
            self.assertEqual(set(result), {row["ticker"] for row in rows})
            self.assertEqual(sizes, [5, 3, 2, 1, 2])

    def test_zero_budget_blocks_a_paid_request(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            ledger = DownloadLedger(Path(directory), 0)
            client = Mock()
            client.metadata.get_cost.return_value = 0.01
            with self.assertRaises(RuntimeError):
                ledger.fetch_frame(client, "spot", _spot_request([{"ticker": "A"}], date(2026, 9, 4)))
            client.timeseries.get_range.assert_not_called()
            self.assertEqual(ledger.downloaded_requests, 0)

    def test_baseline_fallback_fetches_only_unresolved_eligible_stocks(self):
        from scripts.iv_expansion_history import build_baselines
        rows = [{'ticker': t} for t in ('AAA', 'BBB')]
        expiry = date(2026, 9, 18)
        catalog = {t: {expiry: [{'strike': 100, 'call': t+'C', 'put': t+'P'}]} for t in ('AAA','BBB')}
        attempts = {'AAA': 0, 'BBB': 0}
        def estimate(ticker, *args):
            attempts[ticker] += 1
            return None if ticker == 'BBB' and attempts[ticker] == 1 else {'annualized_implied_vol_pct': 30}
        with patch('scripts.iv_expansion_history._contracts_for_session', return_value=catalog), \
             patch('scripts.iv_expansion_history._spot_frame', return_value=pd.DataFrame()) as spots, \
             patch('scripts.iv_expansion_history.latest_spots', return_value={'AAA':100,'BBB':100}), \
             patch('scripts.iv_expansion_history._option_frames', return_value=pd.DataFrame()), \
             patch('scripts.iv_expansion_history.prepare_snapshot_pairs', return_value={}), \
             patch('scripts.iv_expansion_history.estimate_snapshot_from_pairs', side_effect=estimate):
            result = build_baselines(None, None, None, rows, date(2026,9,10),
                [date(2026,9,8),date(2026,9,9)], spot_rows=rows+[{'ticker':'EXTRA'}],
                max_fallback_sessions=1, option_symbol_chunk_size=100)
        self.assertEqual([[r['ticker'] for r in c.args[2]] for c in spots.call_args_list], [['AAA','BBB'],['BBB']])
        self.assertEqual(result['BBB'].reference_session, date(2026,9,8))
        self.assertEqual(result['BBB'].fallback_sessions, 1)

    def test_ineligible_catalog_does_not_fetch_prices_or_publish_empty_observations(self):
        from scripts.iv_expansion_history import build_baselines, process_observation_session
        rows = [{'ticker':'AAA'}]
        catalog = {'AAA':{date(2026,9,11):[{'strike':100,'call':'C','put':'P'}]}}
        with patch('scripts.iv_expansion_history._contracts_for_session', return_value=catalog), \
             patch('scripts.iv_expansion_history._spot_frame') as spots:
            result = build_baselines(None,None,None,rows,date(2026,9,10),[date(2026,9,9)],
                max_fallback_sessions=0,option_symbol_chunk_size=100)
            self.assertEqual(result,{})
            spots.assert_not_called()
            with self.assertRaisesRegex(RuntimeError,'No valid IV baselines'):
                process_observation_session(None,None,None,None,rows,date(2026,9,10),[date(2026,9,9)],
                    max_fallback_sessions=0,option_symbol_chunk_size=100,quote_lookback_minutes=15)
            spots.assert_not_called()

    def test_unresolved_historical_symbols_are_empty_but_auth_errors_propagate(self):
        from databento.common.error import BentoClientError
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            ledger = DownloadLedger(Path(directory), 1)
            client = Mock()
            request = _spot_request([{"ticker": "MISSING"}], date(2026, 4, 13))
            client.metadata.get_cost.side_effect = BentoClientError(
                422, message="symbology_invalid_request\nNone of the symbols could be resolved"
            )
            self.assertTrue(ledger.fetch_frame(client, "spot", request).empty)
            self.assertEqual(client.metadata.get_cost.call_count, 1)
            client.timeseries.get_range.assert_not_called()
            self.assertEqual(ledger.estimated_new_cost, 0)
            client.metadata.get_cost.side_effect = BentoClientError(403, message="Forbidden")
            with self.assertRaises(BentoClientError):
                ledger.fetch_frame(client, "spot", request)

    def test_new_catalogs_split_parents_and_reuse_cycle_memory(self):
        rows = [{"ticker": f"S{i}"} for i in range(416)]
        with tempfile.TemporaryDirectory() as directory:
            ledger = DownloadLedger(Path(directory), 1)
            cache = {}
            with patch.object(ledger, "fetch_frame", return_value=pd.DataFrame()) as fetch:
                for _ in range(2):
                    _contracts_for_session(
                        object(), ledger, rows, date(2026, 3, 31),
                        observation_session=date(2026, 4, 1),
                        contract_cache=cache,
                    )
                self.assertEqual(
                    [len(call.args[2]["symbols"]) for call in fetch.call_args_list],
                    [100, 100, 100, 100, 16],
                )

    def test_regular_session_has_thirteen_dst_aware_buckets(self):
        marks = bucket_marks(date(2026, 9, 4))

        self.assertEqual(len(marks), 13)
        self.assertEqual(marks[0].label, "09:30")
        self.assertEqual(marks[0].mark.isoformat(), "2026-09-04T09:59:00-04:00")
        self.assertEqual(marks[-1].label, "15:30")
        self.assertEqual(marks[-1].mark.isoformat(), "2026-09-04T15:59:00-04:00")

    def test_six_month_window_has_baseline_padding_and_holdout(self):
        window = history_window(date(2026, 9, 4), 6, max_fallback_sessions=5)

        self.assertEqual(window.holdout_session, date(2026, 9, 4))
        self.assertGreaterEqual(len(window.calibration_sessions), 120)
        self.assertLess(window.required_sessions[0], window.calibration_sessions[0])

    def test_cost_sampling_includes_endpoints(self):
        sessions = [date(2026, 9, day) for day in range(1, 10)]

        sampled = evenly_sampled_sessions(sessions, 3)

        self.assertEqual(sampled, [sessions[0], sessions[4], sessions[-1]])

    def test_session_limit_keeps_reference_padding(self):
        full = history_window(date(2026, 9, 4), 6, max_fallback_sessions=5)

        limited = limit_history_window(full, 1, max_fallback_sessions=5)

        self.assertEqual(limited.calibration_sessions, (date(2026, 9, 4),))
        self.assertEqual(limited.holdout_session, date(2026, 9, 4))
        self.assertLess(limited.required_sessions[0], date(2026, 9, 4))

    def test_expiry_is_friday_or_last_session_before_holiday(self):
        self.assertEqual(option_expiry_for_observation(date(2026, 9, 1)), date(2026, 9, 4))
        self.assertEqual(option_expiry_for_observation(date(2026, 9, 4)), date(2026, 9, 11))

    def test_expiry_rolls_after_friday(self):
        expected = {
            date(2026, 8, 17): date(2026, 8, 21),
            date(2026, 8, 18): date(2026, 8, 21),
            date(2026, 8, 19): date(2026, 8, 21),
            date(2026, 8, 20): date(2026, 8, 28),
            date(2026, 8, 21): date(2026, 8, 28),
            date(2026, 8, 24): date(2026, 8, 28),
        }

        self.assertEqual(
            {session: option_expiry_for_observation(session) for session in expected},
            expected,
        )

    def test_definition_catalog_is_reused_for_the_whole_expiry_cycle(self):
        expected = {
            date(2026, 8, 20): date(2026, 8, 19),
            date(2026, 8, 21): date(2026, 8, 19),
            date(2026, 8, 24): date(2026, 8, 19),
            date(2026, 8, 25): date(2026, 8, 19),
            date(2026, 8, 26): date(2026, 8, 19),
        }

        self.assertEqual(
            {
                session: definition_catalog_session_for_observation(session)
                for session in expected
            },
            expected,
        )
        self.assertEqual(
            definition_catalog_sessions(tuple(expected)),
            (date(2026, 8, 19),),
        )

    def test_holiday_shortened_cycle_uses_the_prior_session_catalog(self):
        # July 3, 2026 is a market holiday, so the listed weekly expiry is
        # Thursday July 2.  The two-day rule first targets the following cycle
        # on Wednesday July 1 and uses Tuesday June 30 definitions.
        self.assertEqual(
            option_expiry_for_observation(date(2026, 7, 1)),
            date(2026, 7, 10),
        )
        self.assertEqual(
            definition_catalog_session_for_observation(date(2026, 7, 1)),
            date(2026, 6, 30),
        )

    def test_tenor_classes_separate_front_roll_and_fallback(self):
        session = date(2026, 8, 20)

        self.assertEqual(
            tenor_class_for_observation(
                session,
                date(2026, 8, 28),
                expiry_status="target_weekly",
            ),
            "rolled_next_weekly",
        )
        self.assertEqual(
            tenor_class_for_observation(
                date(2026, 8, 24),
                date(2026, 8, 28),
                expiry_status="target_weekly",
            ),
            "front_weekly",
        )
        self.assertEqual(
            tenor_class_for_observation(
                session,
                date(2026, 9, 18),
                expiry_status="nearest_listed_fallback",
            ),
            "longer_dated_fallback",
        )

    def test_listed_expiry_prefers_target_then_nearest_fallback(self):
        target = select_listed_expiry(
            [date(2026, 9, 4), date(2026, 9, 11), date(2026, 9, 18)],
            reference_session=date(2026, 9, 3),
            observation_session=date(2026, 9, 4),
        )
        fallback = select_listed_expiry(
            [date(2026, 3, 20), date(2026, 4, 17)],
            reference_session=date(2026, 3, 3),
            observation_session=date(2026, 3, 4),
        )

        self.assertEqual(target, (date(2026, 9, 11), "target_weekly"))
        self.assertEqual(fallback, (date(2026, 3, 20), "nearest_listed_fallback"))

    def test_robust_sigma_uses_scaled_mad(self):
        distribution = robust_distribution([1, 2, 3, 4, 100], min_observations=5)

        self.assertEqual(distribution["status"], "valid")
        self.assertEqual(distribution["median"], 3)
        self.assertEqual(distribution["mad"], 1)
        self.assertAlmostEqual(distribution["robust_sigma"], 1.4826)
        self.assertAlmostEqual(rarity_score(6, distribution), 3 / 1.4826)

    def test_insufficient_and_degenerate_history_do_not_score(self):
        insufficient = robust_distribution([1, 2], min_observations=3)
        degenerate = robust_distribution([2, 2, 2], min_observations=3)

        self.assertEqual(insufficient["status"], "insufficient_history")
        self.assertEqual(degenerate["status"], "degenerate_history")
        self.assertIsNone(rarity_score(3, insufficient))
        self.assertIsNone(rarity_score(3, degenerate))

    def test_rarity_stars_are_z_score_not_percent_thresholds(self):
        self.assertEqual([rarity_level(value) for value in (0.99, 1, 1.99, 2, 3, 8)], [0, 1, 1, 2, 3, 3])
        self.assertEqual(rarity_level(-3), 0)
        self.assertEqual(rarity_level(math.nan), 0)

    def test_individual_and_batch_row_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "state.json"
            source.write_text('{"today":{"rows":[{"ticker":"TSEM"},{"ticker":"BE"}]}}')
            individual = argparse.Namespace(
                mode="individual", tickers=["tsem"], ticker_source=source,
            )
            batch = argparse.Namespace(mode="batch", tickers=None, ticker_source=source)

            self.assertEqual(resolve_rows(individual)[0]["ticker"], "TSEM")
            self.assertEqual([row["ticker"] for row in resolve_rows(batch)], ["TSEM", "BE"])

    def test_baseline_contract_selection_is_driven_by_stock_price(self):
        contracts = [
            {"strike": strike, "call": f"C{strike}", "put": f"P{strike}"}
            for strike in (40, 45, 50, 55, 60)
        ]
        symbols, selected = select_baseline_symbols(
            {"contracts": {"TSEM": contracts}},
            {"TSEM": 54},
            neighbor_count=1,
        )

        self.assertEqual([row["strike"] for row in selected["TSEM"]], [50, 55, 60])
        self.assertEqual(symbols, ["C50", "C55", "C60", "P50", "P55", "P60"])

    def test_contract_catalog_vector_parse_filters_expiry_horizon(self):
        definitions = pd.DataFrame({
            "raw_symbol": [
                "TSEM  260904C00050000",
                "TSEM  260904P00050000",
                "TSEM  270101C00050000",
                "TSEM  270101P00050000",
                "OTHER 260904C00050000",
            ],
        })

        catalog = contracts_by_expiry(
            definitions,
            [{"ticker": "TSEM", "iv_chain": {"occ_root": "TSEM"}}],
            min_expiry=date(2026, 9, 1),
            max_expiry=date(2026, 11, 1),
        )

        self.assertEqual(list(catalog["TSEM"]), [date(2026, 9, 4)])
        self.assertEqual(catalog["TSEM"][date(2026, 9, 4)][0]["strike"], 50.0)

    def test_raw_symbol_requests_use_default_mapped_output(self):
        row = {"ticker": "TSEM"}
        mark = bucket_marks(date(2026, 9, 4))[0]

        spot = _spot_request([row], date(2026, 9, 4))
        option = _option_request(["TSEM  260904C00050000"], mark.start, mark.end)

        self.assertNotIn("stype_out", spot)
        self.assertNotIn("stype_out", option)

    def test_snapshot_pairs_are_indexed_once_and_keep_latest_quote(self):
        quotes = pd.DataFrame({
            "ts_recv": pd.to_datetime([
                "2026-09-04T13:58:00Z",
                "2026-09-04T13:59:00Z",
                "2026-09-04T13:59:00Z",
            ]),
            "symbol": [
                "TSEM  260904C00050000",
                "TSEM  260904C00050000",
                "TSEM  260904P00050000",
            ],
            "bid_px_00": [1.0, 1.2, 0.8],
            "ask_px_00": [1.2, 1.4, 1.0],
        })

        pairs = prepare_snapshot_pairs(
            quotes,
            [{"ticker": "TSEM", "iv_chain": {"occ_root": "TSEM"}}],
            {"TSEM": date(2026, 9, 4)},
        )

        self.assertEqual(len(pairs["TSEM"]), 1)
        self.assertEqual(pairs["TSEM"][0]["strike"], 50.0)
        self.assertAlmostEqual(pairs["TSEM"][0]["call"]["mid"], 1.3)
        self.assertAlmostEqual(pairs["TSEM"][0]["put"]["mid"], 0.9)

    def test_observation_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "history.sqlite3")
            row = {
                "ticker": "TSEM",
                "session_date": "2026-09-04",
                "bucket": "09:30",
                "observation_ts": "2026-09-04T09:59:00-04:00",
                "requested_reference_session": "2026-09-03",
                "reference_session": "2026-09-03",
                "reference_status": "yesterday",
                "fallback_sessions": 0,
                "expiry": "2026-09-04",
                "expiry_status": "target_weekly",
                "observation_dte_days": 0,
                "observation_tenor_class": "expiry_day",
                "reference_iv_pct": 50.0,
                "observed_iv_pct": 55.0,
                "iv_expansion_pct": 10.0,
                "iv_log_change_pct": 100 * math.log(1.1),
                "reference_reliability": "high",
                "observed_reliability": "high",
                "moneyness_match_max_error": 0.01,
            }
            upsert_observation(connection, row)
            upsert_observation(connection, {**row, "observed_iv_pct": 56.0})
            count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            observed = connection.execute("SELECT observed_iv_pct FROM observations").fetchone()[0]
            connection.close()

            self.assertEqual(count, 1)
            self.assertEqual(observed, 56.0)

    def test_statistics_emit_all_tenor_regimes_not_only_holdout_regime(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "history.sqlite3")
            common = {
                "ticker": "TSEM",
                "bucket": "09:30",
                "observation_ts": "2026-09-03T09:59:00-04:00",
                "requested_reference_session": "2026-09-02",
                "reference_session": "2026-09-02",
                "reference_status": "yesterday",
                "fallback_sessions": 0,
                "expiry_status": "target_weekly",
                "reference_iv_pct": 50.0,
                "observed_iv_pct": 55.0,
                "iv_expansion_pct": 10.0,
                "iv_log_change_pct": 100 * math.log(1.1),
                "reference_reliability": "high",
                "observed_reliability": "high",
                "moneyness_match_max_error": 0.01,
            }
            upsert_observation(connection, {
                **common,
                "session_date": "2026-09-03",
                "expiry": "2026-09-04",
                "observation_dte_days": 1,
                "observation_tenor_class": "non_expiry",
            })
            upsert_observation(connection, {
                **common,
                "session_date": "2026-09-04",
                "observation_ts": "2026-09-04T09:59:00-04:00",
                "requested_reference_session": "2026-09-03",
                "reference_session": "2026-09-03",
                "expiry": "2026-09-04",
                "observation_dte_days": 0,
                "observation_tenor_class": "expiry_day",
            })

            statistics = build_statistics(
                connection,
                ["TSEM"],
                holdout_session=date(2026, 9, 4),
                min_observations=1,
                include_low_reliability=False,
            )
            connection.close()

            bucket = [row for row in statistics if row["bucket"] == "09:30"]
            self.assertEqual(
                [row["tenor_class"] for row in bucket],
                ["expiry_day", "non_expiry"],
            )
            self.assertIsNotNone(bucket[0]["holdout"])
            self.assertIsNone(bucket[1]["holdout"])

    def test_low_reliability_holdout_gets_provisional_score_from_clean_history(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = initialize_database(Path(directory) / "history.sqlite3")

            def observation(session, log_change, observed_reliability="high"):
                return {
                    "ticker": "TSEM",
                    "session_date": session,
                    "bucket": "15:30",
                    "observation_ts": f"{session}T15:59:00-04:00",
                    "requested_reference_session": "2026-08-25",
                    "reference_session": "2026-08-25",
                    "reference_status": "yesterday",
                    "fallback_sessions": 0,
                    "expiry": "2026-09-04",
                    "expiry_status": "target_weekly",
                    "observation_dte_days": 4,
                    "observation_tenor_class": "non_expiry",
                    "reference_iv_pct": 100.0,
                    "observed_iv_pct": 100 * math.exp(log_change / 100),
                    "iv_expansion_pct": 100 * (math.exp(log_change / 100) - 1),
                    "iv_log_change_pct": log_change,
                    "reference_reliability": "high",
                    "observed_reliability": observed_reliability,
                    "moneyness_match_max_error": 0.01,
                }

            for session, log_change in (
                ("2026-08-26", 0.0),
                ("2026-08-27", 10.0),
                ("2026-08-28", 20.0),
            ):
                upsert_observation(connection, observation(session, log_change))
            upsert_observation(
                connection,
                observation("2026-09-01", 15.0, observed_reliability="low"),
            )

            statistics = build_statistics(
                connection,
                ["TSEM"],
                holdout_session=date(2026, 9, 1),
                min_observations=3,
                include_low_reliability=False,
            )
            connection.close()

            row = next(item for item in statistics if item["bucket"] == "15:30")
            self.assertEqual(row["log_iv_change_distribution"]["n"], 3)
            self.assertEqual(
                row["holdout"]["score_status"],
                "provisional_low_reliability",
            )
            self.assertIsNotNone(row["holdout"]["iv_expansion_z"])

    def test_interrupted_download_does_not_leave_a_cache_hit(self):
        class Metadata:
            @staticmethod
            def get_cost(**_request):
                return 0.01

        class Timeseries:
            @staticmethod
            def get_range(**request):
                Path(request["path"]).write_bytes(b"incomplete")
                raise KeyboardInterrupt

        class Client:
            metadata = Metadata()
            timeseries = Timeseries()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = DownloadLedger(root, 1.0)
            request = {
                "dataset": "EQUS.MINI",
                "schema": "ohlcv-1m",
                "symbols": ["TSEM"],
                "start": "2026-09-04T09:30:00-04:00",
                "end": "2026-09-04T16:00:00-04:00",
            }

            with self.assertRaises(KeyboardInterrupt):
                ledger.fetch_frame(Client(), "spot", request)

            self.assertEqual(list(root.rglob("*.part")), [])
            self.assertEqual(list(root.rglob("*.dbn.zst")), [])

    def test_transient_download_is_retried_without_double_counting_cost(self):
        class Metadata:
            @staticmethod
            def get_cost(**_request):
                return 0.01

        class Store:
            @staticmethod
            def to_df():
                return pd.DataFrame({"symbol": ["TSEM"]})

        class Timeseries:
            calls = 0

            @classmethod
            def get_range(cls, **request):
                cls.calls += 1
                if cls.calls == 1:
                    Path(request["path"]).write_bytes(b"incomplete")
                    raise RuntimeError("transient")
                return Store()

        class Client:
            metadata = Metadata()
            timeseries = Timeseries()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = DownloadLedger(
                root,
                1.0,
                max_retries=1,
                retry_base_seconds=0,
            )
            frame = ledger.fetch_frame(
                Client(),
                "spot",
                _spot_request([{"ticker": "TSEM"}], date(2026, 9, 4)),
            )

            self.assertEqual(Timeseries.calls, 2)
            self.assertEqual(frame["symbol"].tolist(), ["TSEM"])
            self.assertEqual(list(root.rglob("*.part")), [])
            self.assertAlmostEqual(ledger.summary()["estimated_new_cost_usd"], 0.01)

    def test_option_chunks_use_bounded_concurrent_download_lanes(self):
        class Ledger:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.calls = []
                self.lock = threading.Lock()

            def fetch_frame(self, _client, category, request):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.calls.append((category, tuple(request["symbols"])))
                time.sleep(0.02)
                with self.lock:
                    self.active -= 1
                return pd.DataFrame({"symbol": request["symbols"]})

        ledger = Ledger()
        mark = bucket_marks(date(2026, 9, 4))[0]
        symbols = [f"S{index}" for index in range(7)]

        frame = _option_frames(
            object(),
            ledger,
            symbols,
            mark.start,
            mark.end,
            2,
            "observation_options",
            download_workers=3,
        )

        self.assertEqual(ledger.max_active, 3)
        self.assertEqual([len(symbols) for _, symbols in ledger.calls], [2, 2, 2, 1])
        self.assertEqual(frame["symbol"].tolist(), symbols)

    def test_concurrent_cost_reservations_enforce_the_hard_cap(self):
        entered = threading.Event()
        release = threading.Event()

        class Metadata:
            @staticmethod
            def get_cost(**_request):
                return 0.6

        class Store:
            @staticmethod
            def to_df():
                return pd.DataFrame({"symbol": ["TSEM"]})

        class Timeseries:
            @staticmethod
            def get_range(**_request):
                entered.set()
                release.wait(timeout=2)
                return Store()

        class Client:
            metadata = Metadata()
            timeseries = Timeseries()

        with tempfile.TemporaryDirectory() as directory:
            ledger = DownloadLedger(Path(directory), 1.0)
            first = _spot_request([{"ticker": "TSEM"}], date(2026, 9, 3))
            second = _spot_request([{"ticker": "BE"}], date(2026, 9, 4))
            with ThreadPoolExecutor(max_workers=2) as executor:
                pending = executor.submit(ledger.fetch_frame, Client(), "spot", first)
                self.assertTrue(entered.wait(timeout=1))
                with self.assertRaises(RuntimeError):
                    ledger.fetch_frame(Client(), "spot", second)
                release.set()
                pending.result(timeout=2)

            summary = ledger.summary()
            self.assertAlmostEqual(summary["estimated_new_cost_usd"], 0.6)
            self.assertEqual(summary["reserved_cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
