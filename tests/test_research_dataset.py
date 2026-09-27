import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from research_dataset import (
    fetch_hyperliquid_candles,
    fetch_okx_candles,
    build_research_report,
    normalize_okx_candle_rows,
    normalize_okx_records,
    prepare_normalized_events,
    validate_candle_coverage,
    main,
)


class ResearchDatasetTests(unittest.TestCase):
    def test_normalizes_okx_multiplier_and_rejects_malformed_records(self):
        records = [
            {"ts": "1722012300000", "bkPx": "2000", "sz": "3", "side": "sell"},
            {"ts": "bad", "bkPx": "2000", "sz": "3"},
            {"ts": 1722012400000, "bkPx": 0, "sz": 3},
            {"ts": 1722012500000, "bkPx": 2000, "sz": -3},
        ]

        events, report = normalize_okx_records(records)

        self.assertEqual(events, [{
            "ts": 1722012300000,
            "price": 2000.0,
            "notional_usd": 600.0,
            "source": "okx",
            "source_record_hash": hashlib.sha256(
                json.dumps(records[0], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }])
        self.assertEqual(report["raw_event_line_count"], 4)
        self.assertEqual(report["rejected_count"], 3)
        self.assertEqual(report["total_notional_usd"], 600.0)
        self.assertEqual(report["time_range"], {"start_ms": 1722012300000, "end_ms": 1722012300000})

    def test_deduplicates_deterministically(self):
        first = {"ts": 2000, "bkPx": "2100", "sz": "2"}
        duplicate = dict(first)
        second = {"ts": 1000, "bkPx": "2200", "sz": "1"}

        events, report = prepare_normalized_events([first, duplicate, second])

        self.assertEqual([event["ts"] for event in events], [1000, 2000])
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["raw_event_hash_count"], 2)
        self.assertEqual(report["total_notional_usd"], 640.0)

    def test_validate_candle_coverage_uses_completion_timestamps_and_detects_gaps(self):
        candles = [
            {"t": 0, "T": 299999, "o": "1", "h": "2", "l": "1", "c": "1.5"},
            {"t": 600000, "T": 899999, "o": "1.5", "h": "3", "l": "1", "c": "2"},
        ]

        report = validate_candle_coverage(candles, "5m", requested_start_ms=0, requested_end_ms=899999)

        self.assertEqual(report["completion_time_range"], {"start_ms": 299999, "end_ms": 899999})
        self.assertEqual(report["gap_count"], 1)
        self.assertEqual(report["missing_candle_count"], 1)
        self.assertTrue(report["coverage_sufficient"])
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            validate_candle_coverage([candles[0], dict(candles[0])], "5m")
        with self.assertRaisesRegex(ValueError, "missing required field 'c'"):
            validate_candle_coverage([{"t": 0, "T": 1, "o": 1, "h": 1, "l": 1}], "5m")

    def test_fetches_candles_in_chunks_and_validates_response_shape(self):
        calls = []

        def fetcher(url, payload):
            calls.append((url, payload))
            start = payload["req"]["startTime"]
            end = payload["req"]["endTime"]
            return [{"t": start, "T": end, "o": "1", "h": "2", "l": "1", "c": "1.5"}]

        candles, report = fetch_hyperliquid_candles(
            "ETH", "5m", 0, 900000, fetcher=fetcher, chunk_duration_ms=300000
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual([call[1]["req"]["startTime"] for call in calls], [0, 300000, 600000])
        self.assertEqual(calls[0][1]["type"], "candleSnapshot")
        self.assertEqual(candles[-1]["T"], 900000)
        self.assertEqual(report["api_url"], "https://api.hyperliquid.xyz/info")
        self.assertEqual(report["requested_range"], {"start_ms": 0, "end_ms": 900000})

        with self.assertRaisesRegex(ValueError, "JSON array"):
            fetch_hyperliquid_candles("ETH", "5m", 0, 300000, fetcher=lambda *_: {"bad": True})

    def test_normalizes_okx_candle_rows_using_completion_timestamps(self):
        candles = normalize_okx_candle_rows([["0", "1", "2", "0.5", "1.5", "10"]], "5m")

        self.assertEqual(candles, [{"ts": 299999, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}])

    def test_fetches_okx_pages_backward_deduplicates_and_validates_coverage(self):
        calls = []

        def fetcher(url, params):
            calls.append((url, params))
            pages = {
                "900000": [
                    ["600000", "3", "4", "2", "3.5"],
                    ["300000", "2", "3", "1", "2.5"],
                    ["300000", "9", "10", "8", "9.5"],
                ],
                "300000": [["0", "1", "2", "0.5", "1.5"]],
            }
            return {"code": "0", "msg": "", "data": pages[params["after"]]}

        candles, report = fetch_okx_candles(start_ms=0, end_ms=899999, fetcher=fetcher)

        self.assertEqual([call[1]["after"] for call in calls], ["900000", "300000"])
        self.assertTrue(all(call[1]["limit"] == "100" for call in calls))
        self.assertEqual([candle["ts"] for candle in candles], [299999, 599999, 899999])
        self.assertEqual(candles[1]["open"], 2.0)
        self.assertEqual(report["duplicate_candle_count"], 1)
        self.assertEqual(report["request_count"], 2)
        self.assertEqual(report["completion_time_range"], {"start_ms": 299999, "end_ms": 899999})
        self.assertTrue(report["coverage_sufficient"])
        self.assertEqual(report["source"], "okx")
        self.assertEqual(report["inst_id"], "ETH-USDT-SWAP")

    def test_reports_okx_coverage_shortfall_as_a_stop_condition(self):
        candles, report = fetch_okx_candles(
            start_ms=0, end_ms=299999,
            fetcher=lambda *_: {"code": "0", "data": []},
        )

        self.assertEqual(candles, [])
        self.assertFalse(report["coverage_sufficient"])
        self.assertEqual(report["stop_conditions"], ["insufficient candle API coverage"])

    def test_rejects_malformed_okx_status_and_rows(self):
        with self.assertRaisesRegex(ValueError, "OKX response status"):
            fetch_okx_candles(start_ms=0, end_ms=299999, fetcher=lambda *_: {"code": "1", "data": []})
        with self.assertRaisesRegex(ValueError, "OKX candle row"):
            fetch_okx_candles(
                start_ms=0, end_ms=299999,
                fetcher=lambda *_: {"code": "0", "data": [["0", "1", "2"]]},
            )

    def test_event_inside_candle_open_coverage_is_not_out_of_range(self):
        event = [{"ts": 150, "price": 100.0, "notional_usd": 1000.0}]
        event_report = {
            "raw_event_line_count": 1,
            "raw_event_hash_count": 1,
            "rejected_count": 0,
            "duplicate_count": 0,
        }
        candle_report = {
            "api_url": "https://example.test",
            "source": "okx",
            "inst_id": "ETH-USDT-SWAP",
            "interval": "5m",
            "requested_range": {"start_ms": 0, "end_ms": 299999},
            "fetched_range": {"start_ms": 0, "end_ms": 299999},
            "completion_time_range": {"start_ms": 299999, "end_ms": 299999},
            "coverage_sufficient": True,
            "gap_count": 0,
        }

        report = build_research_report(event_report, candle_report, event)

        self.assertEqual(report["stop_conditions"], [])

    def test_cli_defaults_to_okx_for_five_minute_candles(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "okx.jsonl"
            source.write_text(json.dumps({"ts": 300000, "bkPx": "2000", "sz": "1"}) + "\n", encoding="utf-8")
            output = root / "dataset"

            def fetcher(_url, params):
                self.assertEqual(params["instId"], "ETH-USDT-SWAP")
                return {"code": "0", "data": [["0", "1", "2", "0.5", "1.5"]]}

            self.assertEqual(main([
                "--input", str(source), "--output-dir", str(output), "--start-ms", "0", "--end-ms", "299999"
            ], fetcher=fetcher), 0)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["candles"]["source"], "okx")
            self.assertEqual(report["provenance"]["candle_source"], "okx")

    def test_cli_writes_read_only_dataset_and_uses_completion_range(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "okx.jsonl"
            source.write_text(json.dumps({"ts": 300000, "bkPx": "2000", "sz": "1"}) + "\n", encoding="utf-8")
            output = root / "dataset"

            def fetcher(_url, payload):
                return [{"t": payload["req"]["startTime"], "T": payload["req"]["endTime"],
                         "o": "1", "h": "2", "l": "1", "c": "1.5"}]

            self.assertEqual(main([
                "--input", str(source), "--output-dir", str(output), "--start-ms", "0", "--end-ms", "300000",
                "--candle-source", "hyperliquid",
            ], fetcher=fetcher), 0)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["candles"]["timestamp_field"], "T")
            self.assertEqual(report["stop_conditions"], [])
            self.assertTrue((output / "normalized_events.json").exists())
            self.assertTrue((output / "candles.json").exists())


if __name__ == "__main__":
    unittest.main()
