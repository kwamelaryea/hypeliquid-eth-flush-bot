"""Read-only OKX scheduler for the prospective Hypothesis B paper ledger.

This module uses only OKX public REST endpoints.  It deliberately has no
credentials, exchange trading clients, WebSocket connections, or order code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import requests

from liquidation_exhaustion import CANDLE_INTERVAL_MS, PAPER_LABEL
from prospective_paper import LedgerPaths, run_prospective_paper
from research_dataset import (
    DEFAULT_INTERVAL,
    DEFAULT_OKX_INST_ID,
    OKX_USER_AGENT,
    fetch_okx_candles,
)

OKX_LIQUIDATION_ORDERS_URL = "https://www.okx.com/api/v5/public/liquidation-orders"
OKX_LIQUIDATION_INST_TYPE = "SWAP"
OKX_LIQUIDATION_ULY = "ETH-USDT"
OKX_LIQUIDATION_INST_ID = "ETH-USDT-SWAP"
OKX_CONTRACT_SIZE_ETH = 0.1
OKX_LIQUIDATION_PAGE_LIMIT = 100
OKX_LIQUIDATION_STATE = "filled"
BOOTSTRAP_MARKER_NAME = "prospective_bootstrap.json"

CandleFetcher = Callable[..., Any]
EventFetcher = Callable[..., Any]
Clock = Callable[[], float]


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"OKX liquidation detail has invalid {field}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"OKX liquidation detail has invalid {field}") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"OKX liquidation detail has invalid {field}")
    return number


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"OKX liquidation detail has invalid {field}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"OKX liquidation detail has invalid {field}") from error
    if not math.isfinite(number) or not number.is_integer() or number <= 0:
        raise ValueError(f"OKX liquidation detail has invalid {field}")
    return int(number)


def _raw_detail_hash(detail: Mapping[str, Any]) -> str:
    """Return a reproducible identity for a complete raw OKX detail object."""
    try:
        encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("OKX liquidation detail is not canonical JSON") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_okx_liquidation_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one OKX detail without inventing missing identity or values.

    The public endpoint reports contract count in ``sz``.  ETH-USDT-SWAP uses
    0.1 ETH contracts, so notional is ``sz * bkPx * 0.1``.
    """
    if not isinstance(detail, Mapping):
        raise ValueError("OKX liquidation detail must be an object")
    try:
        timestamp = _timestamp(detail["ts"], "ts")
        price = _finite_positive(detail["bkPx"], "bkPx")
        contracts = _finite_positive(detail["sz"], "sz")
    except KeyError as error:
        raise ValueError(f"OKX liquidation detail is missing {error.args[0]}") from error
    return {
        "event_id": f"okx:{_raw_detail_hash(detail)}",
        "ts": timestamp,
        "price": price,
        "notional_usd": contracts * price * OKX_CONTRACT_SIZE_ETH,
    }


def _default_okx_event_fetcher(url: str, params: dict[str, str]) -> Any:
    response = requests.get(url, params=params, headers={"User-Agent": OKX_USER_AGENT}, timeout=20)
    response.raise_for_status()
    return response.json()


def fetch_okx_liquidation_events(
    *,
    fetcher: Optional[Callable[[str, dict[str, str]], Any]] = None,
    api_url: str = OKX_LIQUIDATION_ORDERS_URL,
    limit: int = OKX_LIQUIDATION_PAGE_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch and strictly normalize paged public ETH swap liquidation details.

    ``after`` is advanced to the oldest detail timestamp from each full page.
    An empty first page is a valid "no public liquidations" observation, but is
    surfaced in the returned report so callers log it rather than treating it
    as an invisible complete data cycle.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= OKX_LIQUIDATION_PAGE_LIMIT:
        raise ValueError("OKX liquidation page limit must be an integer from 1 to 100")
    get_page = fetcher or _default_okx_event_fetcher
    after: Optional[int] = None
    request_count = 0
    empty_response = False
    by_id: dict[str, dict[str, Any]] = {}

    while True:
        params = {
            "instType": OKX_LIQUIDATION_INST_TYPE,
            "uly": OKX_LIQUIDATION_ULY,
            "state": OKX_LIQUIDATION_STATE,
            "limit": str(limit),
        }
        if after is not None:
            params["after"] = str(after)
        response = get_page(api_url, params)
        request_count += 1
        if not isinstance(response, Mapping) or response.get("code") != "0":
            raise ValueError("OKX liquidation response status must be an object with code '0'")
        page = response.get("data")
        if not isinstance(page, list):
            raise ValueError("OKX liquidation response data must be a JSON array")
        if not page:
            empty_response = request_count == 1
            break

        page_events: list[dict[str, Any]] = []
        for group_index, group in enumerate(page):
            if not isinstance(group, Mapping):
                raise ValueError(f"OKX liquidation data item {group_index} must be an object")
            if group.get("instType") != OKX_LIQUIDATION_INST_TYPE:
                raise ValueError("OKX liquidation data has unexpected instType")
            if group.get("instId") != OKX_LIQUIDATION_INST_ID:
                raise ValueError("OKX liquidation data has unexpected instId")
            details = group.get("details")
            if not isinstance(details, list) or not details:
                raise ValueError("OKX liquidation data item must contain non-empty details")
            for detail in details:
                page_events.append(normalize_okx_liquidation_detail(detail))

        oldest_timestamp = min(event["ts"] for event in page_events)
        if after is not None and oldest_timestamp >= after:
            raise ValueError("OKX liquidation pagination did not make backward progress")
        for event in page_events:
            previous = by_id.get(event["event_id"])
            if previous is not None and previous != event:
                raise ValueError("OKX liquidation event identity conflict")
            by_id[event["event_id"]] = event
        if len(page) < limit:
            break
        after = oldest_timestamp

    events = sorted(by_id.values(), key=lambda event: (event["ts"], event["event_id"]))
    return events, {
        "source": "okx",
        "api_url": api_url,
        "inst_type": OKX_LIQUIDATION_INST_TYPE,
        "uly": OKX_LIQUIDATION_ULY,
        "request_count": request_count,
        "event_count": len(events),
        "empty_response": empty_response,
    }


# A short alias makes the public liquidation fetcher easy to discover beside
# research_dataset.fetch_okx_candles.
fetch_okx_liquidations = fetch_okx_liquidation_events


def _marker_path(state_dir: Path) -> Path:
    return state_dir / BOOTSTRAP_MARKER_NAME


def _read_marker(state_dir: Path) -> Optional[dict[str, Any]]:
    path = _marker_path(state_dir)
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid prospective bootstrap marker: {path}") from error
    if not isinstance(marker, dict) or marker.get("bootstrap_completed") is not True:
        raise ValueError(f"invalid prospective bootstrap marker: {path}")
    return marker


def _latest_persisted_candle_ts(state_dir: Path) -> Optional[int]:
    path = LedgerPaths.in_state_dir(state_dir).candles
    if not path.exists():
        return None
    timestamps: list[int] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid completed candle ledger at line {line_number}") from error
        if not isinstance(record, Mapping) or record.get("record_type") != "completed_candle":
            raise ValueError("invalid completed candle ledger record")
        timestamps.append(_timestamp(record.get("ts"), "persisted candle timestamp"))
    return max(timestamps) if timestamps else None


def _clock_ms(clock: Clock) -> int:
    value = clock()
    if isinstance(value, bool):
        raise ValueError("clock must return a finite epoch timestamp")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("clock must return a finite epoch timestamp") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("clock must return a finite epoch timestamp")
    return int(seconds * 1000)


def _latest_completed_candle_ts(clock_ms: int) -> int:
    """Return the inclusive completion timestamp immediately before the current bar."""
    return (clock_ms // CANDLE_INTERVAL_MS) * CANDLE_INTERVAL_MS - 1


def _unpack_candle_fetch(result: Any) -> list[Mapping[str, Any]]:
    candles = result[0] if isinstance(result, tuple) and len(result) == 2 else result
    if not isinstance(candles, list):
        raise ValueError("OKX candle fetcher must return a candle list or (list, report)")
    if not all(isinstance(candle, Mapping) for candle in candles):
        raise ValueError("OKX candle fetcher returned a malformed candle row")
    return candles


def _unpack_event_fetch(result: Any) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    if isinstance(result, tuple) and len(result) == 2:
        events, report = result
    else:
        events, report = result, {}
    if not isinstance(events, list) or not all(isinstance(event, Mapping) for event in events):
        raise ValueError("OKX liquidation fetcher must return an event list or (list, report)")
    if not isinstance(report, Mapping):
        raise ValueError("OKX liquidation fetcher returned a malformed report")
    return events, dict(report)


def _completed_only(candles: Sequence[Mapping[str, Any]], latest_completed_ts: int) -> list[Mapping[str, Any]]:
    completed: list[Mapping[str, Any]] = []
    for index, candle in enumerate(candles):
        complete = candle.get("complete", candle.get("completed", True))
        if not isinstance(complete, bool):
            raise ValueError(f"candle {index}.complete must be boolean when supplied")
        if not complete:
            continue
        timestamp = _timestamp(candle.get("ts"), f"candle {index}.ts")
        if timestamp <= latest_completed_ts:
            completed.append(candle)
    return completed


def run_once(
    *,
    state_dir: Path,
    manifest: Path,
    source: str,
    candle_fetcher: Optional[CandleFetcher] = None,
    event_fetcher: Optional[EventFetcher] = None,
    clock: Clock = time.time,
    logger: Optional[logging.Logger] = None,
) -> dict[str, Any]:
    """Collect one completed-bar batch and pass it to the paper-only ledger.

    Injected candle fetchers receive ``inst_id``, ``bar``, ``start_ms``, and
    ``end_ms`` keyword arguments; injected event fetchers receive no arguments
    and return normalized events or ``(events, report)``.  Neither injection is
    allowed to supply trading capability.
    """
    state_dir = Path(state_dir)
    manifest = Path(manifest)
    log = logger or logging.getLogger(__name__)
    marker = _read_marker(state_dir)
    persisted_latest = _latest_persisted_candle_ts(state_dir)
    if marker is None and persisted_latest is not None:
        raise ValueError("existing candle ledger has no bootstrap marker; refusing unsafe bootstrap")
    if marker is not None and persisted_latest is None:
        raise ValueError("bootstrap marker exists but completed candle ledger is empty")

    latest_completed = _latest_completed_candle_ts(_clock_ms(clock))
    if latest_completed < 0:
        raise ValueError("clock precedes the first complete 5m candle")
    bootstrap = marker is None
    if bootstrap:
        start_ms = latest_completed - 2 * CANDLE_INTERVAL_MS
    else:
        assert persisted_latest is not None
        start_ms = persisted_latest + CANDLE_INTERVAL_MS

    candles: list[Mapping[str, Any]] = []
    if start_ms <= latest_completed:
        fetch_candles = candle_fetcher or fetch_okx_candles
        fetched = fetch_candles(
            inst_id=DEFAULT_OKX_INST_ID,
            bar=DEFAULT_INTERVAL,
            start_ms=start_ms,
            end_ms=latest_completed,
        )
        candles = _completed_only(_unpack_candle_fetch(fetched), latest_completed)
        if not candles:
            raise ValueError("OKX candle response was empty for an expected completed-candle range")

    fetch_events = event_fetcher or fetch_okx_liquidation_events
    events, event_report = _unpack_event_fetch(fetch_events())
    if event_report.get("empty_response"):
        log.warning("OKX liquidation response was empty; recording no public liquidation events this cycle")

    if bootstrap:
        # A clean prospective run has context, not retrospective decisions.
        if len(candles) < 3:
            raise ValueError("OKX candle response did not provide three completed bootstrap candles")
        candles = candles[-3:]
    result = run_prospective_paper(candles, events, state_dir=state_dir, manifest=manifest, source=source)

    if bootstrap:
        marker_payload = {
            "bootstrap_completed": True,
            "context_candle_timestamps": [candle["ts"] for candle in candles],
            "label": PAPER_LABEL,
        }
        state_dir.mkdir(parents=True, exist_ok=True)
        _marker_path(state_dir).write_text(
            json.dumps(marker_payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        result["bootstrap"] = True
    else:
        result["bootstrap"] = False
    result["event_fetch"] = event_report
    return result


def run_forever(
    *,
    state_dir: Path,
    manifest: Path,
    source: str,
    poll_interval: float = 60.0,
    candle_fetcher: Optional[CandleFetcher] = None,
    event_fetcher: Optional[EventFetcher] = None,
    clock: Clock = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Run the read-only collector until interrupted, logging failed cycles."""
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("poll_interval must be a positive finite number of seconds")
    log = logger or logging.getLogger(__name__)
    log.warning("%s", PAPER_LABEL)
    try:
        while True:
            try:
                result = run_once(
                    state_dir=state_dir, manifest=manifest, source=source,
                    candle_fetcher=candle_fetcher, event_fetcher=event_fetcher,
                    clock=clock, logger=log,
                )
                log.info("paper collector cycle: %s", result["new_records"])
            except (OSError, ValueError, requests.RequestException) as error:
                # run_once fetches before journal mutation; failures never turn
                # an API error into a partial ledger update.
                log.error("paper collector cycle failed: %s", error)
            sleeper(float(poll_interval))
    except KeyboardInterrupt:
        log.info("paper collector stopped by KeyboardInterrupt")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    candle_fetcher: Optional[CandleFetcher] = None,
    event_fetcher: Optional[EventFetcher] = None,
    clock: Clock = time.time,
) -> int:
    parser = argparse.ArgumentParser(description="PAPER/SIMULATION ONLY read-only OKX prospective collector")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true", help="run one safe public-data collection cycle")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(PAPER_LABEL)
    try:
        if args.once:
            result = run_once(
                state_dir=args.state_dir, manifest=args.manifest, source=args.source,
                candle_fetcher=candle_fetcher, event_fetcher=event_fetcher, clock=clock,
            )
            print(json.dumps(result, sort_keys=True, allow_nan=False))
        else:
            run_forever(
                state_dir=args.state_dir, manifest=args.manifest, source=args.source,
                poll_interval=args.poll_interval, candle_fetcher=candle_fetcher,
                event_fetcher=event_fetcher, clock=clock,
            )
    except (OSError, ValueError, requests.RequestException) as error:
        logging.getLogger(__name__).error("paper collector error: %s", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
