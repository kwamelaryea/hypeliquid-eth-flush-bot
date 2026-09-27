import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import hyperliquid_bot


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(hyperliquid_bot.app)

    def test_health_is_public_and_has_security_headers(self):
        with patch.dict("os.environ", {}, clear=True):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        csp = response.headers["content-security-policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_root_dashboard_is_public_html(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn('/static/dashboard.js', response.text)
        self.assertNotIn('<script>', response.text)

    def test_public_status_is_synthetic_and_excludes_account_snapshot(self):
        state = hyperliquid_bot.bot_state
        state.address = "private-wallet"
        state.balance = 321.45
        state.position = "long"
        state.pnl_pct = 8.25
        state.perps_positions = [{"coin": "ETH", "size": 2}]
        state.spot_positions = [{"coin": "USDC", "total": 321.45}]
        state.logs.clear()
        state.logs.append({"time": "12:00", "msg": "private operation"})

        response = self.client.get("/api/public-status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["demo_data"])
        self.assertEqual(data["execution_mode"], "DEMO")
        self.assertEqual(data["position"], "none")
        self.assertEqual(data["pnl"], 0.0)
        self.assertEqual(data["perps_positions"], [])
        self.assertEqual(data["spot_positions"], [])
        self.assertNotIn("address", data)
        self.assertNotIn("private operation", str(data))
        self.assertNotEqual(data["balance"], 321.45)

    @patch("requests.post")
    def test_public_pnl_history_never_fetches_account_data(self, post):
        hyperliquid_bot.bot_state.address = "private-wallet"

        response = self.client.get("/api/public-pnl-history")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"demo_data": True, "data": [], "total_pnl": 0.0})
        post.assert_not_called()

    def test_account_status_requires_operational_api_token(self):
        with patch.dict("os.environ", {"OPERATIONAL_API_TOKEN": "secret"}, clear=True):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_account_status_is_unavailable_when_token_not_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 503)

    def test_status_accepts_bearer_token(self):
        with patch.dict("os.environ", {"OPERATIONAL_API_TOKEN": "secret"}, clear=True):
            response = self.client.get(
                "/api/status", headers={"Authorization": "Bearer secret"}
            )
        self.assertEqual(response.status_code, 200)

    def test_api_schema_is_not_exposed(self):
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 404)


if __name__ == "__main__":
    unittest.main()
