"""Exchange-sourced accounting helpers for HL Flush Bot.

All profitability reporting should use economic PnL:
closed PnL - fees + funding, with transfers separated.
"""

from __future__ import annotations

from typing import Any, Iterable


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _delta_usdc(record: dict[str, Any]) -> float:
    delta = record.get("delta", {})
    if isinstance(delta, dict):
        for key in ("usdc", "USDC", "amount", "ntl"):
            if key in delta:
                return _as_float(delta.get(key))
    for key in ("usdc", "USDC", "amount", "delta"):
        if key in record:
            return _as_float(record.get(key))
    return 0.0


def compute_economic_summary(
    fills: Iterable[dict[str, Any]],
    funding: Iterable[dict[str, Any]] = (),
    transfers: Iterable[dict[str, Any]] = (),
) -> dict[str, float]:
    """Compute economic PnL from exchange records.

    Hyperliquid fill fees are reported as positive fee amounts, so they are
    represented as a negative PnL component here.
    """
    gross_closed = sum(_as_float(fill.get("closedPnl")) for fill in fills)
    fees = -sum(abs(_as_float(fill.get("fee"))) for fill in fills)
    funding_total = sum(_delta_usdc(row) for row in funding)
    transfer_total = sum(_delta_usdc(row) for row in transfers)
    trading_before_transfers = gross_closed + fees + funding_total
    equity_delta = trading_before_transfers + transfer_total
    return {
        "gross_closed_pnl": round(gross_closed, 4),
        "fees": round(fees, 4),
        "funding": round(funding_total, 4),
        "transfers": round(transfer_total, 4),
        "trading_pnl_before_transfers": round(trading_before_transfers, 4),
        "equity_delta_explained": round(equity_delta, 4),
    }
