"""Fail-closed, long-only economic policy for the Hyperliquid research baseline.

This module is deliberately independent from exchange clients. It decides whether a
candidate long is admissible after estimated costs and computes fixed-risk quantity.
It is not a profitability claim and must be validated before integration with live
execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PolicyConfig:
    max_feed_age_seconds: float = 30.0
    min_net_reward_risk: float = 1.5

    def __post_init__(self):
        values = (self.max_feed_age_seconds, self.min_net_reward_risk)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError("policy thresholds must be finite and non-negative")


@dataclass(frozen=True)
class CostModel:
    round_trip_fee_pct: float
    slippage_pct: float
    funding_pct: float
    adverse_selection_pct: float = 0.0

    def __post_init__(self):
        values = (
            self.round_trip_fee_pct,
            self.slippage_pct,
            self.funding_pct,
            self.adverse_selection_pct,
        )
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError("costs must be finite and non-negative")

    @property
    def total_pct(self) -> float:
        return (
            self.round_trip_fee_pct
            + self.slippage_pct
            + self.funding_pct
            + self.adverse_selection_pct
        )


@dataclass(frozen=True)
class EntryContext:
    side: str
    expected_gross_move_pct: float
    stop_distance_pct: float
    feed_age_seconds: float
    required_feeds_healthy: bool


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    reason: str
    expected_net_move_pct: float
    cost_pct: float
    reward_risk: float


def estimate_cumulative_funding_cost_pct(hourly_rate: float, holding_hours: float) -> float:
    """Convert a decimal hourly funding rate to cumulative percentage cost.

    Hyperliquid's funding rate is hourly and expressed as a decimal (for example,
    0.001 is 0.1% per hour).  The economic gate uses an explicit expected holding
    horizon, and treats either payment or credit conservatively as a cost.
    """
    if (
        not math.isfinite(hourly_rate)
        or not math.isfinite(holding_hours)
        or holding_hours < 0
        or hourly_rate <= -1
    ):
        raise ValueError("funding rate and holding horizon must be valid")
    return abs(hourly_rate) * holding_hours * 100.0


class LongOnlyPolicy:
    """Apply the minimum economic and data-health gates before an entry."""

    def __init__(self, config: PolicyConfig):
        self.config = config

    def evaluate(self, context: EntryContext, costs: CostModel) -> PolicyDecision:
        if context.side != "long":
            return PolicyDecision(False, "shorts_disabled", 0.0, costs.total_pct, 0.0)
        if not context.required_feeds_healthy:
            return PolicyDecision(False, "required_feed_unhealthy", 0.0, costs.total_pct, 0.0)
        if (
            not math.isfinite(context.feed_age_seconds)
            or context.feed_age_seconds < 0
            or context.feed_age_seconds > self.config.max_feed_age_seconds
        ):
            return PolicyDecision(False, "stale_feed", 0.0, costs.total_pct, 0.0)
        if (
            not math.isfinite(context.expected_gross_move_pct)
            or not math.isfinite(context.stop_distance_pct)
            or context.expected_gross_move_pct <= 0
            or context.stop_distance_pct <= 0
            or costs.total_pct < 0
        ):
            return PolicyDecision(False, "invalid_trade_inputs", 0.0, costs.total_pct, 0.0)

        expected_net = context.expected_gross_move_pct - costs.total_pct
        reward_risk = expected_net / context.stop_distance_pct
        if reward_risk < self.config.min_net_reward_risk:
            return PolicyDecision(
                False,
                "insufficient_after_cost_edge",
                expected_net,
                costs.total_pct,
                reward_risk,
            )
        return PolicyDecision(True, "accepted", expected_net, costs.total_pct, reward_risk)


def compute_position_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    risk_fraction: float,
    notional_cap_fraction: float,
) -> float:
    """Return base quantity sized by fixed risk and capped notional.

    The caller must still apply Hyperliquid lot/size precision and verify the
    exchange position after the fill.
    """
    values = (equity, entry_price, stop_price, risk_fraction, notional_cap_fraction)
    if not all(math.isfinite(value) for value in values):
        return 0.0
    if equity <= 0 or entry_price <= 0 or stop_price <= 0:
        return 0.0
    if risk_fraction <= 0 or notional_cap_fraction <= 0:
        return 0.0
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return 0.0

    risk_budget = equity * risk_fraction
    quantity_by_risk = risk_budget / stop_distance
    quantity_by_cap = (equity * notional_cap_fraction) / entry_price
    return max(0.0, min(quantity_by_risk, quantity_by_cap))
