import argparse
import unittest

import pandas as pd

from scripts.nasdaq_universe_movers import resolve_run_config


class RunConfigTests(unittest.TestCase):
    def test_liquidity_sessions_exclude_exchange_holidays(self):
        args = argparse.Namespace(
            window_start=pd.Timestamp("2026-09-04"),
            window_end=pd.Timestamp("2026-09-08"),
            option_expiry=pd.Timestamp("2026-09-11"),
            option_expiry_close=None,
            spot_dataset="EQUS.MINI",
            return_anchor_field="close",
            option_snapshot_start=None,
            option_snapshot_end=None,
            option_valuation_time=None,
            endpoint_time=None,
            option_cache_dir=None,
        )

        config = resolve_run_config(args)

        self.assertEqual(
            [str(value) for value in config["week_dates"]],
            ["2026-09-04", "2026-09-08"],
        )


if __name__ == "__main__":
    unittest.main()
