import unittest
from unittest.mock import patch

import dex_trader


class DexLiveSafetyTests(unittest.TestCase):
    @patch.object(dex_trader.config, "DRY_RUN", False)
    @patch.object(dex_trader.config, "DEX_LIVE_RUNTIME_OPT_IN", True)
    @patch.object(dex_trader.config, "DEX_LIVE_TRADING_OPT_IN", False)
    @patch.object(dex_trader.config, "DEX_NETWORK_VALID", True)
    @patch.object(dex_trader.config, "WALLET_PRIVATE_KEY", "example")
    def test_hyperliquid_mode_cannot_implicitly_enable_dex_actions(self):
        self.assertFalse(dex_trader.dex_live_actions_allowed())

    @patch.object(dex_trader.config, "DRY_RUN", False)
    @patch.object(dex_trader.config, "DEX_LIVE_RUNTIME_OPT_IN", True)
    @patch.object(dex_trader.config, "DEX_LIVE_TRADING_OPT_IN", True)
    @patch.object(dex_trader.config, "DEX_NETWORK_VALID", False)
    @patch.object(dex_trader.config, "WALLET_PRIVATE_KEY", "example")
    def test_invalid_dex_network_blocks_live_actions(self):
        self.assertFalse(dex_trader.dex_live_actions_allowed())


if __name__ == "__main__":
    unittest.main()
