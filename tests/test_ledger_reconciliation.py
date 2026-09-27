import unittest

from ledger_reconciliation import reconcile_ledger


class LedgerReconciliationTests(unittest.TestCase):
    def test_reconciles_observed_hyperliquid_record_shapes(self):
        fills = [
            {"closedPnl": "-2.0000", "fee": "0.5000", "side": "A", "dir": "Close Long"},
            {"closedPnl": "-2.2030", "fee": "7.2472", "side": "B", "dir": "Close Short"},
        ]
        funding = [
            {"delta": {"usdc": "-0.5000"}},
            {"delta": {"usdc": "-0.0867"}},
        ]
        ledger = [
            {"delta": {"type": "deposit", "usdc": "100.00"}},
            {"delta": {"type": "withdraw", "amount": "-20.00"}},
            {"delta": {"type": "internalTransfer", "usdcValue": "3.00"}},
            {"delta": {"type": "mysteryCredit", "usdc": "1.25"}},
        ]

        summary = reconcile_ledger(
            fills,
            funding,
            ledger,
            current_equity={"marginSummary": {"accountValue": "148.7131"}},
            initial_equity="80.00",
        )

        self.assertEqual(summary["gross_closed_pnl"], -4.203)
        self.assertEqual(summary["fees"], -7.7472)
        self.assertEqual(summary["funding_total"], -0.5867)
        self.assertEqual(
            summary["transfers_by_type"],
            {"deposit": 100.0, "withdraw": -20.0, "internalTransfer": 3.0, "mysteryCredit": 1.25},
        )
        self.assertEqual(summary["transfers_total"], 84.25)
        self.assertEqual(summary["external_transfers_total"], 81.25)
        self.assertEqual(summary["trading_pnl_before_transfers"], -12.5369)
        self.assertEqual(summary["explained_equity_delta"], 68.7131)
        self.assertEqual(summary["residual"], 0.0)
        self.assertEqual(summary["fill_count"], 2)
        self.assertEqual(summary["fill_side_counts"], {"A": 1, "B": 1})
        self.assertEqual(summary["fill_direction_counts"], {"Close Long": 1, "Close Short": 1})

    def test_unknown_ledger_type_is_preserved_and_never_added_to_trade_pnl(self):
        summary = reconcile_ledger(
            fills=[],
            funding_records=[],
            ledger_records=[{"delta": {"type": "unrecognizedEvent", "usdc": "4.50"}}],
        )

        self.assertEqual(summary["gross_closed_pnl"], 0.0)
        self.assertEqual(summary["transfers_by_type"], {"unrecognizedEvent": 4.5})
        self.assertEqual(summary["transfers_total"], 4.5)
        self.assertEqual(summary["explained_equity_delta"], 4.5)
        self.assertIsNone(summary["residual"])

    def test_equity_residual_requires_both_snapshot_and_baseline(self):
        summary = reconcile_ledger(
            fills=[],
            funding_records=[],
            ledger_records=[],
            current_equity={"marginSummary": {"accountValue": "10"}},
        )

        self.assertIsNone(summary["residual"])


if __name__ == "__main__":
    unittest.main()
