import unittest

from paper_baseline import (
    BaselineConfig,
    WalkForwardConfig,
    calculate_rsi,
    run_paper_baseline,
    run_walk_forward_validation,
)


class PaperBaselineTests(unittest.TestCase):
    def candle(self, ts, open_price, high, low, close, rsi=50):
        return {
            "ts": ts,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "rsi": rsi,
        }

    def test_calculates_rsi_when_candles_do_not_supply_it(self):
        candles = [
            {"ts": index, "open": price, "high": price, "low": price, "close": price}
            for index, price in enumerate([10, 9, 8, 7, 6, 5, 4, 3, 2])
        ]

        values = calculate_rsi(candles, period=8)

        self.assertEqual(values[:8], [None] * 8)
        self.assertEqual(values[8], 0.0)

    def test_enters_at_next_candle_open_after_completed_rsi_signal(self):
        candles = [
            self.candle(0, 110, 111, 109, 110),
            self.candle(60_000, 100, 101, 99, 100, rsi=20),
            self.candle(120_000, 91, 93, 90, 92),
            self.candle(180_000, 94, 102, 93, 101),
        ]
        result = run_paper_baseline(
            candles,
            [],
            BaselineConfig(take_profit_pct=0.10, stop_loss_pct=0.20),
        )

        decision = result["control"]["decisions"][0]
        trade = result["control"]["trades"][0]
        self.assertEqual(decision["entry_ts"], 120_000)
        self.assertEqual(trade["entry_ts"], 120_000)
        self.assertEqual(trade["entry_price"], 91.0)

    def test_treatment_does_not_use_a_future_liquidation_event(self):
        candles = [
            self.candle(0, 101, 102, 100, 101),
            self.candle(100, 100, 101, 99, 100, rsi=20),
            self.candle(200, 99, 100, 98, 99),
        ]
        events = [{"ts": 150, "price": 90, "notional_usd": 1_000}]

        result = run_paper_baseline(candles, events, BaselineConfig(event_lookback_ms=1_000))

        self.assertEqual(result["treatment"]["summary"]["eligible_treatment_signals"], 0)
        self.assertEqual(result["treatment"]["summary"]["rejected_treatment_signals"], 1)
        self.assertEqual(result["treatment"]["trades"], [])

    def test_treatment_accepts_observed_normalized_event_at_or_below_signal_price(self):
        candles = [
            self.candle(0, 101, 102, 100, 101),
            self.candle(100, 100, 101, 99, 100, rsi=20),
            self.candle(200, 100, 110, 99, 105),
        ]
        events = [{"ts": 100, "price": 100, "notional_usd": 1_000}]

        result = run_paper_baseline(
            candles,
            events,
            BaselineConfig(
                event_lookback_ms=1_000,
                min_liquidation_notional_usd=1_000,
                take_profit_pct=0.10,
                stop_loss_pct=0.20,
            ),
        )

        self.assertEqual(result["treatment"]["summary"]["eligible_treatment_signals"], 1)
        self.assertEqual(result["treatment"]["summary"]["rejected_treatment_signals"], 0)
        self.assertEqual(result["treatment"]["summary"]["trades"], 1)

    def test_event_without_normalized_notional_is_ignored(self):
        candles = [
            self.candle(0, 101, 102, 100, 101),
            self.candle(100, 100, 101, 99, 100, rsi=20),
            self.candle(200, 99, 100, 98, 99),
        ]
        events = [{"ts": 100, "price": 90, "sz": "999999"}]

        result = run_paper_baseline(candles, events, BaselineConfig(event_lookback_ms=1_000))

        self.assertEqual(result["treatment"]["summary"]["eligible_treatment_signals"], 0)
        self.assertEqual(result["treatment"]["summary"]["rejected_treatment_signals"], 1)

    def test_same_bar_tp_and_sl_uses_stop_loss_priority(self):
        candles = [
            self.candle(0, 101, 102, 100, 101),
            self.candle(100, 100, 101, 99, 100, rsi=20),
            self.candle(200, 100, 106, 94, 100),
        ]

        result = run_paper_baseline(
            candles,
            [],
            BaselineConfig(take_profit_pct=0.05, stop_loss_pct=0.05, round_trip_fee_bps=0),
        )

        trade = result["control"]["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop_loss_same_bar_priority")
        self.assertEqual(trade["exit_price"], 95.0)
        self.assertEqual(trade["gross_pnl"], -1.0)
        self.assertEqual(trade["quantity"], 0.2)
        self.assertEqual(trade["entry_notional"], 20.0)
        self.assertEqual(trade["risk_budget_usd"], 1.0)

    def test_round_trip_fee_is_included_in_trade_and_summary(self):
        candles = [
            self.candle(0, 101, 102, 100, 101),
            self.candle(100, 100, 101, 99, 100, rsi=20),
            self.candle(200, 100, 110, 99, 105),
        ]

        result = run_paper_baseline(
            candles,
            [],
            BaselineConfig(take_profit_pct=0.10, stop_loss_pct=0.20, round_trip_fee_bps=200),
        )

        trade = result["control"]["trades"][0]
        summary = result["control"]["summary"]
        self.assertAlmostEqual(trade["gross_pnl"], 0.5)
        self.assertAlmostEqual(trade["entry_notional"], 5.0)
        self.assertAlmostEqual(trade["exit_notional"], 5.5)
        self.assertAlmostEqual(trade["entry_fee"], 0.05)
        self.assertAlmostEqual(trade["exit_fee"], 0.055)
        self.assertAlmostEqual(trade["fees"], 0.105)
        self.assertAlmostEqual(trade["net_pnl"], 0.395)
        self.assertAlmostEqual(summary["fees"], 0.105)
        self.assertAlmostEqual(summary["net_pnl"], 0.395)

    def test_rejects_non_positive_risk_and_negative_fees(self):
        with self.assertRaisesRegex(ValueError, "risk_per_trade_usd must be positive"):
            run_paper_baseline([], [], BaselineConfig(risk_per_trade_usd=0))
        with self.assertRaisesRegex(ValueError, "round_trip_fee_bps must be non-negative"):
            run_paper_baseline([], [], BaselineConfig(round_trip_fee_bps=-0.01))

    def test_fixed_dollar_risk_has_same_stop_loss_at_different_entry_prices(self):
        candles = [
            self.candle(0, 100, 101, 99, 100, rsi=20),
            self.candle(100, 100, 101, 89, 95),
            self.candle(200, 100, 101, 99, 100, rsi=20),
            self.candle(300, 200, 201, 178, 190),
        ]

        result = run_paper_baseline(
            candles,
            [],
            BaselineConfig(stop_loss_pct=0.10, take_profit_pct=0.50, round_trip_fee_bps=0),
        )

        trades = result["control"]["trades"]
        self.assertEqual(len(trades), 2)
        self.assertAlmostEqual(trades[0]["quantity"], 0.1)
        self.assertAlmostEqual(trades[0]["entry_notional"], 10.0)
        self.assertAlmostEqual(trades[1]["quantity"], 0.05)
        self.assertAlmostEqual(trades[1]["entry_notional"], 10.0)
        self.assertAlmostEqual(trades[0]["gross_pnl"], -1.0)
        self.assertAlmostEqual(trades[1]["gross_pnl"], -1.0)

    def test_walk_forward_boundaries_holdout_and_variants_are_separate(self):
        candles = [self.candle(index, 100, 101, 99, 100, rsi=20) for index in range(20)]
        result = run_walk_forward_validation(
            candles,
            [],
            WalkForwardConfig(train_bars=4, validation_bars=4, holdout_bars=4, step_bars=4),
            BaselineConfig(take_profit_pct=0.50, stop_loss_pct=0.50, max_hold_bars=1),
        )

        self.assertEqual(result["split_boundaries"]["development"], {"start_index": 0, "end_index": 4})
        self.assertEqual(
            [window["boundaries"] for window in result["walk_forward"]],
            [
                {"start_index": 4, "end_index": 8},
                {"start_index": 8, "end_index": 12},
                {"start_index": 12, "end_index": 16},
            ],
        )
        self.assertEqual(result["holdout"]["boundaries"], {"start_index": 16, "end_index": 20})
        self.assertEqual(result["counts"]["holdout_evaluations"], 1)
        self.assertEqual(result["counts"]["walk_forward_windows"], 3)
        self.assertEqual(result["walk_forward"][0]["control"]["summary"]["trades"], 1)
        self.assertEqual(result["walk_forward"][0]["treatment"]["summary"]["trades"], 0)
        self.assertFalse(result["parameters_fitted_or_tuned"])

    def test_walk_forward_rejects_insufficient_data(self):
        candles = [self.candle(index, 100, 101, 99, 100, rsi=20) for index in range(11)]

        with self.assertRaisesRegex(ValueError, "insufficient candles"):
            run_walk_forward_validation(
                candles,
                [],
                WalkForwardConfig(train_bars=4, validation_bars=4, holdout_bars=4, step_bars=4),
            )

    def test_control_and_treatment_diverge_without_eligible_liquidation(self):
        candles = [
            self.candle(0, 101, 102, 100, 101),
            self.candle(100, 100, 101, 99, 100, rsi=20),
            self.candle(200, 100, 110, 99, 105),
        ]

        result = run_paper_baseline(
            candles,
            [],
            BaselineConfig(take_profit_pct=0.10, stop_loss_pct=0.20),
        )

        self.assertEqual(result["control"]["summary"]["trades"], 1)
        self.assertEqual(result["treatment"]["summary"]["trades"], 0)
        self.assertEqual(result["treatment"]["summary"]["rejected_treatment_signals"], 1)


if __name__ == "__main__":
    unittest.main()
