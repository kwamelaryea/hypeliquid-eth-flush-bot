import unittest

from websocket_manager import LiquidationEvent, WebSocketManager


class OkxLiquidationFeedTests(unittest.TestCase):
    def setUp(self):
        self.manager = WebSocketManager(logger=None)

    def test_parses_okx_eth_swap_liquidation_payload(self):
        payload = {
            "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "ETH-USDT-SWAP",
                    "details": [
                        {"bkPx": "1865.5", "sz": "42", "side": "sell", "ts": "1722012345000"}
                    ],
                }
            ],
        }

        events = self.manager._parse_okx_liquidation_message(payload)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, LiquidationEvent)
        self.assertEqual(event.exchange, "okx")
        self.assertEqual(event.symbol, "ETH")
        self.assertEqual(event.price, 1865.5)
        self.assertEqual(event.size_usd, 7835.1)
        self.assertEqual(event.side, "long")
        self.assertEqual(event.timestamp, 1722012345.0)

    def test_ignores_non_eth_okx_liquidations(self):
        payload = {
            "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "BTC-USDT-SWAP",
                    "details": [
                        {"bkPx": "65000", "sz": "1", "side": "sell", "ts": "1722012345000"}
                    ],
                }
            ],
        }

        events = self.manager._parse_okx_liquidation_message(payload)

        self.assertEqual(events, [])

    def test_okx_feed_is_started_with_other_market_streams(self):
        self.assertIn(self.manager._connect_okx, self.manager._market_streams())


if __name__ == "__main__":
    unittest.main()
