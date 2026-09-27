"""Fetch and reconcile public Hyperliquid ledger records for one wallet.

This module is intentionally read-only: it uses only the public Info API and
never loads account credentials or assigns strategy labels.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, TextIO

import requests

from ledger_reconciliation import reconcile_ledger

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
REQUEST_TYPES = (
    "userFills",
    "userFunding",
    "userNonFundingLedgerUpdates",
    "clearinghouseState",
)
FetchInfo = Callable[[str, Mapping[str, str]], Any]


class InfoApiError(RuntimeError):
    """The public Hyperliquid Info API could not provide a valid response."""


def fetch_info(url: str, payload: Mapping[str, str]) -> Any:
    """POST one public Info API query and return its decoded JSON response."""
    request_type = payload.get("type", "unknown")
    try:
        response = requests.post(url, json=dict(payload), timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise InfoApiError(f"Info API request for {request_type} failed: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise InfoApiError(f"Info API response for {request_type} was not valid JSON") from exc


def _fetch_records(user: str, url: str, fetcher: FetchInfo) -> dict[str, Any]:
    return {
        request_type: fetcher(url, {"type": request_type, "user": user})
        for request_type in REQUEST_TYPES
    }


def _require_record_list(response_type: str, value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{response_type} response must be a JSON array")
    if not all(isinstance(record, Mapping) for record in value):
        raise ValueError(f"{response_type} response contains a non-object record")
    return value


def _require_state(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("clearinghouseState response must be a JSON object")
    positions = value.get("assetPositions")
    if not isinstance(positions, list):
        raise ValueError("clearinghouseState response is missing an assetPositions array")
    return value


def _open_positions(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return exchange-reported non-zero positions, without interpreting them."""
    positions = state["assetPositions"]
    assert isinstance(positions, list)  # Checked by _require_state.
    open_positions: list[dict[str, Any]] = []

    for index, asset_position in enumerate(positions):
        if not isinstance(asset_position, Mapping):
            raise ValueError(f"clearinghouseState assetPositions[{index}] must be an object")
        position = asset_position.get("position")
        if not isinstance(position, Mapping):
            raise ValueError(
                f"clearinghouseState assetPositions[{index}].position must be an object"
            )
        if "coin" not in position or "szi" not in position:
            raise ValueError(
                f"clearinghouseState assetPositions[{index}].position is missing coin or szi"
            )
        try:
            size = Decimal(str(position["szi"]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"clearinghouseState assetPositions[{index}].position.szi must be numeric"
            ) from exc
        if not size.is_finite():
            raise ValueError(
                f"clearinghouseState assetPositions[{index}].position.szi must be finite"
            )
        if size == 0:
            continue

        open_positions.append(
            {
                "coin": position["coin"],
                "size": position["szi"],
                "entry_price": position.get("entryPx"),
                "unrealized_pnl": position.get("unrealizedPnl"),
                "leverage": position.get("leverage"),
            }
        )

    return sorted(open_positions, key=lambda position: str(position["coin"]))


def build_reconciliation_report(
    user: str,
    *,
    url: str = DEFAULT_INFO_URL,
    fetcher: FetchInfo = fetch_info,
) -> dict[str, Any]:
    """Fetch public records and build a JSON-serializable reconciliation report."""
    records = _fetch_records(user, url, fetcher)
    fills = _require_record_list("userFills", records["userFills"])
    funding = _require_record_list("userFunding", records["userFunding"])
    ledger = _require_record_list(
        "userNonFundingLedgerUpdates", records["userNonFundingLedgerUpdates"]
    )
    state = _require_state(records["clearinghouseState"])

    return {
        "api_url": url,
        "current_open_positions": _open_positions(state),
        "reconciliation": reconcile_ledger(
            fills,
            funding,
            ledger,
            current_equity=state,
        ),
        "user": user,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: FetchInfo = fetch_info,
    output: TextIO | None = None,
) -> int:
    """Run the read-only reconciliation CLI."""
    parser = argparse.ArgumentParser(description="Reconcile public Hyperliquid account ledger records.")
    parser.add_argument("--user", required=True, help="Hyperliquid wallet address to inspect")
    parser.add_argument("--url", default=DEFAULT_INFO_URL, help="Hyperliquid Info API URL")
    args = parser.parse_args(argv)

    try:
        report = build_reconciliation_report(args.user, url=args.url, fetcher=fetcher)
    except (InfoApiError, ValueError) as exc:
        parser.error(str(exc))

    json.dump(report, output or sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    (output or sys.stdout).write("\n")
    return 0


if __name__ == "__main__":
    main()
