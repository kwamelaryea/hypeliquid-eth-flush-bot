"""Read-only data preparation for ETH liquidation/RSI research.

This module deliberately contains no trading, authentication, or private-key logic.
Candle timestamps are evaluated at Hyperliquid's ``T`` (candle completion) field so
consumers can use only candles that were complete at an event timestamp.
"""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
OKX_HISTORY_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
DEFAULT_OKX_INST_ID = "ETH-USDT-SWAP"
DEFAULT_INTERVAL = "5m"
DEFAULT_MAX_CANDLES_PER_REQUEST = 5000
OKX_MAX_CANDLES_PER_REQUEST = 100
OKX_USER_AGENT = "Mozilla/5.0 (compatible; research-dataset/1.0)"


def _canonical_record_hash(record: Any) -> str:
    """Return a stable SHA-256 digest for a parsed source record."""
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid %s" % field) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("invalid %s" % field)
    return parsed


def _as_positive_ms(value: Any, field: str = "ts") -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid %s" % field) from exc
    if parsed <= 0:
        raise ValueError("invalid %s" % field)
    return parsed


def interval_to_ms(interval: str) -> int:
    """Convert a Hyperliquid candle interval such as ``5m`` to milliseconds."""
    if not isinstance(interval, str) or len(interval) < 2:
        raise ValueError("interval must be a positive value such as '5m'")
    unit = interval[-1]
    try:
        amount = int(interval[:-1])
    except ValueError as exc:
        raise ValueError("interval must be a positive value such as '5m'") from exc
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if amount <= 0 or unit not in multipliers:
        raise ValueError("unsupported interval: %r" % interval)
    return amount * multipliers[unit]


def normalize_okx_records(
    records: Iterable[Any], source: str = "okx"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Normalize direct OKX liquidation JSONL objects.

    OKX ETH-USDT-SWAP contracts represent 0.1 ETH.  Notional is therefore
    ``sz * bkPx * 0.1``, matching :mod:`websocket_manager` exactly.  Bad or
    non-positive records are omitted rather than guessed.
    """
    events: List[Dict[str, Any]] = []
    raw_hashes = set()
    rejected_count = 0
    line_count = 0

    for record in records:
        line_count += 1
        try:
            record_hash = _canonical_record_hash(record)
            raw_hashes.add(record_hash)
        except (TypeError, ValueError, OverflowError):
            rejected_count += 1
            continue
        if not isinstance(record, dict):
            rejected_count += 1
            continue
        try:
            timestamp = _as_positive_ms(record.get("ts"))
            price = _as_positive_float(record.get("bkPx"), "bkPx")
            contracts = _as_positive_float(record.get("sz"), "sz")
        except (TypeError, ValueError, OverflowError):
            rejected_count += 1
            continue

        events.append({
            "ts": timestamp,
            "price": price,
            "notional_usd": contracts * price * 0.1,
            "source": source,
            "source_record_hash": record_hash,
        })

    time_range = _event_time_range(events)
    return events, {
        "raw_event_line_count": line_count,
        "raw_event_hash_count": len(raw_hashes),
        "rejected_count": rejected_count,
        "valid_count": len(events),
        "duplicate_count": 0,
        "time_range": time_range,
        "total_notional_usd": sum(event["notional_usd"] for event in events),
    }


def _event_time_range(events: Sequence[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    if not events:
        return None
    timestamps = [event["ts"] for event in events]
    return {"start_ms": min(timestamps), "end_ms": max(timestamps)}


def deduplicate_events(events: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Deduplicate source-identical events and return a stable timestamp ordering.

    The identity is timestamp, normalized price, and the source record digest.
    The digest preserves the raw OKX size (and any other raw identifying fields),
    avoiding a collision between distinct records that merely share a timestamp.
    """
    ordered = sorted(
        events,
        key=lambda event: (event["ts"], event["price"], event["source_record_hash"]),
    )
    seen = set()
    deduplicated: List[Dict[str, Any]] = []
    duplicate_count = 0
    for event in ordered:
        identity = (event["ts"], event["price"], event["source_record_hash"])
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        deduplicated.append(event)
    return deduplicated, duplicate_count


def prepare_normalized_events(
    records: Iterable[Any], source: str = "okx"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Normalize, source-deduplicate, and summarize OKX liquidation records."""
    normalized, report = normalize_okx_records(records, source=source)
    events, duplicate_count = deduplicate_events(normalized)
    report = dict(report)
    report.update({
        "duplicate_count": duplicate_count,
        "valid_count": len(events),
        "time_range": _event_time_range(events),
        "total_notional_usd": sum(event["notional_usd"] for event in events),
    })
    return events, report


def _candle_value(candle: Dict[str, Any], field: str) -> float:
    if field not in candle:
        raise ValueError("candle missing required field %r" % field)
    try:
        value = float(candle[field])
    except (TypeError, ValueError) as exc:
        raise ValueError("candle has invalid field %r" % field) from exc
    if not math.isfinite(value):
        raise ValueError("candle has invalid field %r" % field)
    return value


def _candle_timestamp(candle: Dict[str, Any], field: str) -> int:
    if field not in candle:
        raise ValueError("candle missing required field %r" % field)
    try:
        value = int(candle[field])
    except (TypeError, ValueError) as exc:
        raise ValueError("candle has invalid field %r" % field) from exc
    if value < 0:
        raise ValueError("candle has invalid field %r" % field)
    return value


def validate_candle_coverage(
    candles: Sequence[Dict[str, Any]],
    interval: str,
    requested_start_ms: Optional[int] = None,
    requested_end_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate OHLC candles using their completion (``T``) timestamps.

    Raises ``ValueError`` for malformed fields, duplicate completions, or
    non-increasing completions.  Gaps and requested-boundary shortfalls are
    returned as report fields so a caller can surface them as stop conditions.
    """
    interval_ms = interval_to_ms(interval)
    previous_completion: Optional[int] = None
    previous_open: Optional[int] = None
    completion_times: List[int] = []
    open_times: List[int] = []
    gaps: List[Dict[str, int]] = []

    for candle in candles:
        if not isinstance(candle, dict):
            raise ValueError("each candle must be a JSON object")
        opened_at = _candle_timestamp(candle, "t")
        completed_at = _candle_timestamp(candle, "T")
        for field in ("o", "h", "l", "c"):
            _candle_value(candle, field)
        if completed_at <= opened_at:
            raise ValueError("candle completion timestamp must be after its open timestamp")
        if previous_completion is not None:
            delta = completed_at - previous_completion
            if delta <= 0:
                raise ValueError("candle completion timestamps must be strictly increasing")
            if delta > interval_ms:
                gaps.append({
                    "after_completion_ms": previous_completion,
                    "before_completion_ms": completed_at,
                    "duration_ms": delta,
                    "missing_candle_count": max(0, (delta - 1) // interval_ms),
                })
        if previous_open is not None and opened_at <= previous_open:
            raise ValueError("candle open timestamps must be strictly increasing")
        previous_completion = completed_at
        previous_open = opened_at
        completion_times.append(completed_at)
        open_times.append(opened_at)

    fetched_range = None
    completion_range = None
    if completion_times:
        fetched_range = {"start_ms": open_times[0], "end_ms": completion_times[-1]}
        completion_range = {"start_ms": completion_times[0], "end_ms": completion_times[-1]}
    requested_range = None
    if requested_start_ms is not None or requested_end_ms is not None:
        if requested_start_ms is None or requested_end_ms is None:
            raise ValueError("requested candle range requires both start and end timestamps")
        if requested_end_ms < requested_start_ms:
            raise ValueError("requested candle end must not precede start")
        requested_range = {"start_ms": int(requested_start_ms), "end_ms": int(requested_end_ms)}

    coverage_sufficient = bool(fetched_range)
    coverage_issues: List[str] = []
    if not fetched_range:
        coverage_sufficient = False
        coverage_issues.append("no candles returned")
    elif requested_range:
        if fetched_range["start_ms"] > requested_range["start_ms"]:
            coverage_sufficient = False
            coverage_issues.append("fetched candles start after requested range")
        if fetched_range["end_ms"] < requested_range["end_ms"]:
            coverage_sufficient = False
            coverage_issues.append("fetched candles end before requested range")

    return {
        "candle_count": len(candles),
        "interval": interval,
        "interval_ms": interval_ms,
        "timestamp_field": "T",
        "requested_range": requested_range,
        "fetched_range": fetched_range,
        "completion_time_range": completion_range,
        "gap_count": len(gaps),
        "missing_candle_count": sum(gap["missing_candle_count"] for gap in gaps),
        "max_gap_ms": max((gap["duration_ms"] for gap in gaps), default=0),
        "gaps": gaps,
        "coverage_sufficient": coverage_sufficient,
        "coverage_issues": coverage_issues,
    }


def _default_fetcher(url: str, payload: Dict[str, Any]) -> Any:
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    return response.json()


def fetch_hyperliquid_candles(
    coin: str,
    interval: str = DEFAULT_INTERVAL,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    *,
    fetcher: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    api_url: str = HYPERLIQUID_INFO_URL,
    chunk_duration_ms: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch public Hyperliquid ``candleSnapshot`` data over a chunked range.

    ``fetcher`` is injected in tests and receives ``(api_url, payload)``.  The
    default sends only the public request payload; no private credentials are
    accepted or read by this module.
    """
    if not isinstance(coin, str) or not coin:
        raise ValueError("coin is required")
    interval_ms = interval_to_ms(interval)
    if start_ms is None or end_ms is None:
        raise ValueError("start_ms and end_ms are required")
    try:
        start_ms = int(start_ms)
        end_ms = int(end_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_ms and end_ms must be integer milliseconds") from exc
    if start_ms < 0 or end_ms <= 0:
        raise ValueError("start_ms must be non-negative and end_ms must be positive")
    if end_ms <= start_ms:
        raise ValueError("end_ms must be after start_ms")
    if chunk_duration_ms is None:
        chunk_duration_ms = interval_ms * DEFAULT_MAX_CANDLES_PER_REQUEST
    if not isinstance(chunk_duration_ms, int) or chunk_duration_ms <= 0:
        raise ValueError("chunk_duration_ms must be a positive integer")

    fetch = fetcher or _default_fetcher
    raw_candles: List[Dict[str, Any]] = []
    request_count = 0
    chunk_start = start_ms
    while chunk_start < end_ms:
        chunk_end = min(chunk_start + chunk_duration_ms, end_ms)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": chunk_start,
                "endTime": chunk_end,
            },
        }
        response = fetch(api_url, payload)
        request_count += 1
        if not isinstance(response, list):
            raise ValueError("candleSnapshot response must be a JSON array")
        if not all(isinstance(candle, dict) for candle in response):
            raise ValueError("candleSnapshot response entries must be JSON objects")
        raw_candles.extend(response)
        chunk_start = chunk_end

    # Boundary candles can be returned by both adjacent inclusive requests.
    # Require exact duplicate snapshots at a boundary; conflicting snapshots are
    # malformed provenance rather than silently choosing one.
    by_completion: Dict[int, Dict[str, Any]] = {}
    for candle in raw_candles:
        completion = _candle_timestamp(candle, "T")
        if completion in by_completion and _canonical_record_hash(by_completion[completion]) != _canonical_record_hash(candle):
            raise ValueError("conflicting duplicate candle completion timestamp")
        by_completion[completion] = candle
    candles = sorted(by_completion.values(), key=lambda candle: (_candle_timestamp(candle, "T"), _candle_timestamp(candle, "t")))
    coverage = validate_candle_coverage(candles, interval, start_ms, end_ms)
    report = dict(coverage)
    report.update({
        "source": "hyperliquid",
        "source_url": api_url,
        "api_url": api_url,
        "coin": coin,
        "interval": interval,
        "requested_range": {"start_ms": start_ms, "end_ms": end_ms},
        "request_count": request_count,
        "raw_candle_count": len(raw_candles),
        "duplicate_candle_count": len(raw_candles) - len(candles),
    })
    return candles, report


def _default_okx_fetcher(url: str, params: Dict[str, str]) -> Any:
    """Fetch public OKX candles without authentication or private state."""
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": OKX_USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _normalize_okx_candle_row(row: Any, interval: str) -> Tuple[Dict[str, Any], int]:
    """Convert one OKX array row to the paper-harness candle schema."""
    if not isinstance(row, list) or len(row) < 5:
        raise ValueError("OKX candle row must be a JSON array with at least five fields")
    try:
        opened_at = int(row[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("OKX candle row has invalid timestamp") from exc
    if opened_at < 0:
        raise ValueError("OKX candle row has invalid timestamp")

    fields = (("open", row[1]), ("high", row[2]), ("low", row[3]), ("close", row[4]))
    candle: Dict[str, Any] = {"ts": opened_at + interval_to_ms(interval) - 1}
    for field, value in fields:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("OKX candle row has invalid %s" % field) from exc
        if not math.isfinite(parsed):
            raise ValueError("OKX candle row has invalid %s" % field)
        candle[field] = parsed
    return candle, opened_at


def normalize_okx_candle_rows(records: Iterable[Any], interval: str = DEFAULT_INTERVAL) -> List[Dict[str, Any]]:
    """Normalize OKX history-candle arrays using their computed completion time."""
    return [_normalize_okx_candle_row(record, interval)[0] for record in records]


def _okx_candles_for_coverage(candles: Sequence[Dict[str, Any]], interval: str) -> List[Dict[str, Any]]:
    """Supply the Hyperliquid-shaped timestamps required by coverage validation."""
    interval_ms = interval_to_ms(interval)
    return [{
        "t": candle["ts"] - interval_ms + 1,
        "T": candle["ts"],
        "o": candle["open"],
        "h": candle["high"],
        "l": candle["low"],
        "c": candle["close"],
    } for candle in candles]


def fetch_okx_candles(
    inst_id: str = DEFAULT_OKX_INST_ID,
    bar: str = DEFAULT_INTERVAL,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    *,
    fetcher: Optional[Callable[[str, Dict[str, str]], Any]] = None,
    api_url: str = OKX_HISTORY_CANDLES_URL,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch an inclusive OKX completion-time range, paging backward with ``after``.

    OKX history candles are capped at 100 rows.  Its ``after`` cursor returns
    older opening timestamps, so each request advances the cursor to the oldest
    opening timestamp in the preceding response.
    """
    if not isinstance(inst_id, str) or not inst_id:
        raise ValueError("inst_id is required")
    interval_to_ms(bar)
    if start_ms is None or end_ms is None:
        raise ValueError("start_ms and end_ms are required")
    try:
        start_ms = int(start_ms)
        end_ms = int(end_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_ms and end_ms must be integer milliseconds") from exc
    if start_ms < 0 or end_ms < start_ms:
        raise ValueError("OKX candle range is invalid")

    fetch = fetcher or _default_okx_fetcher
    # Raw timestamps mark candle opening; ``end_ms + 1`` includes a candle that
    # completes exactly at the requested inclusive end boundary.
    cursor = end_ms + 1
    raw_rows: List[Any] = []
    normalized_with_open: List[Tuple[Dict[str, Any], int]] = []
    request_count = 0
    while True:
        params = {
            "instId": inst_id,
            "bar": bar,
            "after": str(cursor),
            "limit": str(OKX_MAX_CANDLES_PER_REQUEST),
        }
        response = fetch(api_url, params)
        request_count += 1
        if not isinstance(response, dict) or response.get("code") != "0":
            raise ValueError("OKX response status must be an object with code '0'")
        page = response.get("data")
        if not isinstance(page, list):
            raise ValueError("OKX response data must be a JSON array")
        if not page:
            break

        page_opens: List[int] = []
        for row in page:
            candle, opened_at = _normalize_okx_candle_row(row, bar)
            raw_rows.append(row)
            normalized_with_open.append((candle, opened_at))
            page_opens.append(opened_at)
        oldest_open = min(page_opens)
        if oldest_open >= cursor:
            raise ValueError("OKX candle pagination did not make backward progress")
        # This page includes the candle needed to establish the requested start
        # boundary.  Additional older pages cannot affect inclusive coverage.
        if oldest_open <= start_ms:
            break
        cursor = oldest_open

    by_completion: Dict[int, Dict[str, Any]] = {}
    for candle, _opened_at in normalized_with_open:
        completion = candle["ts"]
        if start_ms <= completion <= end_ms and completion not in by_completion:
            by_completion[completion] = candle
    candles = [by_completion[completion] for completion in sorted(by_completion)]
    coverage = validate_candle_coverage(
        _okx_candles_for_coverage(candles, bar), bar, start_ms, end_ms
    )
    report = dict(coverage)
    report.update({
        "source": "okx",
        "source_url": api_url,
        "api_url": api_url,
        "inst_id": inst_id,
        "bar": bar,
        "interval": bar,
        "request_count": request_count,
        "raw_rows": raw_rows,
        "raw_candle_count": len(raw_rows),
        "duplicate_candle_count": sum(
            1 for candle, _opened_at in normalized_with_open
            if start_ms <= candle["ts"] <= end_ms
        ) - len(candles),
        "stop_conditions": ["insufficient candle API coverage"]
        if not coverage["coverage_sufficient"] or coverage["gap_count"] else [],
    })
    return candles, report


def read_okx_jsonl(path: Path) -> Tuple[List[Any], Dict[str, int]]:
    """Read JSONL without changing its source, retaining parsing provenance."""
    records: List[Any] = []
    line_count = 0
    malformed_json_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            line_count += 1
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed_json_count += 1
    return records, {"raw_event_line_count": line_count, "malformed_json_count": malformed_json_count}


def _event_range_outside_candles(events: Sequence[Dict[str, Any]], candle_report: Dict[str, Any]) -> bool:
    # Events may occur during the first candle before its completion timestamp;
    # compare against candle *open* coverage, not completion-only timestamps.
    fetched_range = candle_report.get("fetched_range")
    if not events or not fetched_range:
        return bool(events)
    return (
        events[0]["ts"] < fetched_range["start_ms"]
        or events[-1]["ts"] > fetched_range["end_ms"]
    )


def build_research_report(
    event_report: Dict[str, Any], candle_report: Dict[str, Any], events: Sequence[Dict[str, Any]], malformed_json_count: int = 0
) -> Dict[str, Any]:
    """Combine provenance and explicit stop conditions for a research run."""
    raw_line_count = event_report["raw_event_line_count"] + malformed_json_count
    malformed_count = event_report["rejected_count"] + malformed_json_count
    malformed_rate = malformed_count / raw_line_count if raw_line_count else 0.0
    stop_conditions: List[str] = []
    if not candle_report["coverage_sufficient"] or candle_report["gap_count"]:
        stop_conditions.append("insufficient candle API coverage")
    if _event_range_outside_candles(events, candle_report):
        stop_conditions.append("event timestamps outside candle range")
    if malformed_rate > 0.01:
        stop_conditions.append("malformed data rate above 1%")

    return {
        "provenance": {
            "candle_source": candle_report.get("source", "hyperliquid"),
            "source_url": candle_report.get("source_url", candle_report["api_url"]),
            "api_url": candle_report["api_url"],
            "coin": candle_report.get("coin"),
            "inst_id": candle_report.get("inst_id"),
            "bar": candle_report.get("bar", candle_report["interval"]),
            "interval": candle_report["interval"],
            "requested_range": candle_report["requested_range"],
            "fetched_range": candle_report["fetched_range"],
            "raw_event_line_count": raw_line_count,
            "raw_event_hash_count": event_report["raw_event_hash_count"],
            "rejected_count": event_report["rejected_count"],
            "malformed_json_count": malformed_json_count,
            "duplicate_count": event_report["duplicate_count"],
        },
        "events": event_report,
        "candles": candle_report,
        "malformed_data_rate": malformed_rate,
        "stop_conditions": stop_conditions,
    }


def main(argv: Optional[Sequence[str]] = None, fetcher: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> int:
    """Write normalized events, public candle JSON, and an explicit report."""
    parser = argparse.ArgumentParser(description="Build a read-only ETH liquidation research dataset.")
    parser.add_argument("--input", type=Path, default=Path("data/okx_liquidations.jsonl"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--coin", default="ETH")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--candle-source", choices=("hyperliquid", "okx"))
    parser.add_argument("--inst-id", default=DEFAULT_OKX_INST_ID)
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--api-url")
    parser.add_argument("--chunk-candles", type=int, default=DEFAULT_MAX_CANDLES_PER_REQUEST)
    args = parser.parse_args(argv)

    records, input_report = read_okx_jsonl(args.input)
    events, event_report = prepare_normalized_events(records)
    if not events:
        raise ValueError("no valid OKX liquidation events available for candle range")
    start_ms = args.start_ms if args.start_ms is not None else events[0]["ts"]
    end_ms = args.end_ms if args.end_ms is not None else events[-1]["ts"]
    if end_ms <= start_ms:
        end_ms = start_ms + interval_to_ms(args.interval)
    candle_source = args.candle_source or ("okx" if args.interval == "5m" else "hyperliquid")
    if candle_source == "okx":
        candles, candle_report = fetch_okx_candles(
            args.inst_id,
            args.interval,
            start_ms,
            end_ms,
            fetcher=fetcher,
            api_url=args.api_url or OKX_HISTORY_CANDLES_URL,
        )
    else:
        candles, candle_report = fetch_hyperliquid_candles(
            args.coin,
            args.interval,
            start_ms,
            end_ms,
            fetcher=fetcher,
            api_url=args.api_url or HYPERLIQUID_INFO_URL,
            chunk_duration_ms=interval_to_ms(args.interval) * args.chunk_candles,
        )
    report = build_research_report(event_report, candle_report, events, input_report["malformed_json_count"])

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, value in (("normalized_events.json", events), ("candles.json", candles), ("report.json", report)):
        with (args.output_dir / filename).open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, requests.RequestException) as exc:
        print("research dataset error: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
