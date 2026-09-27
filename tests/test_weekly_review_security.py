import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import weekly_review_agent


class WeeklyReviewSecurityTests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_agent_main_requires_local_private_opt_in(self):
        with self.assertRaises(SystemExit):
            weekly_review_agent.main()

    @patch.dict("os.environ", {"OPERATIONAL_API_TOKEN": "secret"}, clear=False)
    @patch.object(weekly_review_agent, "REVIEW_LOG_URL", "https://example.invalid/api/log")
    @patch("weekly_review_agent.urllib.request.urlopen")
    def test_fetch_fly_data_sends_bearer_token(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({"trades": [], "lines": []}).encode()
        response.__enter__.return_value = response
        urlopen.return_value = response

        weekly_review_agent._fetch_fly_data()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    @patch("weekly_review_agent._fetch_fly_data")
    def test_parse_trades_preserves_entry_strategy_and_separates_exit_reason(self, fetch_data):
        # Keep the fixture inside the production rolling review window rather
        # than depending on a calendar date that eventually becomes stale.
        opened = datetime.now(timezone.utc) - timedelta(days=1, hours=2)
        closed = opened + timedelta(hours=2)
        fetch_data.return_value = (
            [
                f"{opened.strftime('%Y-%m-%dT%H:%M:%SZ')},OPEN_LONG,LONG,2400.00,+0.0000,25.00,127.00,flush",
                f"{closed.strftime('%Y-%m-%dT%H:%M:%SZ')},CLOSE_TP,LONG,2440.00,+1.6667,0.00,127.40,rsi-exit",
            ],
            "",
        )

        trades = weekly_review_agent.parse_trades("")

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["strategy"], "flush")
        self.assertEqual(trades[0]["exit_reason"], "CLOSE_TP")


if __name__ == "__main__":
    unittest.main()
