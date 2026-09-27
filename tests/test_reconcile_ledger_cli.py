import io
import json
import unittest
from unittest.mock import Mock

from reconcile_ledger import build_reconciliation_report, main


class ReconcileLedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.records = {
            "userFills": [
                {"closedPnl": "-2.0000", "fee": "0.5000", "side": "A", "dir": "Close Long"},
                {"closedPnl": "-2.2030", "fee": "7.2472", "side": "B", "dir": "Close Short"},
            ],
            "userFunding": [
                {"delta": {"usdc": "-0.5000"}},
                {"delta": {"usdc": "-0.0867"}},
            ],
            "userNonFundingLedgerUpdates": [
                {"delta": {"type": "deposit", "usdc": "100.00"}},
                {"delta": {"type": "withdraw", "amount": "-20.00"}},
                {"delta": {"type": "accountClassTransfer", "usdcValue": "3.00"}},
            ],
            "clearinghouseState": {
                "marginSummary": {"accountValue": "151.7131"},
                "assetPositions": [
                    {
                        "position": {
                            "coin": "ETH",
                            "szi": "0.5",
                            "entryPx": "3000.00",
                            "unrealizedPnl": "12.34",
                            "leverage": {"type": "cross", "value": 3},
                        }
                    },
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0",
                            "entryPx": None,
                            "unrealizedPnl": "0.00",
                            "leverage": {"type": "cross", "value": 3},
                        }
                    },
                ],
            },
        }

    def _fetcher(self):
        return Mock(side_effect=lambda _url, payload: self.records[payload["type"]])

    def test_builds_reconciliation_from_hyperliquid_records_with_open_positions_separate(self):
        fetcher = self._fetcher()

        report = build_reconciliation_report("0xabc", fetcher=fetcher)

        self.assertEqual(report["user"], "0xabc")
        self.assertEqual(report["reconciliation"]["gross_closed_pnl"], -4.203)
        self.assertEqual(report["reconciliation"]["fees"], -7.7472)
        self.assertEqual(report["reconciliation"]["funding_total"], -0.5867)
        self.assertEqual(
            report["reconciliation"]["transfers_by_type"],
            {"deposit": 100.0, "withdraw": -20.0, "accountClassTransfer": 3.0},
        )
        self.assertEqual(report["reconciliation"]["external_transfers_total"], 80.0)
        self.assertEqual(
            report["current_open_positions"],
            [
                {
                    "coin": "ETH",
                    "size": "0.5",
                    "entry_price": "3000.00",
                    "unrealized_pnl": "12.34",
                    "leverage": {"type": "cross", "value": 3},
                }
            ],
        )
        self.assertNotIn("strategy", json.dumps(report).lower())
        self.assertEqual(
            [call.args[1]["type"] for call in fetcher.call_args_list],
            ["userFills", "userFunding", "userNonFundingLedgerUpdates", "clearinghouseState"],
        )
        self.assertTrue(all(call.args[1]["user"] == "0xabc" for call in fetcher.call_args_list))

    def test_main_emits_stable_json(self):
        output = io.StringIO()

        exit_code = main(["--user", "0xabc"], fetcher=self._fetcher(), output=output)

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), json.dumps(json.loads(output.getvalue()), indent=2, sort_keys=True) + "\n")

    def test_rejects_malformed_api_response_instead_of_reconciling_as_zero(self):
        fetcher = self._fetcher()
        self.records["userFunding"] = {"error": "bad request"}

        with self.assertRaisesRegex(ValueError, "userFunding response must be a JSON array"):
            build_reconciliation_report("0xabc", fetcher=fetcher)


if __name__ == "__main__":
    unittest.main()
