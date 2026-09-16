import json
import tempfile
import unittest
from pathlib import Path

from scripts.weekly_options import load_report, parse_report, weekly_universe


REPORT = """Active Weekly Options Summary as of 09/09/2026,,,,,,
,Weekly Options Summary for 10/30/2026 Expiration,,,,Weekly Options Summary for 10/23/2026 Expiration,
SYMBOL,OPTION NAME,EFFECTIVE DATE,,SYMBOL,OPTION NAME,EFFECTIVE DATE
AAPL  ,APPLE INC,06/25/2010,,OLD,PREVIOUS ONLY,01/01/2020
BRKB  ,BERKSHIRE,09/29/2014,,AAPL,APPLE INC,06/25/2010
,,,,OLD2,PREVIOUS ONLY,01/01/2020
"""


class WeeklyOptionsTests(unittest.TestCase):
    def test_universe_filters_both_views_and_recounts_without_mutating_source(self):
        rows = [
            {"ticker": "AAPL", "iv_reliability": "high"},
            {"ticker": "A", "iv_reliability": "low"},
            {"ticker": "BRK.B", "iv_reliability": "medium"},
            {"ticker": "ALIAS", "iv_chain": {"occ_root": "BRKB"}, "iv_reliability": "high"},
        ]
        bundle = {"today": {"rows": rows}, "snapshot": {"rows": rows[1:2]}}
        filtered = weekly_universe(bundle, parse_report(REPORT))
        self.assertEqual([r["ticker"] for r in filtered["today"]["rows"]], ["AAPL", "BRK.B", "ALIAS"])
        self.assertEqual(filtered["today"]["valid_iv_coverage"], 3)
        self.assertEqual(filtered["today"]["reliability_mix"], {"high": 2, "medium": 1})
        self.assertEqual(filtered["snapshot"]["rows"], [])
        self.assertEqual(len(bundle["today"]["rows"]), 4)

    def test_missing_or_nonmatching_list_cannot_widen_universe(self):
        bundle = {key: {"rows": [{"ticker": "A"}]} for key in ("today", "snapshot")}
        for report in (load_report("/does-not-exist"), parse_report(REPORT)):
            with self.assertRaises(ValueError):
                weekly_universe(bundle, report)

    def test_current_list_only_and_source_dates(self):
        data = parse_report(REPORT)
        self.assertEqual(data["symbols"], ["AAPL", "BRKB"])
        self.assertEqual(data["as_of"], "2026-09-09")
        self.assertEqual(data["expiration"], "2026-10-30")

    def test_malformed_source_is_rejected(self):
        for raw in ("", "<html>unavailable</html>", REPORT.replace("SYMBOL,OPTION NAME", "BAD,HEADER")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_report(raw)

    def test_missing_or_corrupt_cache_stays_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "list.json"
            self.assertEqual(load_report(path)["status"], "unavailable")
            for raw in ("bad", "[]", '{"status":"available","symbols":[1]}'):
                path.write_text(raw)
                self.assertEqual(load_report(path)["status"], "unavailable")
            path.write_text(json.dumps(parse_report(REPORT)))
            self.assertEqual(load_report(path)["status"], "available")
