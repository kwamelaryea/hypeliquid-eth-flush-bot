import unittest

from hyperliquid_bot import BotState
from websocket_manager import LiquidationEvent


class LiquidationTelemetryTests(unittest.TestCase):
    def test_liquidation_telemetry_is_operational_only(self):
        state = BotState()
        event = LiquidationEvent(
            exchange="okx", symbol="ETH", price=1912.5, size_usd=12345.678,
            side="long", timestamp=1722012345.0,
        )
        state.record_ws_liquidation(event, buffer_count=7)

        private_snapshot = state.get_snapshot()
        public_snapshot = state.get_public_snapshot()

        self.assertEqual(private_snapshot["ws_liquidation_count"], 1)
        self.assertEqual(private_snapshot["liquidation_buffer_count"], 7)
        self.assertNotIn("ws_liquidation_count", public_snapshot)
        self.assertNotIn("ws_last_liquidation", public_snapshot)
        self.assertTrue(public_snapshot["demo_data"])

    def test_stream_status_is_operational_only(self):
        state = BotState()
        state.record_ws_stream_status(
            "okx", {"status": "subscribed", "channel": "liquidation-orders"}
        )

        self.assertEqual(state.get_snapshot()["ws_streams"]["okx"]["status"], "subscribed")
        self.assertNotIn("ws_streams", state.get_public_snapshot())


if __name__ == "__main__":
    unittest.main()
