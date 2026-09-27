import unittest
from unittest.mock import MagicMock, patch

import guardian


class GuardianSafetyTests(unittest.TestCase):
    def test_get_position_returns_unknown_on_api_error(self):
        info = MagicMock()
        info.user_state.side_effect = RuntimeError("api down")
        result = guardian.get_position(info, "example-address")
        self.assertIs(result, guardian.UNKNOWN_POSITION)

    @patch.object(guardian.config, "GUARDIAN_ENABLE_LIVE_ACTIONS", False)
    @patch.object(guardian.config, "LIVE_TRADING_OPT_IN", True)
    @patch.object(guardian.config, "DRY_RUN", False)
    @patch.object(guardian.config, "DEMO_MODE", False)
    @patch.object(guardian.config, "HL_NETWORK_VALID", True)
    def test_force_close_requires_independent_guardian_opt_in(self):
        exchange = MagicMock()
        result = guardian.force_close(exchange, MagicMock(), "example-address", "test")
        self.assertFalse(result)
        exchange.market_close.assert_not_called()

    @patch("guardian.live_actions_allowed", return_value=True)
    @patch("guardian.notify")
    @patch("guardian.get_position")
    def test_force_close_requires_position_to_disappear_after_exchange_close(
        self, get_position, _notify, _allowed
    ):
        exchange = MagicMock()
        open_pos = {"side": "short", "size": 0.1, "entry_price": 100.0, "unrealized_pnl": -1.0}
        get_position.side_effect = [open_pos, open_pos]
        exchange.market_close.return_value = {"status": "ok"}
        result = guardian.force_close(exchange, MagicMock(), "example-address", "test")
        self.assertFalse(result)
        exchange.market_close.assert_called_once_with(guardian.SYMBOL)

    @patch("guardian.live_actions_allowed", return_value=True)
    @patch("guardian.notify")
    @patch("guardian.get_position")
    def test_force_close_succeeds_only_after_verified_flat(
        self, get_position, _notify, _allowed
    ):
        exchange = MagicMock()
        open_pos = {"side": "short", "size": 0.1, "entry_price": 100.0, "unrealized_pnl": -1.0}
        get_position.side_effect = [open_pos, None]
        exchange.market_close.return_value = {"status": "ok"}
        result = guardian.force_close(exchange, MagicMock(), "example-address", "test")
        self.assertTrue(result)
        exchange.market_close.assert_called_once_with(guardian.SYMBOL)

    @patch.object(guardian.config, "GUARDIAN_ENABLE_LIVE_ACTIONS", True)
    @patch.object(guardian.config, "GUARDIAN_LIVE_RUNTIME_OPT_IN", True)
    @patch.object(guardian.config, "LIVE_TRADING_OPT_IN", True)
    @patch.object(guardian.config, "DRY_RUN", False)
    @patch.object(guardian.config, "DEMO_MODE", False)
    @patch.object(guardian.config, "HL_NETWORK_VALID", True)
    @patch("guardian.SecurityManager.get_key", return_value="")
    def test_missing_credentials_returns_cleanly_without_system_exit(self, _get_key):
        self.assertIsNone(guardian.get_exchange_and_info())

    @patch.object(guardian.config, "GUARDIAN_ENABLE_LIVE_ACTIONS", True)
    @patch.object(guardian.config, "GUARDIAN_LIVE_RUNTIME_OPT_IN", False)
    @patch.object(guardian.config, "LIVE_TRADING_OPT_IN", True)
    @patch.object(guardian.config, "DRY_RUN", False)
    @patch.object(guardian.config, "DEMO_MODE", False)
    @patch.object(guardian.config, "HL_NETWORK_VALID", True)
    def test_guardian_environment_opt_ins_still_require_runtime_flag(self):
        self.assertFalse(guardian.live_actions_allowed())


if __name__ == "__main__":
    unittest.main()
