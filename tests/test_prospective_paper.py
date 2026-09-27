import inspect
import json
import tempfile
import unittest
from pathlib import Path

import prospective_paper
from prospective_paper import LedgerPaths, run_prospective_paper


INTERVAL = 300_000


class ProspectivePaperTests(unittest.TestCase):
    def candles(self, count=5):
        return [
            {"ts": (index + 1) * INTERVAL - 1, "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0}
            for index in range(count)
        ]

    def events(self):
        return [
            {"event_id": "a", "ts": 100_000, "price": 100.0, "notional_usd": 400_000.0},
            {"event_id": "b", "ts": 350_000, "price": 100.0, "notional_usd": 300_000.0},
            {"event_id": "c", "ts": 700_000, "price": 100.0, "notional_usd": 400_000.0},
        ]

    def run_ledger(self, root, candles=None, events=None, source="fixture/local-v1"):
        return run_prospective_paper(candles if candles is not None else self.candles(),
                                     events if events is not None else self.events(),
                                     state_dir=root / "state", manifest=root / "manifest" / "frozen.json",
                                     source=source)

    def test_first_run_creates_frozen_manifest_and_all_journals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_ledger(root)
            manifest = json.loads((root / "manifest" / "frozen.json").read_text())
            self.assertEqual(manifest["hypothesis_name"], "liquidation_exhaustion_hypothesis_b")
            self.assertEqual(manifest["parameters"], prospective_paper.frozen_parameters())
            self.assertEqual(result["label"], prospective_paper.PAPER_LABEL)
            paths = LedgerPaths.in_state_dir(root / "state")
            for path in (paths.candles, paths.events, paths.decisions, paths.trades):
                self.assertTrue(path.exists(), path)
                self.assertIn("PAPER/SIMULATION ONLY", path.read_text())

    def test_config_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_ledger(root)
            with self.assertRaisesRegex(ValueError, "drift"):
                self.run_ledger(root, source="fixture/local-v2")

    def test_idempotent_rerun_does_not_append_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_ledger(root)
            paths = LedgerPaths.in_state_dir(root / "state")
            before = {path: path.read_text() for path in (paths.candles, paths.events, paths.decisions, paths.trades)}
            second = self.run_ledger(root)
            self.assertEqual(second["new_records"], {"candles": 0, "events": 0, "decisions": 0, "trade_records": 0})
            self.assertEqual(before, {path: path.read_text() for path in before})

    def test_rejects_malformed_and_out_of_order_batches_and_ignores_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "ordered"):
                self.run_ledger(root, candles=list(reversed(self.candles())))
            with self.assertRaisesRegex(ValueError, "missing price"):
                self.run_ledger(root, events=[{"ts": 1, "notional_usd": 1}])
            incomplete = self.candles(1)[0]
            incomplete["complete"] = False
            result = self.run_ledger(root, candles=[incomplete], events=[])
            self.assertEqual(result["new_records"]["candles"], 0)

    def test_persists_signal_decisions_entries_exits_and_false_promotion_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_ledger(root)
            paths = LedgerPaths.in_state_dir(root / "state")
            decisions = [json.loads(line) for line in paths.decisions.read_text().splitlines()]
            trades = [json.loads(line) for line in paths.trades.read_text().splitlines()]
            self.assertTrue(decisions)
            self.assertTrue(all("eligible" in item and "reason" in item and "execution_decision" in item and "current_trade_count" in item for item in decisions))
            self.assertEqual({item["record_type"] for item in trades}, {"simulated_trade_entry", "simulated_trade_exit"})
            self.assertIn("cumulative_paper_pnl", trades[-1])
            self.assertFalse(result["summary"]["promotion_ready"])
            self.assertLess(result["summary"]["trades"], 100)

    def test_module_has_no_live_or_exchange_imports(self):
        source = inspect.getsource(prospective_paper)
        for forbidden in ("requests", "ccxt", "hyperliquid", "websocket", "dex_trader"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
