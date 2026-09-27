"""Paper-only implementation of preregistered liquidation-exhaustion Hypothesis B.

This module reads local, already-normalized OKX liquidation records only.  It has
no exchange clients, credentials, live-order code, or fitted parameters.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


PAPER_LABEL = "PAPER/SIMULATION ONLY — preregistered Hypothesis B; no live orders or network access"
CANDLE_INTERVAL_MS = 300_000
BURST_CANDLES = 3
BURST_NOTIONAL_USD = 1_000_000.0
CONFIRMATION_SHARE_OF_AVERAGE = 0.50
TAKE_PROFIT_PCT = 0.01
STOP_LOSS_PCT = 0.01
RISK_PER_TRADE_USD = 1.0
ROUND_TRIP_FEE_BPS = 10.0
MAX_HOLD_BARS = 12


@dataclass(frozen=True)
class HypothesisBSplitConfig:
    """Explicit non-overlapping chronological split boundaries."""

    development_bars: int
    validation_window_bars: int
    validation_step_bars: int
    holdout_bars: int


@dataclass(frozen=True)
class _Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class _Event:
    ts: int
    price: float
    notional_usd: float


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _timestamp(value: Any, field: str) -> int:
    result = _number(value, field)
    if not result.is_integer() or result < 0:
        raise ValueError(f"{field} must be a non-negative integer millisecond timestamp")
    return int(result)


def _normalize_candles(candles: Sequence[Mapping[str, Any]]) -> list[_Candle]:
    normalized: list[_Candle] = []
    prior_ts: Optional[int] = None
    for index, raw in enumerate(candles):
        if not isinstance(raw, Mapping):
            raise ValueError(f"candle {index} must be a mapping")
        try:
            candle = _Candle(
                ts=_timestamp(raw["ts"], f"candle {index}.ts"),
                open=_number(raw["open"], f"candle {index}.open"),
                high=_number(raw["high"], f"candle {index}.high"),
                low=_number(raw["low"], f"candle {index}.low"),
                close=_number(raw["close"], f"candle {index}.close"),
            )
        except KeyError as error:
            raise ValueError(f"candle {index} is missing {error.args[0]}") from error
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            raise ValueError(f"candle {index} prices must be positive")
        if candle.high < candle.low:
            raise ValueError(f"candle {index} high must be at least low")
        if prior_ts is not None and candle.ts - prior_ts != CANDLE_INTERVAL_MS:
            raise ValueError("candles must be contiguous 5m completion candles")
        normalized.append(candle)
        prior_ts = candle.ts
    return normalized


def _normalize_events(events: Sequence[Mapping[str, Any]]) -> list[_Event]:
    """Keep only finite, explicit normalized dollar-notional records."""
    normalized: list[_Event] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            continue
        try:
            event = _Event(
                ts=_timestamp(raw["ts"], "liquidation.ts"),
                price=_number(raw["price"], "liquidation.price"),
                notional_usd=_number(raw["notional_usd"], "liquidation.notional_usd"),
            )
        except (KeyError, ValueError):
            continue
        if event.price > 0 and event.notional_usd > 0:
            normalized.append(event)
    return normalized


def _interval_start(candle: _Candle) -> int:
    # OKX normalized candle timestamps mark completion; the open is inclusive.
    return candle.ts - CANDLE_INTERVAL_MS + 1


def _in_closed_interval(event: _Event, start_ms: int, end_ms: int) -> bool:
    return start_ms <= event.ts <= end_ms


def _signal_from_normalized(
    candles: Sequence[_Candle], events: Sequence[_Event], confirmation_index: int
) -> dict[str, Any]:
    if isinstance(confirmation_index, bool) or not isinstance(confirmation_index, int):
        raise ValueError("confirmation_index must be an integer")
    if confirmation_index < 0 or confirmation_index >= len(candles):
        raise ValueError("confirmation_index is outside the candle sequence")

    confirmation = candles[confirmation_index]
    entry = candles[confirmation_index + 1] if confirmation_index + 1 < len(candles) else None
    result: dict[str, Any] = {
        "confirmation_index": confirmation_index,
        "confirmation_ts": confirmation.ts,
        "entry_ts": entry.ts if entry else None,
        "entry_price": entry.open if entry else None,
        "burst_total_notional_usd": 0.0,
        "confirmation_total_notional_usd": 0.0,
        # ``threshold`` is retained as an unambiguous name for the preregistered
        # $1m burst requirement; the exhaustion cap has its own explicit key.
        "threshold": BURST_NOTIONAL_USD,
        "burst_threshold_usd": BURST_NOTIONAL_USD,
        "confirmation_threshold_usd": None,
        "burst_low": None,
        "confirmation_low": confirmation.low,
        "eligible": False,
        "reason": "insufficient_preceding_candles",
    }
    if confirmation_index < BURST_CANDLES:
        return result

    burst = candles[confirmation_index - BURST_CANDLES:confirmation_index]
    burst_start = _interval_start(burst[0])
    burst_end = burst[-1].ts
    confirmation_start = _interval_start(confirmation)
    burst_total = sum(
        event.notional_usd for event in events if _in_closed_interval(event, burst_start, burst_end)
    )
    confirmation_total = sum(
        event.notional_usd
        for event in events
        if _in_closed_interval(event, confirmation_start, confirmation.ts)
    )
    confirmation_threshold = (burst_total / BURST_CANDLES) * CONFIRMATION_SHARE_OF_AVERAGE
    burst_low = min(candle.low for candle in burst)
    result.update({
        "burst_total_notional_usd": burst_total,
        "confirmation_total_notional_usd": confirmation_total,
        "confirmation_threshold_usd": confirmation_threshold,
        "burst_low": burst_low,
    })
    if burst_total < BURST_NOTIONAL_USD:
        result["reason"] = "insufficient_burst_notional"
    elif confirmation_total > confirmation_threshold:
        result["reason"] = "confirmation_notional_above_exhaustion_threshold"
    elif confirmation.low < burst_low:
        result["reason"] = "confirmation_made_new_low"
    else:
        result.update({"eligible": True, "reason": "eligible"})
    return result


def evaluate_exhaustion_signal(
    candles: Sequence[Mapping[str, Any]],
    liquidation_events: Sequence[Mapping[str, Any]],
    confirmation_index: int,
) -> dict[str, Any]:
    """Evaluate one completed confirmation candle using only its prior 15 minutes.

    The three burst candles end before the confirmation interval begins.  The
    returned next-open fields are execution metadata only and never contribute
    to eligibility.
    """
    return _signal_from_normalized(
        _normalize_candles(candles), _normalize_events(liquidation_events), confirmation_index
    )


def _at_or_above(value: float, level: float) -> bool:
    return value > level or math.isclose(value, level, rel_tol=1e-12, abs_tol=1e-12)


def _at_or_below(value: float, level: float) -> bool:
    return value < level or math.isclose(value, level, rel_tol=1e-12, abs_tol=1e-12)


def _close_trade(position: Mapping[str, Any], candle: _Candle, index: int, price: float, reason: str) -> dict[str, Any]:
    fee_rate_per_leg = ROUND_TRIP_FEE_BPS / 20_000.0
    quantity = position["quantity"]
    exit_notional = quantity * price
    entry_fee = position["entry_notional"] * fee_rate_per_leg
    exit_fee = exit_notional * fee_rate_per_leg
    gross_pnl = quantity * (price - position["entry_price"])
    fees = entry_fee + exit_fee
    return {
        "entry_ts": position["entry_ts"],
        "entry_price": position["entry_price"],
        "exit_ts": candle.ts,
        "exit_price": price,
        "exit_reason": reason,
        "quantity": quantity,
        "entry_notional": position["entry_notional"],
        "exit_notional": exit_notional,
        "risk_budget_usd": RISK_PER_TRADE_USD,
        "bars_held": index - position["entry_index"],
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": fees,
        "net_pnl": gross_pnl - fees,
    }


def _summary(trades: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    gross_pnl = sum(trade["gross_pnl"] for trade in trades)
    fees = sum(trade["fees"] for trade in trades)
    net_pnl = sum(trade["net_pnl"] for trade in trades)
    wins = sum(trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0)
    losses = sum(trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0)
    if losses < 0:
        profit_factor: Optional[float] = wins / abs(losses)
    elif wins > 0:
        profit_factor = None  # Infinite: there are wins and no closed net losses.
    else:
        profit_factor = 0.0
    equity = peak = max_drawdown = 0.0
    for trade in trades:
        equity += trade["net_pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    eligible = sum(1 for decision in decisions if decision["eligible"])
    return {
        "signal_evaluations": len(decisions),
        "eligible_signals": eligible,
        "denied_signals": len(decisions) - eligible,
        "scheduled_signals": sum(1 for decision in decisions if decision["execution_decision"] == "scheduled"),
        "blocked_position_signals": sum(1 for decision in decisions if decision["execution_decision"] == "position_open"),
        "no_next_candle_signals": sum(1 for decision in decisions if decision["execution_decision"] == "no_next_candle"),
        "trades": len(trades),
        "wins": sum(1 for trade in trades if trade["net_pnl"] > 0),
        "gross_pnl": gross_pnl,
        "fees": fees,
        "net_pnl": net_pnl,
        "net_expectancy": net_pnl / len(trades) if trades else None,
        "profit_factor": profit_factor,
        "profit_factor_status": "infinite" if profit_factor is None and wins > 0 else "finite",
        "max_drawdown": max_drawdown,
    }


def _run_normalized(candles: Sequence[_Candle], events: Sequence[_Event]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    position: Optional[dict[str, Any]] = None
    pending: Optional[dict[str, Any]] = None
    for index, candle in enumerate(candles):
        if pending is not None:
            quantity = RISK_PER_TRADE_USD / (candle.open * STOP_LOSS_PCT)
            position = {
                "entry_index": index,
                "entry_ts": candle.ts,
                "entry_price": candle.open,
                "quantity": quantity,
                "entry_notional": quantity * candle.open,
                "take_profit": candle.open * (1.0 + TAKE_PROFIT_PCT),
                "stop_loss": candle.open * (1.0 - STOP_LOSS_PCT),
            }
            pending = None
        if position is not None:
            if _at_or_above(candle.open, position["take_profit"]):
                trades.append(_close_trade(position, candle, index, candle.open, "take_profit_gap_open"))
                position = None
            elif _at_or_below(candle.open, position["stop_loss"]):
                trades.append(_close_trade(position, candle, index, candle.open, "stop_loss_gap_open"))
                position = None
            elif index - position["entry_index"] >= MAX_HOLD_BARS:
                trades.append(_close_trade(position, candle, index, candle.open, "max_hold_open"))
                position = None
            elif _at_or_below(candle.low, position["stop_loss"]) and _at_or_above(candle.high, position["take_profit"]):
                trades.append(_close_trade(position, candle, index, position["stop_loss"], "stop_loss_same_bar_priority"))
                position = None
            elif _at_or_below(candle.low, position["stop_loss"]):
                trades.append(_close_trade(position, candle, index, position["stop_loss"], "stop_loss"))
                position = None
            elif _at_or_above(candle.high, position["take_profit"]):
                trades.append(_close_trade(position, candle, index, position["take_profit"], "take_profit"))
                position = None
        if index < BURST_CANDLES:
            continue
        decision = _signal_from_normalized(candles, events, index)
        if not decision["eligible"]:
            decision["execution_decision"] = "signal_denied"
        elif index == len(candles) - 1:
            decision["execution_decision"] = "no_next_candle"
        elif position is not None or pending is not None:
            decision["execution_decision"] = "position_open"
        else:
            decision["execution_decision"] = "scheduled"
            pending = decision
        decisions.append(decision)
    open_position = None
    if position is not None:
        open_position = {
            "entry_ts": position["entry_ts"],
            "entry_price": position["entry_price"],
            "take_profit": position["take_profit"],
            "stop_loss": position["stop_loss"],
            "bars_held": len(candles) - 1 - position["entry_index"],
        }
    return {"decisions": decisions, "trades": trades, "open_position": open_position, "summary": _summary(trades, decisions)}


def run_hypothesis_b_simulation(
    candles: Sequence[Mapping[str, Any]], liquidation_events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Run the fixed, long-only preregistered Hypothesis B paper simulation."""
    normalized_candles = _normalize_candles(candles)
    return {
        "label": PAPER_LABEL,
        "parameters": {
            "candle_interval_ms": CANDLE_INTERVAL_MS,
            "burst_candles": BURST_CANDLES,
            "burst_notional_usd": BURST_NOTIONAL_USD,
            "confirmation_share_of_burst_per_candle_average": CONFIRMATION_SHARE_OF_AVERAGE,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "risk_per_trade_usd": RISK_PER_TRADE_USD,
            "round_trip_fee_bps": ROUND_TRIP_FEE_BPS,
            "max_hold_bars": MAX_HOLD_BARS,
            "same_bar_exit_priority": "stop_loss",
        },
        **_run_normalized(normalized_candles, _normalize_events(liquidation_events)),
    }


def _validate_split(config: HypothesisBSplitConfig, total_bars: int) -> None:
    for field in ("development_bars", "validation_window_bars", "validation_step_bars", "holdout_bars"):
        value = getattr(config, field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
    required = config.development_bars + config.validation_window_bars + config.holdout_bars
    if total_bars < required:
        raise ValueError(f"insufficient candles for chronological validation: need at least {required}, got {total_bars}")
    if config.validation_step_bars < config.validation_window_bars:
        raise ValueError("validation windows overlap: validation_step_bars must be at least validation_window_bars")
    if config.validation_step_bars > config.validation_window_bars:
        raise ValueError("validation windows are not sequential: validation_step_bars must equal validation_window_bars")
    validation_region = total_bars - config.development_bars - config.holdout_bars
    if validation_region <= 0 or validation_region % config.validation_window_bars:
        raise ValueError("validation region must contain an exact number of non-overlapping validation windows")


def _stop_decision(summary: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if summary["trades"] < 100:
        reasons.append("fewer than 100 trades")
    profit_factor = summary["profit_factor"]
    if profit_factor is not None and profit_factor < 1.20:
        reasons.append("profit factor below 1.20")
    expectancy = summary["net_expectancy"]
    if expectancy is None or expectancy <= 0:
        reasons.append("net expectancy is not positive")
    return {"status": "FAIL" if reasons else "PASS", "reasons": reasons}


def run_hypothesis_b_validation(
    candles: Sequence[Mapping[str, Any]],
    liquidation_events: Sequence[Mapping[str, Any]],
    split_config: HypothesisBSplitConfig,
) -> dict[str, Any]:
    """Evaluate fixed parameters in fresh sequential validation and one holdout."""
    normalized_candles = _normalize_candles(candles)
    normalized_events = _normalize_events(liquidation_events)
    _validate_split(split_config, len(normalized_candles))
    total = len(normalized_candles)
    holdout_start = total - split_config.holdout_bars
    starts = list(range(split_config.development_bars, holdout_start, split_config.validation_step_bars))
    windows: list[dict[str, Any]] = []
    for window_index, start in enumerate(starts):
        end = start + split_config.validation_window_bars
        if end > holdout_start:
            raise ValueError("validation window overlaps the holdout")
        result = _run_normalized(normalized_candles[start:end], normalized_events)
        windows.append({"window_index": window_index, "boundaries": {"start_index": start, "end_index": end}, **result})
    holdout = _run_normalized(normalized_candles[holdout_start:], normalized_events)
    holdout["boundaries"] = {"start_index": holdout_start, "end_index": total}
    holdout["stop_decision"] = _stop_decision(holdout["summary"])
    return {
        "label": PAPER_LABEL,
        "split_config": asdict(split_config),
        "parameters_fitted_or_tuned": False,
        "methodology": "Fixed preregistered parameters; development is not fitted or tuned, validation windows are sequential, and holdout is evaluated once.",
        "split_boundaries": {
            "development": {"start_index": 0, "end_index": split_config.development_bars},
            "validation_region": {"start_index": split_config.development_bars, "end_index": holdout_start},
            "holdout": {"start_index": holdout_start, "end_index": total},
        },
        "counts": {"input_candles": total, "walk_forward_windows": len(windows), "holdout_evaluations": 1},
        "walk_forward": windows,
        "holdout": holdout,
    }


def _read_candles(path: Path) -> list[Mapping[str, Any]]:
    with path.open(encoding="utf-8") as source:
        records = json.load(source)
    if not isinstance(records, list):
        raise ValueError("candle JSON must contain a list of candle mappings")
    return records


def _read_events(path: Path) -> list[Mapping[str, Any]]:
    """Read either the dataset builder's normalized JSON array or normalized JSONL."""
    payload = path.read_text(encoding="utf-8").strip()
    if not payload:
        return []
    if payload.startswith("["):
        records = json.loads(payload)
        if not isinstance(records, list) or not all(isinstance(record, Mapping) for record in records):
            raise ValueError("normalized liquidation JSON must contain a list of mappings")
        return records
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        if line.strip():
            record = json.loads(line)
            if not isinstance(record, Mapping):
                raise ValueError(f"liquidation JSONL line {line_number} must be a mapping")
            records.append(record)
    return records


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run only local-file paper validation; this command never fetches or trades."""
    parser = argparse.ArgumentParser(description="PAPER/SIMULATION ONLY preregistered liquidation-exhaustion Hypothesis B")
    parser.add_argument("--candles", required=True, type=Path)
    parser.add_argument("--liquidations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--development-bars", required=True, type=int)
    parser.add_argument("--validation-window-bars", required=True, type=int)
    parser.add_argument("--validation-step-bars", required=True, type=int)
    parser.add_argument("--holdout-bars", required=True, type=int)
    args = parser.parse_args(argv)
    result = run_hypothesis_b_validation(
        _read_candles(args.candles),
        _read_events(args.liquidations),
        HypothesisBSplitConfig(
            development_bars=args.development_bars,
            validation_window_bars=args.validation_window_bars,
            validation_step_bars=args.validation_step_bars,
            holdout_bars=args.holdout_bars,
        ),
    )
    with args.output.open("w", encoding="utf-8") as destination:
        json.dump(result, destination, indent=2, sort_keys=True, allow_nan=False)
        destination.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
