import unittest
from unittest.mock import MagicMock, patch

import hyperliquid_bot
from hyperliquid_bot import HyperliquidFlushBot


class ChaseOrderSafetyTests(unittest.TestCase):
    def setUp(self):
        pause_patch = patch.object(hyperliquid_bot.config, "PAUSE_NEW_ENTRIES", False)
        pause_patch.start()
        self.addCleanup(pause_patch.stop)
        for name, value in (("LIVE_TRADING_OPT_IN", True), ("HL_LIVE_RUNTIME_OPT_IN", True)):
            live_patch = patch.object(hyperliquid_bot.config, name, value)
            live_patch.start()
            self.addCleanup(live_patch.stop)

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch.object(hyperliquid_bot.config, "HYPERLIQUID_PRIVATE_KEY", "0xabc")
    @patch.object(hyperliquid_bot.config, "HYPERLIQUID_ADDRESS", "0xConfigured")
    @patch("hyperliquid_bot.Info")
    @patch("hyperliquid_bot.Exchange")
    @patch("eth_account.Account.from_key")
    def test_startup_aborts_when_signer_and_configured_account_differ(self, from_key, _exchange, _info):
        from_key.return_value = MagicMock(address="0xSigner")

        with self.assertRaises(RuntimeError):
            HyperliquidFlushBot()

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
        bot.close_position = MagicMock(return_value=True)
        bot._log_trade = MagicMock()
        bot.notify = MagicMock()
        bot._reset_trailing_state = MagicMock()
        bot.trailing_activated = False
        bot.trailing_floor = None
        bot.position_open_time = None
        bot.last_trade_price = None
        bot.long_close_history = []
        bot.long_entry_price_tracked = None
        bot.active_regime = "range"
        bot.position_strategy = None
        bot.position_stop_loss_pct = None
        bot.position_take_profit_pct = None
        bot.equity_for_sizing = 1000.0
        bot._round_price = lambda price: round(price, 1)
        return bot

    def test_trend_follow_stop_survives_active_regime_drift(self):
        bot = self.make_bot()
        bot.get_position = MagicMock(return_value="long")
        bot.get_position_details = MagicMock(return_value=[{
            "coin": "ETH",
            "side": "long",
            "entry_price": 100.0,
            "size": 0.1,
        }])
        bot.get_current_price = MagicMock(return_value=97.4)  # -2.6%
        bot.info.candles_snapshot.return_value = []
        bot.active_regime = "range"  # drifted after entry
        bot.position_strategy = "trend-follow"
        bot.position_stop_loss_pct = 2.5
        bot.position_take_profit_pct = 6.0

        result = bot._check_tp_sl()

        self.assertTrue(result)
        bot.close_position.assert_called_once()
        bot._log_trade.assert_called_once()
        self.assertEqual(bot._log_trade.call_args.kwargs["strategy"], "trend-follow")

    @patch.object(hyperliquid_bot.config, "PAUSE_NEW_ENTRIES", True, create=True)
    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_open_position_respects_entries_only_pause(self):
        bot = self.make_bot()
        bot.leverage = 1
        bot.equity_for_sizing = 1000.0
        bot._calculate_limit_price = MagicMock(return_value=100.0)

        result = bot.open_position(side="long", amount_usd=10.0, price=100.0)

        self.assertFalse(result)
        bot.exchange.update_leverage.assert_not_called()
        bot.exchange.order.assert_not_called()
        bot.exchange.market_open.assert_not_called()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_open_position_aborts_when_leverage_cannot_be_confirmed(self):
        bot = self.make_bot()
        bot.leverage = 1
        bot._calculate_limit_price = MagicMock(return_value=100.0)
        bot.exchange.update_leverage.side_effect = RuntimeError("leverage API down")

        result = bot.open_position(side="long", amount_usd=10.0, price=100.0)

        self.assertFalse(result)
        bot.exchange.order.assert_not_called()
        bot.exchange.market_open.assert_not_called()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_close_position_requires_exchange_position_to_disappear(self):
        bot = self.make_bot()
        bot.close_position = HyperliquidFlushBot.close_position.__get__(bot, HyperliquidFlushBot)
        bot.get_position = MagicMock(side_effect=["short", "short"])
        bot.exchange.market_close.return_value = {"status": "ok"}

        result = bot.close_position()

        self.assertFalse(result)
        bot.exchange.market_close.assert_called_once_with("ETH")

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_close_position_succeeds_after_verified_flat(self):
        bot = self.make_bot()
        bot.close_position = HyperliquidFlushBot.close_position.__get__(bot, HyperliquidFlushBot)
        bot.get_position = MagicMock(side_effect=["short", "none"])
        bot.exchange.market_close.return_value = {"status": "ok"}

        result = bot.close_position()

        self.assertTrue(result)
        bot.exchange.market_close.assert_called_once_with("ETH")

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_open_position_records_exchange_average_entry_after_chase_fill(self):
        bot = self.make_bot()
        bot.leverage = 1
        bot._calculate_limit_price = MagicMock(return_value=99.9)
        bot.active_regime = "uptrend"
        bot.exchange.update_leverage.return_value = {"status": "ok"}
        bot.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}},
        }
        bot._chase_order = MagicMock(return_value=True)
        bot.get_position_details = MagicMock(return_value=[{
            "coin": "ETH",
            "side": "long",
            "entry_price": 101.2,
            "size": 0.1,
        }])

        result = bot.open_position(side="long", amount_usd=10.0, price=100.0)

        self.assertTrue(result)
        self.assertEqual(bot.last_trade_price, 101.2)
        self.assertEqual(bot.exchange.order.call_count, 2)
        stop_call = bot.exchange.order.call_args_list[1]
        self.assertEqual(stop_call.args[:4], ("ETH", False, 0.1, 98.7))
        self.assertTrue(stop_call.kwargs["reduce_only"])
        self.assertEqual(
            stop_call.kwargs["order_type"],
            {"trigger": {"triggerPx": 98.7, "isMarket": True, "tpsl": "sl"}},
        )

    @patch("hyperliquid_bot.time.sleep", return_value=None)
    def test_partial_fill_reprices_only_remaining_quantity(self, _sleep):
        bot = self.make_bot()
        bot.info.open_orders.side_effect = [
            [{"coin": "ETH", "oid": 1}],
            [],  # cancelled order is confirmed absent before repricing
            [],  # final replacement is also absent after cancellation
        ]
        bot.get_position_details.side_effect = [
            [{"coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.4}],
            [{"coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.4}],
        ]
        bot.exchange.cancel.return_value = {"status": "ok"}
        bot.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 2}}]}},
        }

        result = bot._chase_order(order_id=1, side="long", size=1.0, initial_price=100.0, max_attempts=1)

        self.assertFalse(result)
        self.assertEqual(bot.exchange.order.call_args.args[2], 0.6)

    @patch("hyperliquid_bot.time.sleep", return_value=None)
    def test_cancel_race_aborts_without_resubmitting_full_size(self, _sleep):
        bot = self.make_bot()
        bot.info.open_orders.return_value = [{"coin": "ETH", "oid": 1}]
        bot.get_position_details.return_value = [{
            "coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.4,
        }]
        bot.exchange.cancel.return_value = {"status": "error", "response": "cancel rejected"}

        result = bot._chase_order(order_id=1, side="long", size=1.0, initial_price=100.0, max_attempts=1)

        self.assertFalse(result)
        bot.exchange.order.assert_not_called()
        bot.get_position_details.assert_called()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_cancel_all_open_orders_returns_false_when_cancel_fails(self):
        bot = self.make_bot()
        bot.info.open_orders.return_value = [{"coin": "ETH", "oid": 10}]
        bot.exchange.cancel.return_value = {"status": "error"}

        self.assertFalse(bot._cancel_all_open_orders())

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_cancel_all_open_orders_returns_false_when_order_remains_stale(self):
        bot = self.make_bot()
        bot.info.open_orders.side_effect = [
            [{"coin": "ETH", "oid": 10}],
            [{"coin": "ETH", "oid": 10}],
        ]
        bot.exchange.cancel.return_value = {"status": "ok"}

        self.assertFalse(bot._cancel_all_open_orders())

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_failed_chase_protects_reconciled_partial_position(self):
        bot = self.make_bot()
        bot.leverage = 1
        bot._calculate_limit_price = MagicMock(return_value=99.9)
        bot.exchange.update_leverage.return_value = {"status": "ok"}
        bot.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        bot._chase_order = MagicMock(return_value=False)
        bot._place_protective_stop = MagicMock(return_value=True)
        bot.get_position_details.return_value = [{
            "coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.4,
        }]

        self.assertFalse(bot.open_position("long", 10.0, 100.0))
        bot._place_protective_stop.assert_called_once_with("long")

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_software_stop_uses_same_frozen_authoritative_distance(self):
        bot = self.make_bot()
        bot.get_position = MagicMock(return_value="long")
        bot.get_position_details = MagicMock(return_value=[{
            "coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.1,
        }])
        bot.get_current_price = MagicMock(return_value=99.4)
        bot.info.candles_snapshot.return_value = []
        bot.position_strategy = "flush"
        bot.position_stop_loss_pct = 0.5
        bot.position_take_profit_pct = 12.0

        result = bot._check_tp_sl()

        self.assertTrue(result)
        bot.close_position.assert_called_once()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch("hyperliquid_bot.compute_position_size", return_value=0.1)
    def test_authoritative_stop_is_used_for_sizing_metadata_and_exchange_stop(self, sizing):
        bot = self.make_bot()
        bot.leverage = 1
        bot._calculate_limit_price = MagicMock(return_value=100.0)
        bot.exchange.update_leverage.return_value = {"status": "ok"}
        bot.exchange.order.side_effect = [
            {
                "status": "ok",
                "response": {"data": {"statuses": [{"filled": {"totalSz": "0.1"}}]}},
            },
            {
                "status": "ok",
                "response": {"data": {"statuses": [{"resting": {"oid": 456}}]}},
            },
        ]
        bot.get_position_details.return_value = [{
            "coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.1,
        }]

        result = bot.open_position(
            "long", 10.0, 100.0, equity=1000.0,
            stop_price=99.5, stop_loss_pct=0.5,
        )

        self.assertTrue(result)
        self.assertEqual(sizing.call_args.kwargs["stop_price"], 99.5)
        self.assertEqual(bot.position_stop_loss_pct, 0.5)
        stop_call = bot.exchange.order.call_args_list[-1]
        self.assertEqual(stop_call.args[:4], ("ETH", False, 0.1, 99.5))

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch("hyperliquid_bot.time.sleep", return_value=None)
    def test_protective_stop_retries_after_transient_exchange_failure(self, _sleep):
        bot = self.make_bot()
        bot.position_stop_loss_pct = 0.5
        bot.get_position_details.return_value = [{
            "coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.1,
        }]
        bot.exchange.order.side_effect = [
            RuntimeError("temporary"),
            {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {"oid": 123}},
            ]}}},
        ]

        result = bot._place_protective_stop("long")

        self.assertTrue(result)
        self.assertEqual(bot.exchange.order.call_count, 2)

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch("hyperliquid_bot.time.sleep", return_value=None)
    def test_protective_stop_retries_empty_and_malformed_ok_statuses(self, _sleep):
        bot = self.make_bot()
        bot.position_stop_loss_pct = 0.5
        bot.get_position_details.return_value = [{
            "coin": "ETH", "side": "long", "entry_price": 100.0, "size": 0.1,
        }]
        bot.exchange.order.side_effect = [
            {"status": "ok", "response": {"data": {"statuses": []}}},
            {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {}},
            ]}}},
            {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {"oid": 123}},
            ]}}},
        ]

        result = bot._place_protective_stop("long")

        self.assertTrue(result)
        self.assertEqual(bot.exchange.order.call_count, 3)

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_close_position_treats_unknown_position_state_as_failure(self):
        bot = self.make_bot()
        bot._position_state_known = False
        bot.get_position.return_value = "none"

        result = HyperliquidFlushBot.close_position(bot)

        self.assertFalse(result)
        bot.exchange.market_close.assert_not_called()

    def test_analyze_and_trade_denies_unknown_position_state_before_entry(self):
        bot = self.make_bot()
        bot.get_current_price.return_value = 100.0
        bot._update_macro_regime = MagicMock()
        bot._update_vpin = MagicMock()
        bot.get_funding_rate = MagicMock(return_value=0.0)
        bot.cached_oi = 0.0
        bot.info.candles_snapshot.return_value = []
        bot.get_balances = MagicMock(return_value={"USDC": 1000.0, "available": 1000.0})
        bot._log_denial = MagicMock()
        bot.get_position = MagicMock(side_effect=lambda: setattr(bot, "_position_state_known", False) or "none")
        bot.open_position = MagicMock(return_value=True)

        result = bot.analyze_and_trade()

        self.assertEqual(result["action_taken"], "HOLD_POSITION_STATE_UNKNOWN")
        bot.open_position.assert_not_called()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_open_position_rejects_late_unknown_position_state_before_order(self):
        bot = self.make_bot()
        bot.leverage = 1
        bot._position_state_known = True

        # This models the later reconciliation read in analyze_and_trade failing
        # after the initial guarded read has already passed.
        def late_position_read_fails():
            bot._position_state_known = False
            return []

        bot.get_position_details.side_effect = late_position_read_fails
        bot.get_position_details()

        result = bot.open_position("long", amount_usd=10.0, price=100.0)

        self.assertFalse(result)
        bot.exchange.order.assert_not_called()

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch("hyperliquid_bot.time.sleep", return_value=None)
    def test_failed_replacement_cancels_residual_entry_order(self, _sleep):
        bot = self.make_bot()
        bot.info.open_orders.side_effect = [
            [{"coin": "ETH", "oid": 1, "reduceOnly": False}],
            [],
            [{"coin": "ETH", "oid": 2, "reduceOnly": False}],
            [],
        ]
        bot.get_position_details.return_value = []
        bot.exchange.cancel.return_value = {"status": "ok"}
        bot.exchange.order.return_value = {"status": "error", "response": "rejected"}

        result = bot._chase_order(order_id=1, side="long", size=1.0, initial_price=100.0, max_attempts=1)

        self.assertFalse(result)
        self.assertEqual([call.args[1] for call in bot.exchange.cancel.call_args_list], [1, 2])

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    def test_close_position_cancels_entries_but_preserves_protective_stops(self):
        bot = self.make_bot()
        bot.close_position = HyperliquidFlushBot.close_position.__get__(bot, HyperliquidFlushBot)
        bot.get_position = MagicMock(side_effect=["long", "none"])
        bot.info.open_orders.side_effect = [
            [
                {"coin": "ETH", "oid": 10, "reduceOnly": False},
                {"coin": "ETH", "oid": 11, "reduceOnly": True},
            ],
            [{"coin": "ETH", "oid": 11, "reduceOnly": True}],
            [{"coin": "ETH", "oid": 11, "reduceOnly": True}],
            [],
        ]
        bot.exchange.cancel.return_value = {"status": "ok"}
        bot.exchange.market_close.return_value = {"status": "ok"}

        result = bot.close_position()

        self.assertTrue(result)
        self.assertEqual([call.args[1] for call in bot.exchange.cancel.call_args_list], [10])

    @patch.object(hyperliquid_bot.config, "DRY_RUN", False)
    @patch("hyperliquid_bot.compute_position_size", return_value=0.1)
    def test_conflicting_stop_inputs_are_rejected(self, _sizing):
        bot = self.make_bot()
        bot.leverage = 1
        bot._calculate_limit_price = MagicMock(return_value=100.0)
        bot.exchange.update_leverage.return_value = {"status": "ok"}

        result = bot.open_position(
            "long", 10.0, 100.0, equity=1000.0,
            stop_price=98.0, stop_loss_pct=0.5,
        )

        self.assertFalse(result)
        bot.exchange.order.assert_not_called()

    @patch("hyperliquid_bot.time.sleep", return_value=None)
    def test_chase_timeout_aborts_after_final_cancel_without_market_fallback(self, _sleep):
        bot = self.make_bot()

        # Each chase attempt sees the current order still resting, cancels it,
        # and submits a replacement that also rests. Exhaustion must cancel the
        # final maker order and abort rather than silently becoming a taker.
        bot.info.open_orders.side_effect = [
            [{"coin": "ETH", "oid": 1}], [],
            [{"coin": "ETH", "oid": 2}], [],
            [{"coin": "ETH", "oid": 3}], [],
            [{"coin": "ETH", "oid": 4}], [],  # final cancellation confirmation
        ]
        bot.exchange.cancel.return_value = {"status": "ok"}
        bot.exchange.order.side_effect = [
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 2}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 3}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 4}}]}}},
        ]
        bot.exchange.market_open.return_value = {"status": "ok"}

        result = bot._chase_order(order_id=1, side="long", size=0.01, initial_price=100.0, max_attempts=3)

        self.assertFalse(result)
        cancelled_oids = [call.args[1] for call in bot.exchange.cancel.call_args_list]
        self.assertEqual(cancelled_oids, [1, 2, 3, 4])
        bot.exchange.market_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
