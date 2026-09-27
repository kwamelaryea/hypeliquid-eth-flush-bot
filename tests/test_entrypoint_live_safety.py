import sys
import unittest
from unittest.mock import MagicMock, patch

import bot
import config
import config_dex
import dex_trader
import run
import run_dex


class LegacyLiveSafetyTests(unittest.TestCase):
    def setUp(self):
        original = (config.DRY_RUN, config.EXCHANGE_LIVE_RUNTIME_OPT_IN)
        self.addCleanup(setattr, config, "DRY_RUN", original[0])
        self.addCleanup(setattr, config, "EXCHANGE_LIVE_RUNTIME_OPT_IN", original[1])

    def test_environment_opt_in_without_runtime_opt_in_cannot_order(self):
        exchange = MagicMock()
        instance = bot.RSITradingBot.__new__(bot.RSITradingBot)
        instance.exchange = exchange
        instance.logger = MagicMock()
        instance.position = None
        instance.last_trade_price = None
        with patch.multiple(
            config,
            DRY_RUN=False,
            EXCHANGE_LIVE_TRADING_OPT_IN=True,
            EXCHANGE_LIVE_RUNTIME_OPT_IN=False,
            API_KEY="configured",
            API_SECRET="configured",
        ):
            self.assertTrue(instance.execute_buy(100.0))
        exchange.create_market_buy_order.assert_not_called()

    def test_live_cli_rejects_missing_environment_opt_in(self):
        with patch.object(sys, "argv", ["run.py", "--live", "--status"]), \
             patch.object(config, "EXCHANGE_LIVE_TRADING_OPT_IN", False), \
             self.assertRaises(SystemExit) as raised:
            run.main()
        self.assertEqual(raised.exception.code, 2)

    def test_both_legacy_opt_ins_are_required_and_set_for_live_status(self):
        with patch.object(sys, "argv", ["run.py", "--live", "--status"]), \
             patch.object(config, "EXCHANGE_LIVE_TRADING_OPT_IN", True), \
             patch.object(config, "API_KEY", "configured"), \
             patch.object(config, "API_SECRET", "configured"), \
             patch("builtins.input", return_value="YES"), \
             patch.object(run, "show_status") as show_status:
            run.main()
            self.assertFalse(config.DRY_RUN)
            self.assertTrue(config.EXCHANGE_LIVE_RUNTIME_OPT_IN)
            show_status.assert_called_once()


class DexEntrypointSafetyTests(unittest.TestCase):
    def setUp(self):
        original = (config_dex.DRY_RUN, config_dex.DEX_LIVE_RUNTIME_OPT_IN)
        self.addCleanup(setattr, config_dex, "DRY_RUN", original[0])
        self.addCleanup(setattr, config_dex, "DEX_LIVE_RUNTIME_OPT_IN", original[1])

    def test_environment_opt_in_without_runtime_opt_in_cannot_swap(self):
        with patch.multiple(
            dex_trader.config,
            DRY_RUN=False,
            DEX_LIVE_TRADING_OPT_IN=True,
            DEX_LIVE_RUNTIME_OPT_IN=False,
            DEX_NETWORK_VALID=True,
            WALLET_PRIVATE_KEY="configured",
        ):
            self.assertFalse(dex_trader.dex_live_actions_allowed())

    def test_dex_live_cli_rejects_missing_environment_opt_in(self):
        with patch.object(sys, "argv", ["run_dex.py", "--live", "--status"]), \
             patch.object(config_dex, "DEX_LIVE_TRADING_OPT_IN", False), \
             self.assertRaises(SystemExit) as raised:
            run_dex.main()
        self.assertEqual(raised.exception.code, 2)

    def test_dex_live_requires_both_opt_ins(self):
        with patch.object(sys, "argv", ["run_dex.py", "--live", "--status"]), \
             patch.object(config_dex, "DEX_LIVE_TRADING_OPT_IN", True), \
             patch.object(config_dex, "DEX_NETWORK_VALID", True), \
             patch.object(config_dex, "WALLET_PRIVATE_KEY", "configured"), \
             patch("builtins.input", return_value="YES"), \
             patch.object(run_dex, "show_status") as show_status:
            run_dex.main()
            self.assertFalse(config_dex.DRY_RUN)
            self.assertTrue(config_dex.DEX_LIVE_RUNTIME_OPT_IN)
            show_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
