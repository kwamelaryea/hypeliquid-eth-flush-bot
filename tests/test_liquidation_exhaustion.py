import json
import tempfile
import unittest
from pathlib import Path

from liquidation_exhaustion import (
    HypothesisBSplitConfig,
    evaluate_exhaustion_signal,
    run_hypothesis_b_simulation,
    run_hypothesis_b_validation,
    main,
)


INTERVAL = 300_000


class LiquidationExhaustionTests(unittest.TestCase):
    def candle(self, index, open_price=100.0, high=101.0, low=99.0, close=100.0):
        return {
            "ts": (index + 1) * INTERVAL - 1,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
        }

    def burst_events(self):
        return [
            {"ts": 100_000, "price": 100.0, "notional_usd": 400_000.0},
            {"ts": 350_000, "price": 100.0, "notional_usd": 300_000.0},
            {"ts": 700_000, "price": 100.0, "notional_usd": 400_000.0},
        ]

    def test_signal_uses_only_preceding_three_candles_and_next_open(self):
        candles = [self.candle(index, low=95.0 if index == 1 else 99.0) for index in range(5)]
        candles[3]["low"] = 95.0
        candles[4]["open"] = 101.0
        events = self.burst_events() + [
            # This confirmation-interval liquidation is reported separately and
            # cannot make the preceding burst pass.
            {"ts": 950_000, "price": 100.0, "notional_usd": 100_000.0},
            # Future events are not visible to the completed confirmation.
            {"ts": 1_250_000, "price": 100.0, "notional_usd": 9_000_000.0},
        ]

        signal = evaluate_exhaustion_signal(candles, events, confirmation_index=3)

        self.assertEqual(signal["burst_total_notional_usd"], 1_100_000.0)
        self.assertEqual(signal["confirmation_total_notional_usd"], 100_000.0)
        self.assertEqual(signal["burst_threshold_usd"], 1_000_000.0)
        self.assertAlmostEqual(signal["confirmation_threshold_usd"], 1_100_000.0 / 6.0)
        self.assertEqual(signal["burst_low"], 95.0)
        self.assertEqual(signal["confirmation_low"], 95.0)
        self.assertTrue(signal["eligible"])
        self.assertEqual(signal["reason"], "eligible")
        self.assertEqual(signal["entry_ts"], candles[4]["ts"])
        self.assertEqual(signal["entry_price"], 101.0)

    def test_signal_denies_new_low_and_insufficient_history(self):
        candles = [self.candle(index, low=99.0) for index in range(5)]
        candles[3]["low"] = 98.0

        denied = evaluate_exhaustion_signal(candles, self.burst_events(), confirmation_index=3)
        self.assertFalse(denied["eligible"])
        self.assertEqual(denied["reason"], "confirmation_made_new_low")

        insufficient = evaluate_exhaustion_signal(candles, [], confirmation_index=2)
        self.assertFalse(insufficient["eligible"])
        self.assertEqual(insufficient["reason"], "insufficient_preceding_candles")

    def test_simulation_uses_fixed_dollar_risk_next_open_and_same_bar_stop_priority(self):
        candles = [self.candle(index) for index in range(5)]
        candles[3]["low"] = 99.0
        candles[4] = self.candle(4, open_price=100.0, high=101.0, low=99.0, close=100.0)

        result = run_hypothesis_b_simulation(candles, self.burst_events())

        trade = result["trades"][0]
        decision = next(item for item in result["decisions"] if item["confirmation_index"] == 3)
        self.assertEqual(decision["execution_decision"], "scheduled")
        self.assertEqual(trade["entry_ts"], candles[4]["ts"])
        self.assertEqual(trade["entry_price"], 100.0)
        self.assertEqual(trade["exit_reason"], "stop_loss_same_bar_priority")
        self.assertAlmostEqual(trade["gross_pnl"], -1.0)
        self.assertAlmostEqual(trade["quantity"], 1.0)
        self.assertAlmostEqual(trade["fees"], 0.0995)
        self.assertEqual(result["summary"]["scheduled_signals"], 1)
        self.assertEqual(result["summary"]["trades"], 1)

    def test_validation_has_non_overlapping_chronological_boundaries_and_stop_rule(self):
        candles = [self.candle(index) for index in range(18)]
        result = run_hypothesis_b_validation(
            candles,
            [],
            HypothesisBSplitConfig(
                development_bars=6,
                validation_window_bars=3,
                validation_step_bars=3,
                holdout_bars=6,
            ),
        )

        self.assertEqual(result["split_boundaries"]["development"], {"start_index": 0, "end_index": 6})
        self.assertEqual(
            [window["boundaries"] for window in result["walk_forward"]],
            [
                {"start_index": 6, "end_index": 9},
                {"start_index": 9, "end_index": 12},
            ],
        )
        self.assertEqual(result["split_boundaries"]["holdout"], {"start_index": 12, "end_index": 18})
        self.assertEqual(result["holdout"]["stop_decision"]["status"], "FAIL")
        self.assertIn("fewer than 100 trades", result["holdout"]["stop_decision"]["reasons"])

    def test_validation_rejects_insufficient_data_and_overlapping_windows(self):
        candles = [self.candle(index) for index in range(11)]
        with self.assertRaisesRegex(ValueError, "insufficient candles"):
            run_hypothesis_b_validation(
                candles,
                [],
                HypothesisBSplitConfig(6, 3, 3, 6),
            )
        with self.assertRaisesRegex(ValueError, "overlap"):
            run_hypothesis_b_validation(
                [self.candle(index) for index in range(18)],
                [],
                HypothesisBSplitConfig(6, 3, 2, 6),
            )

    def test_cli_runs_from_local_files_with_explicit_split_arguments(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candle_path = root / "candles.json"
            liquidations_path = root / "normalized_events.json"
            output_path = root / "result.json"
            candle_path.write_text(json.dumps([self.candle(index) for index in range(18)]), encoding="utf-8")
            liquidations_path.write_text(json.dumps([]), encoding="utf-8")

            result_code = main([
                "--candles", str(candle_path),
                "--liquidations", str(liquidations_path),
                "--output", str(output_path),
                "--development-bars", "6",
                "--validation-window-bars", "3",
                "--validation-step-bars", "3",
                "--holdout-bars", "6",
            ])

            self.assertEqual(result_code, 0)
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["counts"]["input_candles"], 18)
            self.assertEqual(result["holdout"]["boundaries"], {"end_index": 18, "start_index": 12})


if __name__ == "__main__":
    unittest.main()
