import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.weekly_options import load_report, main, parse_report, weekly_universe


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

    def test_ensure_reuses_valid_cache_but_does_not_accept_invalid_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "list.json"
            with patch("sys.argv", ["weekly_options", "--ensure", "--output", str(path)]), patch("scripts.weekly_options.urlopen", side_effect=OSError("blocked")) as download:
                with self.assertRaises(OSError):
                    main()
                path.write_text(json.dumps(parse_report(REPORT)))
                download.reset_mock()
                main()
                download.assert_not_called()

    def test_download_uses_occ_accepted_user_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "list.json"
            with patch("sys.argv", ["weekly_options", "--output", str(path)]), patch("scripts.weekly_options.urlopen", return_value=io.BytesIO(REPORT.encode())) as download:
                main()
                request = download.call_args.args[0]
                self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0")
                self.assertEqual(load_report(path)["symbols"], ["AAPL", "BRKB"])
