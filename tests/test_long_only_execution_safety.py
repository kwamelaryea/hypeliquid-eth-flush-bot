import unittest
from unittest.mock import MagicMock, patch

import config_dex as config
import hyperliquid_bot
from hyperliquid_bot import HyperliquidFlushBot


class LongOnlyExecutionSafetyTests(unittest.TestCase):
    def make_bot(self):
        bot = HyperliquidFlushBot.__new__(HyperliquidFlushBot)
        bot.symbol = "ETH"
        bot.address = "0xabc"
        bot.info = MagicMock()
        bot.exchange = MagicMock()
        bot.log = MagicMock()
        bot.get_current_price = MagicMock(return_value=100.0)
        bot.get_position = MagicMock(return_value="none")
        bot.get_position_details = MagicMock(return_value=[])
        bot._calculate_limit_price = MagicMock(return_value=99.9)
        bot._round_price = lambda price: round(price, 1)
        bot.leverage = 1
        bot.equity_for_sizing = 1000.0
        bot.active_regime = "range"
        bot.position_stop_loss_pct = 5.0
        bot.position_take_profit_pct = 10.0
        bot._place_protective_stop = MagicMock(return_value=True)
        return bot

    def test_economic_gate_uses_cumulative_funding_horizon(self):
        bot = self.make_bot()
        bot._log_denial = MagicMock()
        with patch.object(config, "RESEARCH_EXPECTED_MOVE_INPUT_ENABLED", True, create=True), \
             patch.object(config, "RESEARCH_EXPECTED_GROSS_MOVE_PCT", 5.0, create=True), \
             patch.object(config, "RESEARCH_EXPECTED_MOVE_FEED_AGE_SECONDS", 1.0, create=True), \
             patch.object(config, "ROUND_TRIP_FEE_PCT", 0.0, create=True), \
             patch.object(config, "SLIPPAGE_PCT", 0.0, create=True), \
             patch.object(config, "ADVERSE_SELECTION_PCT", 0.0, create=True), \
             patch.object(config, "FUNDING_HORIZON_HOURS", 24.0, create=True):
            decision = bot._evaluate_long_entry_policy(100.0, 1.0, True, funding_cost_pct=0.001)

        self.assertAlmostEqual(decision.cost_pct, 2.4)

    def test_entry_uses_fixed_risk_quantity_not_margin_amount(self):
        bot = self.make_bot()
        bot.exchange.update_leverage.return_value = {"status": "ok"}
        bot.exchange.order.side_effect = [
            {
                "status": "ok",
                "response": {"data": {"statuses": [{"filled": {"totalSz": "1"}}]}},
            },
            {
                "status": "ok",
                "response": {"data": {"statuses": [{"resting": {"oid": 456}}]}},
            },
        ]

        with patch.object(config, "DRY_RUN", False), \
             patch.object(config, "LIVE_TRADING_OPT_IN", True), \
             patch.object(config, "HL_LIVE_RUNTIME_OPT_IN", True), \
             patch.object(config, "PAUSE_NEW_ENTRIES", False), \
             patch.object(config, "RISK_PER_TRADE_FRACTION", 0.005, create=True), \
             patch.object(config, "NOTIONAL_CAP_FRACTION", 0.25, create=True):
            result = bot.open_position(
                side="long", amount_usd=900.0, price=100.0,
                equity=1000.0, stop_price=95.0,
            )

        self.assertTrue(result)
        # Risk quantity is min(1000*.005/5, 1000*.25/100) = 1.0,
        # independent of the legacy margin-sized amount_usd argument.
        self.assertEqual(bot.exchange.order.call_args.args[2], 1.0)

    def test_all_short_entry_signals_are_denied_centrally(self):
        bot = self.make_bot()
        self.assertEqual(bot._enforce_long_only_signal("SHORT"), "HOLD")
        self.assertFalse(bot.open_position("short", amount_usd=100.0, price=100.0))
        bot.exchange.order.assert_not_called()
        bot.exchange.market_open.assert_not_called()

    def test_live_mode_requires_explicit_opt_in(self):
        self.assertFalse(config.LIVE_TRADING_OPT_IN)
        self.assertTrue(config.DRY_RUN)


if __name__ == "__main__":
    unittest.main()
