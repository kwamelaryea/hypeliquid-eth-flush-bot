import unittest

import config_dex as config


class StrategyConfigTests(unittest.TestCase):
    def test_flush_short_is_disabled_for_recovery_baseline(self):
        self.assertFalse(config.USE_FLUSH_SHORT)

    def test_long_only_mode_is_explicit(self):
        self.assertTrue(config.LONG_ONLY_MODE)

    def test_live_execution_requires_explicit_opt_in(self):
        self.assertFalse(config.LIVE_TRADING_OPT_IN)
        self.assertTrue(config.DRY_RUN)
        self.assertTrue(config.PAUSE_NEW_ENTRIES)


if __name__ == "__main__":
    unittest.main()
