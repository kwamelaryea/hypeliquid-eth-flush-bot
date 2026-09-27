import unittest

from accounting import compute_economic_summary


class AccountingTests(unittest.TestCase):
    def test_economic_summary_includes_closed_pnl_fees_funding_and_transfers(self):
        fills = [
            {"closedPnl": "2.50", "fee": "0.10"},
            {"closedPnl": "-1.00", "fee": "0.05"},
        ]
        funding = [
            {"delta": {"usdc": "-0.20"}},
            {"delta": {"usdc": "0.03"}},
        ]
        transfers = [
            {"delta": {"usdc": "5.00"}},
        ]

        summary = compute_economic_summary(fills, funding, transfers)

        self.assertEqual(summary["gross_closed_pnl"], 1.5)
        self.assertEqual(summary["fees"], -0.15)
        self.assertEqual(summary["funding"], -0.17)
        self.assertEqual(summary["transfers"], 5.0)
        self.assertEqual(summary["trading_pnl_before_transfers"], 1.18)
        self.assertEqual(summary["equity_delta_explained"], 6.18)


if __name__ == "__main__":
    unittest.main()
