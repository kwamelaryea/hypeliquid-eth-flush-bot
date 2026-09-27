"""Pure, read-only reconciliation of Hyperliquid account ledger records."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


# These records move collateral between Hyperliquid account classes. They are
# preserved in the report but must not be counted as external equity flows.
_INTERNAL_TRANSFER_TYPES = frozenset({"accountClassTransfer", "internalTransfer"})


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _funding_delta_usdc(record: Mapping[str, Any]) -> float:
    """Read the documented Hyperliquid funding ``delta.usdc`` field only."""
    delta = record.get("delta")
    if isinstance(delta, Mapping) and "usdc" in delta:
        return _as_float(delta["usdc"])
    return 0.0


def _ledger_delta_usdc(record: Mapping[str, Any]) -> float:
    """Read supported Hyperliquid non-funding ledger delta representations."""
    delta = record.get("delta")
    if not isinstance(delta, Mapping):
        return 0.0
    # Prefer explicit USDC valuation when both token amount and USD value exist.
    for field in ("usdc", "usdcValue", "amount"):
        if field in delta:
            return _as_float(delta[field])
    return 0.0


def _ledger_type(record: Mapping[str, Any]) -> str:
    delta = record.get("delta")
    record_type = delta.get("type") if isinstance(delta, Mapping) else None
    if record_type is None:
        record_type = record.get("type")
    return str(record_type) if record_type is not None else "unknown"


def _current_equity(snapshot: Any) -> float | None:
    """Extract account equity from a numeric value or a user-state snapshot."""
    if isinstance(snapshot, Mapping):
        for section in ("marginSummary", "crossMarginSummary"):
            value = snapshot.get(section)
            if isinstance(value, Mapping) and "accountValue" in value:
                return _as_float(value["accountValue"])
        if "accountValue" in snapshot:
            return _as_float(snapshot["accountValue"])
        return None
    if snapshot is None:
        return None
    try:
        return float(snapshot)
    except (TypeError, ValueError):
        return None


def reconcile_ledger(
    fills: Iterable[Mapping[str, Any]],
    funding_records: Iterable[Mapping[str, Any]] = (),
    ledger_records: Iterable[Mapping[str, Any]] = (),
    *,
    current_equity: Any | None = None,
    initial_equity: Any | None = None,
) -> dict[str, Any]:
    """Reconcile exchange-native PnL without assigning strategy labels.

    Trading PnL is derived solely from fills and funding. Every non-funding
    ledger record is preserved as a transfer under its exchange-provided type,
    including unknown types, rather than being inferred to be a trade.
    """
    gross_closed_pnl = 0.0
    fees = 0.0
    side_counts: Counter[str] = Counter()
    direction_counts: Counter[str] = Counter()
    fill_count = 0

    for fill in fills:
        fill_count += 1
        gross_closed_pnl += _as_float(fill.get("closedPnl"))
        fees -= abs(_as_float(fill.get("fee")))
        side_counts[str(fill.get("side", "unknown"))] += 1
        direction_counts[str(fill.get("dir", "unknown"))] += 1

    funding_total = sum(_funding_delta_usdc(record) for record in funding_records)
    transfers_by_type: defaultdict[str, float] = defaultdict(float)
    for record in ledger_records:
        transfers_by_type[_ledger_type(record)] += _ledger_delta_usdc(record)

    transfers_total = sum(transfers_by_type.values())
    external_transfers_total = sum(
        amount
        for ledger_type, amount in transfers_by_type.items()
        if ledger_type not in _INTERNAL_TRANSFER_TYPES
    )
    trading_before_transfers = gross_closed_pnl + fees + funding_total
    explained_equity_delta = trading_before_transfers + external_transfers_total

    equity = _current_equity(current_equity)
    baseline = _current_equity(initial_equity)
    residual = None
    if equity is not None and baseline is not None:
        residual = round((equity - baseline) - explained_equity_delta, 4)

    return {
        "gross_closed_pnl": round(gross_closed_pnl, 4),
        "fees": round(fees, 4),
        "funding_total": round(funding_total, 4),
        "transfers_by_type": {
            ledger_type: round(amount, 4) for ledger_type, amount in transfers_by_type.items()
        },
        "transfers_total": round(transfers_total, 4),
        "external_transfers_total": round(external_transfers_total, 4),
        "trading_pnl_before_transfers": round(trading_before_transfers, 4),
        "explained_equity_delta": round(explained_equity_delta, 4),
        "residual": residual,
        "fill_count": fill_count,
        "fill_side_counts": dict(side_counts),
        "fill_direction_counts": dict(direction_counts),
    }
