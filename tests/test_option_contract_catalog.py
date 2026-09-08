import unittest

from scripts.option_contract_catalog import (
    build_option_contract_catalog,
    plan_option_subscriptions,
)


def option_symbol(strike, side):
    return f"TSEM  260911{side}{round(strike * 1000):08d}"


class OptionContractCatalogTests(unittest.TestCase):
    def test_definition_catalog_includes_unquoted_shifted_strikes(self):
        records = [
            {"raw_symbol": option_symbol(strike, side)}
            for strike in (217.5, 220, 222.5, 225, 227.5, 230)
            for side in ("C", "P")
        ]
        rows = [{
            "ticker": "TSEM",
            "prior_close_price": 206.16,
            "anchor_price": 222.335,
            "last_price": 222.335,
            "iv_chain": {"occ_root": "TSEM", "strikes": [217.5, 220, 230]},
            "iv_expansion_reference": {
                "forward_price": 206.7449566936682,
                "log_moneyness": [
                    -0.020746053800524265,
                    -0.00847596120870987,
                    0.0036453993236349887,
                    0.015621590370350618,
                    0.027456048017353397,
                ],
            },
        }]

        catalog = build_option_contract_catalog(
            records,
            rows,
            as_of_date="2026-09-04",
            option_expiry="2026-09-11",
        )
        symbols, plans = plan_option_subscriptions(rows, catalog)

        self.assertEqual(
            [contract["strike"] for contract in catalog["contracts"]["TSEM"]],
            [217.5, 220.0, 222.5, 225.0, 227.5, 230.0],
        )
        self.assertIn(option_symbol(222.5, "C"), symbols)
        self.assertIn(option_symbol(225, "P"), symbols)
        self.assertEqual(plans["TSEM"]["selected_strikes"], [
            217.5, 220.0, 222.5, 225.0, 227.5, 230.0,
        ])

    def test_catalog_requires_both_contract_sides(self):
        rows = [{
            "ticker": "TSEM",
            "iv_chain": {"occ_root": "TSEM"},
            "iv_expansion_reference": {"log_moneyness": [0.0]},
            "anchor_price": 220,
        }]
        catalog = build_option_contract_catalog(
            [{"raw_symbol": option_symbol(220, "C")}],
            rows,
            as_of_date="2026-09-04",
            option_expiry="2026-09-11",
        )
        self.assertEqual(catalog["contracts"], {})

    def test_current_stock_price_moves_the_selected_moneyness(self):
        rows = [{
            "ticker": "TSEM",
            "prior_close_price": 100,
            "anchor_price": 100,
            "iv_chain": {"occ_root": "TSEM"},
            "iv_expansion_reference": {
                "forward_price": 100,
                "log_moneyness": [0.0],
            },
        }]
        records = [
            {"raw_symbol": option_symbol(strike, side)}
            for strike in (90, 100, 110, 120)
            for side in ("C", "P")
        ]
        catalog = build_option_contract_catalog(
            records,
            rows,
            as_of_date="2026-09-04",
            option_expiry="2026-09-11",
        )

        _, plans = plan_option_subscriptions(
            rows,
            catalog,
            spot_prices={"TSEM": 120},
            neighbor_count=0,
        )

        self.assertEqual(plans["TSEM"]["selected_strikes"], [120.0])


if __name__ == "__main__":
    unittest.main()
