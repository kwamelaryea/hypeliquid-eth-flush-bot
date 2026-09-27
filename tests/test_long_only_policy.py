import unittest

from long_only_policy import (
    CostModel,
    EntryContext,
    LongOnlyPolicy,
    PolicyConfig,
    compute_position_size,
)


class LongOnlyPolicyTests(unittest.TestCase):
    def test_rejects_short_side(self):
        policy = LongOnlyPolicy(PolicyConfig())
        decision = policy.evaluate(
            EntryContext(
                side="short",
                expected_gross_move_pct=3.0,
                stop_distance_pct=1.0,
                feed_age_seconds=1,
                required_feeds_healthy=True,
            ),
            CostModel(round_trip_fee_pct=0.10, slippage_pct=0.05, funding_pct=0.02),
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "shorts_disabled")

    def test_rejects_trade_when_costs_consume_edge(self):
        policy = LongOnlyPolicy(PolicyConfig(min_net_reward_risk=1.5))
        decision = policy.evaluate(
            EntryContext(
                side="long",
                expected_gross_move_pct=1.0,
                stop_distance_pct=1.0,
                feed_age_seconds=1,
                required_feeds_healthy=True,
            ),
            CostModel(round_trip_fee_pct=0.20, slippage_pct=0.45, funding_pct=0.10),
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "insufficient_after_cost_edge")

    def test_rejects_stale_or_unhealthy_data(self):
        policy = LongOnlyPolicy(PolicyConfig(max_feed_age_seconds=30))
        stale = policy.evaluate(
            EntryContext(
                side="long",
                expected_gross_move_pct=4.0,
                stop_distance_pct=1.0,
                feed_age_seconds=31,
                required_feeds_healthy=True,
            ),
            CostModel(round_trip_fee_pct=0.1, slippage_pct=0.1, funding_pct=0.0),
        )
        unhealthy = policy.evaluate(
            EntryContext(
                side="long",
                expected_gross_move_pct=4.0,
                stop_distance_pct=1.0,
                feed_age_seconds=1,
                required_feeds_healthy=False,
            ),
            CostModel(round_trip_fee_pct=0.1, slippage_pct=0.1, funding_pct=0.0),
        )
        self.assertEqual(stale.reason, "stale_feed")
        self.assertEqual(unhealthy.reason, "required_feed_unhealthy")

    def test_size_is_fixed_risk_and_notional_capped(self):
        size = compute_position_size(
            equity=1000.0,
            entry_price=2000.0,
            stop_price=1960.0,
            risk_fraction=0.005,
            notional_cap_fraction=0.25,
        )
        self.assertAlmostEqual(size, 0.125)

    def test_rejects_nonfinite_or_negative_costs(self):
        for field, value in (("round_trip_fee_pct", float("nan")), ("slippage_pct", float("inf")), ("funding_pct", -0.1), ("adverse_selection_pct", -1.0)):
            with self.subTest(field=field):
                values = {"round_trip_fee_pct": 0.1, "slippage_pct": 0.1, "funding_pct": 0.1, "adverse_selection_pct": 0.0}
                values[field] = value
                with self.assertRaises(ValueError):
                    CostModel(**values)

    def test_rejects_invalid_policy_configuration(self):
        with self.assertRaises(ValueError):
            LongOnlyPolicy(PolicyConfig(max_feed_age_seconds=float("nan")))
        with self.assertRaises(ValueError):
            LongOnlyPolicy(PolicyConfig(min_net_reward_risk=-0.1))

    def test_funding_hourly_rate_is_converted_to_horizon_cost(self):
        from long_only_policy import estimate_cumulative_funding_cost_pct

        self.assertAlmostEqual(estimate_cumulative_funding_cost_pct(0.001, 24), 2.4)

    def test_size_is_zero_for_invalid_stop(self):
        self.assertEqual(
            compute_position_size(1000.0, 2000.0, 2000.0, 0.005, 0.25), 0.0
        )


if __name__ == "__main__":
    unittest.main()
