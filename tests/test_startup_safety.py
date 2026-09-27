import unittest
from unittest.mock import MagicMock, patch

import hyperliquid_bot
from hyperliquid_bot import HyperliquidFlushBot


class StartupSafetyTests(unittest.TestCase):
    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch.object(hyperliquid_bot.config, "LIVE_TRADING_OPT_IN", True, create=True)
    @patch.object(hyperliquid_bot.config, "HYPERLIQUID_PRIVATE_KEY", "", create=True)
    @patch("hyperliquid_bot.Info")
    def test_live_opt_in_without_private_key_fails_closed(self, _info):
        with self.assertRaises(RuntimeError):
            HyperliquidFlushBot()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch.object(hyperliquid_bot.config, "LIVE_TRADING_OPT_IN", True, create=True)
    @patch.object(hyperliquid_bot.config, "HL_LIVE_RUNTIME_OPT_IN", False, create=True)
    @patch.object(hyperliquid_bot.config, "HYPERLIQUID_PRIVATE_KEY", "configured", create=True)
    def test_live_environment_opt_in_without_runtime_opt_in_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "--live"):
            HyperliquidFlushBot()

    @patch.object(hyperliquid_bot.config, "DEMO_MODE", False)
    @patch.object(hyperliquid_bot.config, "LIVE_TRADING_OPT_IN", True)
    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch.object(hyperliquid_bot.config, "HL_LIVE_RUNTIME_OPT_IN", True)
    @patch("hyperliquid_bot.HyperliquidFlushBot")
    def test_environment_opt_in_alone_is_paper_only_at_main_entrypoint(self, bot_cls):
        bot_cls.return_value.denial_log = []

        self.assertEqual(hyperliquid_bot.main([]), 0)

        self.assertTrue(hyperliquid_bot.config.DRY_RUN)
        self.assertFalse(hyperliquid_bot.config.HL_LIVE_RUNTIME_OPT_IN)
        bot_cls.return_value.run.assert_called_once()

    @patch("hyperliquid_bot.uvicorn.run")
    @patch("hyperliquid_bot.threading.Thread")
    def test_run_aborts_threads_when_startup_validation_fails(self, thread_cls, uvicorn_run):
        bot = HyperliquidFlushBot.__new__(HyperliquidFlushBot)
        bot.symbol = "ETH"
        bot.leverage = 1
        bot.log = MagicMock()
        bot.logger = MagicMock()
        bot._recover_open_position_log = MagicMock()
        bot.run_startup_validation = MagicMock(return_value=False)
        bot.trade_loop = MagicMock()
        bot.start_websocket_loop = MagicMock()
        bot.daily_review_loop = MagicMock()

        bot.run()

        thread_cls.assert_not_called()
        uvicorn_run.assert_not_called()
        bot.log.assert_any_call("Startup validation failed — trading/API threads not started", "error")


if __name__ == "__main__":
    unittest.main()
