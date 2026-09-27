import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import prospective_collector
from prospective_collector import (
    BOOTSTRAP_MARKER_NAME,
    fetch_okx_liquidation_events,
    normalize_okx_liquidation_detail,
    run_once,
)
from prospective_paper import LedgerPaths


INTERVAL = 300_000


def candle(ts, *, complete=True):
    return {
        "ts": ts,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "complete": complete,
    }


def group(*details):
    return {
        "instType": "SWAP",
        "instId": "ETH-USDT-SWAP",
        "details": list(details),
    }


class OkxLiquidationCollectorTests(unittest.TestCase):
    def test_normalizes_detail_with_stable_raw_hash_and_contract_notional(self):
        detail = {"ts": "1200000", "bkPx": "2000.5", "sz": "3", "side": "sell"}
        event = normalize_okx_liquidation_detail(detail)
        canonical = json.dumps(detail, sort_keys=True, separators=(",", ":"), allow_nan=False)
        expected_hash = hashlib.sha256(canonical.encode()).hexdigest()
        self.assertEqual(set(event), {"event_id", "ts", "price", "notional_usd"})
        self.assertEqual(event["event_id"], "okx:" + expected_hash)
        self.assertEqual(event["ts"], 1_200_000)
        self.assertEqual(event["price"], 2000.5)
        self.assertEqual(event["notional_usd"], 600.15)
        self.assertEqual(event["event_id"], normalize_okx_liquidation_detail(dict(detail))["event_id"])
        with self.assertRaisesRegex(ValueError, "missing sz"):
            normalize_okx_liquidation_detail({"ts": "1", "bkPx": "1"})

    def test_paginates_public_liquidations_with_after_and_deduplicates_boundary(self):
        calls = []
        newer = {"ts": "200", "bkPx": "2000", "sz": "2"}
        older = {"ts": "100", "bkPx": "1900", "sz": "4"}

        def fetcher(url, params):
            calls.append((url, dict(params)))
            pages = {
                None: {"code": "0", "data": [group(newer)]},
                "200": {"code": "0", "data": [group(newer, older)]},
                "100": {"code": "0", "data": []},
            }
            return pages[params.get("after")]

        events, report = fetch_okx_liquidation_events(fetcher=fetcher, limit=1)
        self.assertEqual([call[1].get("after") for call in calls], [None, "200", "100"])
        self.assertTrue(all(call[1]["instType"] == "SWAP" and call[1]["uly"] == "ETH-USDT" for call in calls))
        self.assertEqual([event["ts"] for event in events], [100, 200])
        self.assertEqual(report["request_count"], 3)

    def test_rejects_api_error_and_malformed_public_response(self):
        with self.assertRaisesRegex(ValueError, "code '0'"):
            fetch_okx_liquidation_events(fetcher=lambda *_: {"code": "1", "data": []})
        with self.assertRaisesRegex(ValueError, "JSON array"):
            fetch_okx_liquidation_events(fetcher=lambda *_: {"code": "0", "data": {}})
        with self.assertRaisesRegex(ValueError, "unexpected instId"):
            fetch_okx_liquidation_events(
                fetcher=lambda *_: {"code": "0", "data": [{"instType": "SWAP", "instId": "BTC-USDT-SWAP", "details": []}]}
            )


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.clock = lambda: 1200.0  # latest completed candle is 1,199,999 ms

    def test_bootstrap_keeps_only_latest_three_completed_context_and_no_decisions(self):
        calls = []

        def fetch_candles(**kwargs):
            calls.append(kwargs)
            return [
                candle(299_999), candle(599_999), candle(899_999), candle(1_199_999),
                candle(1_499_999),  # current/future completion; must be ignored
            ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_once(
                state_dir=root / "state", manifest=root / "manifest.json", source="okx/public-test",
                candle_fetcher=fetch_candles, event_fetcher=lambda: ([], {"empty_response": True}), clock=self.clock,
            )
            paths = LedgerPaths.in_state_dir(root / "state")
            persisted = [json.loads(line)["ts"] for line in paths.candles.read_text().splitlines()]
            marker = json.loads((root / "state" / BOOTSTRAP_MARKER_NAME).read_text())
            self.assertEqual(calls, [{"inst_id": "ETH-USDT-SWAP", "bar": "5m", "start_ms": 599_999, "end_ms": 1_199_999}])
            self.assertEqual(persisted, [599_999, 899_999, 1_199_999])
            self.assertEqual(marker["context_candle_timestamps"], persisted)
            self.assertTrue(result["bootstrap"])
            self.assertEqual(result["new_records"]["decisions"], 0)
            self.assertFalse(paths.decisions.exists())

    def test_second_run_uses_injected_clock_and_only_new_candle_generates_decision(self):
        first_calls, second_calls = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_once(
                state_dir=root / "state", manifest=root / "manifest.json", source="okx/public-test",
                candle_fetcher=lambda **kwargs: first_calls.append(kwargs) or [
                    candle(599_999), candle(899_999), candle(1_199_999)
                ], event_fetcher=lambda: [], clock=self.clock,
            )
            result = run_once(
                state_dir=root / "state", manifest=root / "manifest.json", source="okx/public-test",
                candle_fetcher=lambda **kwargs: second_calls.append(kwargs) or [candle(1_499_999)],
                event_fetcher=lambda: [], clock=lambda: 1500.0,
            )
            paths = LedgerPaths.in_state_dir(root / "state")
            decisions = [json.loads(line) for line in paths.decisions.read_text().splitlines()]
            self.assertEqual(second_calls, [{"inst_id": "ETH-USDT-SWAP", "bar": "5m", "start_ms": 1_499_999, "end_ms": 1_499_999}])
            self.assertFalse(result["bootstrap"])
            self.assertEqual(result["new_records"]["candles"], 1)
            self.assertEqual(result["new_records"]["decisions"], 1)
            self.assertEqual([decision["confirmation_ts"] for decision in decisions], [1_499_999])

    def test_api_failure_does_not_create_bootstrap_marker_or_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "code '0'"):
                run_once(
                    state_dir=root / "state", manifest=root / "manifest.json", source="okx/public-test",
                    candle_fetcher=lambda **_: [candle(599_999), candle(899_999), candle(1_199_999)],
                    event_fetcher=lambda: fetch_okx_liquidation_events(fetcher=lambda *_: {"code": "1", "data": []}),
                    clock=self.clock,
                )
            self.assertFalse((root / "state" / BOOTSTRAP_MARKER_NAME).exists())
            self.assertFalse(LedgerPaths.in_state_dir(root / "state").candles.exists())

    def test_run_forever_stops_gracefully_on_keyboard_interrupt(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def stop_after_first_sleep(_interval):
                raise KeyboardInterrupt

            prospective_collector.run_forever(
                state_dir=root / "state", manifest=root / "manifest.json", source="okx/public-test",
                candle_fetcher=lambda **kwargs: calls.append(kwargs) or [
                    candle(599_999), candle(899_999), candle(1_199_999)
                ],
                event_fetcher=lambda: [], clock=self.clock, poll_interval=0.01,
                sleeper=stop_after_first_sleep,
            )
        self.assertEqual(len(calls), 1)

    def test_cli_requires_paths_and_source_and_once_is_safe_with_injected_fetchers(self):
        with self.assertRaises(SystemExit):
            prospective_collector.main(["--once"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = prospective_collector.main(
                    ["--once", "--state-dir", str(root / "state"), "--manifest", str(root / "manifest.json"), "--source", "okx/public-test"],
                    candle_fetcher=lambda **_: [candle(599_999), candle(899_999), candle(1_199_999)],
                    event_fetcher=lambda: [], clock=self.clock,
                )
            self.assertEqual(status, 0)
            self.assertIn("PAPER/SIMULATION ONLY", output.getvalue())
            self.assertEqual(json.loads((root / "manifest.json").read_text())["dataset_source"], "okx/public-test")


if __name__ == "__main__":
    unittest.main()
