"""Paper-only RSI control/treatment simulation harness.

This module has no exchange clients, credentials, or order execution.  It compares
an RSI-only long control against the same signal gated by *already observed*
normalized liquidation events.  It is a deterministic simulation, not evidence
of statistical significance or a recommendation to trade.

Candle ``ts`` is treated as the time its data is complete.  A close-based RSI
signal at candle N schedules an entry at candle N+1's open.  Long exits first
honour gaps at an open, then the max-hold exit at an open, then intrabar levels.
When both TP and SL occur in the same bar and their ordering is unknowable from
OHLC data, the stop loss wins (the conservative, documented assumption).
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


PAPER_LABEL = "PAPER/SIMULATION ONLY — no live orders or network access"


@dataclass(frozen=True)
class BaselineConfig:
    """Parameters for fixed-dollar-risk, long-only paper simulations.

    ``risk_per_trade_usd`` is the gross loss budget at the configured stop,
    before fees. ``round_trip_fee_bps`` is split equally between entry and exit
    and charged against each leg's actual notional. ``event_lookback_ms`` is
    measured backwards from the completed signal candle's timestamp.
    """

    rsi_period: int = 8
    rsi_oversold: float = 40.0
    event_lookback_ms: int = 300_000
    min_liquidation_notional_usd: float = 100_000.0
    take_profit_pct: float = 0.01
    stop_loss_pct: float = 0.01
    risk_per_trade_usd: float = 1.0
    round_trip_fee_bps: float = 10.0
    max_hold_bars: int = 12


@dataclass(frozen=True)
class WalkForwardConfig:
    """Explicit chronological split sizes for no-tuning validation."""

    train_bars: int
    validation_bars: int
    holdout_bars: int
    step_bars: int


@dataclass(frozen=True)
class _Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    rsi: Optional[float]


@dataclass(frozen=True)
class _LiquidationEvent:
    ts: float
    price: float
    notional_usd: float


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _optional_finite_number(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    return _finite_number(value, field)


def _validate_config(config: BaselineConfig) -> None:
    if config.rsi_period <= 0:
        raise ValueError("rsi_period must be positive")
    if config.event_lookback_ms < 0:
        raise ValueError("event_lookback_ms must be non-negative")
    if config.max_hold_bars <= 0:
        raise ValueError("max_hold_bars must be positive")
    for field in (
        "rsi_oversold",
        "min_liquidation_notional_usd",
        "take_profit_pct",
        "stop_loss_pct",
        "risk_per_trade_usd",
        "round_trip_fee_bps",
    ):
        value = _finite_number(getattr(config, field), field)
        if field != "rsi_oversold" and value < 0:
            raise ValueError(f"{field} must be non-negative")
    if config.take_profit_pct == 0 or config.stop_loss_pct == 0:
        raise ValueError("take_profit_pct and stop_loss_pct must be positive")
    if config.risk_per_trade_usd <= 0:
        raise ValueError("risk_per_trade_usd must be positive")


def _validate_walk_forward_config(config: WalkForwardConfig) -> None:
    for field in ("train_bars", "validation_bars", "holdout_bars", "step_bars"):
        value = getattr(config, field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")


def _normalize_candles(candles: Sequence[Mapping[str, Any]]) -> list[_Candle]:
    normalized: list[_Candle] = []
    previous_ts: Optional[float] = None
    for index, candle in enumerate(candles):
        if not isinstance(candle, Mapping):
            raise ValueError(f"candle {index} must be a mapping")
        try:
            normalized_candle = _Candle(
                ts=_finite_number(candle["ts"], f"candle {index}.ts"),
                open=_finite_number(candle["open"], f"candle {index}.open"),
                high=_finite_number(candle["high"], f"candle {index}.high"),
                low=_finite_number(candle["low"], f"candle {index}.low"),
                close=_finite_number(candle["close"], f"candle {index}.close"),
                rsi=_optional_finite_number(candle.get("rsi"), f"candle {index}.rsi"),
            )
        except KeyError as error:
            raise ValueError(f"candle {index} is missing {error.args[0]}") from error
        if min(
            normalized_candle.open,
            normalized_candle.high,
            normalized_candle.low,
            normalized_candle.close,
        ) <= 0:
            raise ValueError(f"candle {index} prices must be positive")
        if normalized_candle.high < normalized_candle.low:
            raise ValueError(f"candle {index} high must be at least low")
        if previous_ts is not None and normalized_candle.ts <= previous_ts:
            raise ValueError("candles must be strictly ordered by ts")
        normalized.append(normalized_candle)
        previous_ts = normalized_candle.ts
    return normalized


def _normalize_events(events: Sequence[Mapping[str, Any]]) -> list[_LiquidationEvent]:
    """Keep only events with explicit normalized dollar notional.

    Raw exchange fields such as ``sz`` and liquidation side are intentionally
    never interpreted here: callers must provide ``notional_usd`` themselves.
    """

    normalized: list[_LiquidationEvent] = []
    for event in events:
        if not isinstance(event, Mapping) or "notional_usd" not in event:
            continue
        try:
            normalized_event = _LiquidationEvent(
                ts=_finite_number(event["ts"], "liquidation.ts"),
                price=_finite_number(event["price"], "liquidation.price"),
                notional_usd=_finite_number(event["notional_usd"], "liquidation.notional_usd"),
            )
        except (KeyError, ValueError):
            continue
        if normalized_event.price <= 0 or normalized_event.notional_usd <= 0:
            continue
        normalized.append(normalized_event)
    return normalized


def calculate_rsi(candles: Sequence[Mapping[str, Any]], period: int = 8) -> list[Optional[float]]:
    """Return Wilder RSI values aligned to candles, with ``None`` before seed.

    This public helper calculates from close prices only and deliberately does
    not consume a supplied ``rsi`` field. ``run_paper_baseline`` uses supplied,
    finite per-candle RSI values when present and otherwise uses these values.
    """

    if period <= 0:
        raise ValueError("period must be positive")
    closes = [
        _finite_number(candle["close"], f"candle {index}.close")
        for index, candle in enumerate(candles)
    ]
    values: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return values

    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, len(closes))]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, len(closes))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def rsi_from_averages(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        return 100.0 - (100.0 / (1.0 + gain / loss))

    values[period] = rsi_from_averages(average_gain, average_loss)
    for gain, loss, index in zip(gains[period:], losses[period:], range(period + 1, len(closes))):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
        values[index] = rsi_from_averages(average_gain, average_loss)
    return values


def _at_or_above(value: float, level: float) -> bool:
    return value > level or math.isclose(value, level, rel_tol=1e-12, abs_tol=1e-12)


def _at_or_below(value: float, level: float) -> bool:
    return value < level or math.isclose(value, level, rel_tol=1e-12, abs_tol=1e-12)


def _eligible_events(
    events: Sequence[_LiquidationEvent], candle: _Candle, config: BaselineConfig
) -> list[_LiquidationEvent]:
    oldest_ts = candle.ts - config.event_lookback_ms
    return [
        event
        for event in events
        if oldest_ts <= event.ts <= candle.ts
        and event.notional_usd >= config.min_liquidation_notional_usd
        and event.price <= candle.close
    ]


def _close_trade(
    position: Mapping[str, Any],
    candle: Mapping[str, Any],
    exit_price: float,
    exit_reason: str,
    config: BaselineConfig,
) -> dict[str, Any]:
    fee_rate_per_leg = config.round_trip_fee_bps / 20_000.0
    quantity = position["quantity"]
    entry_notional = position["entry_notional"]
    exit_notional = quantity * exit_price
    entry_fee = entry_notional * fee_rate_per_leg
    exit_fee = exit_notional * fee_rate_per_leg
    gross_pnl = quantity * (exit_price - position["entry_price"])
    fees = entry_fee + exit_fee
    return {
        "entry_ts": position["entry_ts"],
        "entry_price": position["entry_price"],
        "exit_ts": candle["ts"],
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "quantity": quantity,
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "risk_budget_usd": config.risk_per_trade_usd,
        "bars_held": candle["index"] - position["entry_index"],
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": fees,
        "net_pnl": gross_pnl - fees,
    }


def _summary(trades: Sequence[dict[str, Any]], eligible: int, rejected: int) -> dict[str, Any]:
    gross_pnl = sum(trade["gross_pnl"] for trade in trades)
    fees = sum(trade["fees"] for trade in trades)
    net_pnl = sum(trade["net_pnl"] for trade in trades)
    net_wins = sum(trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0)
    net_losses = sum(trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0)
    profit_factor: Optional[float]
    if net_losses < 0:
        profit_factor = net_wins / abs(net_losses)
    elif net_wins > 0:
        profit_factor = None
    else:
        profit_factor = 0.0

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        equity += trade["net_pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return {
        "trades": len(trades),
        "wins": sum(1 for trade in trades if trade["net_pnl"] > 0),
        "gross_pnl": gross_pnl,
        "fees": fees,
        "net_pnl": net_pnl,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "eligible_treatment_signals": eligible,
        "rejected_treatment_signals": rejected,
    }


def _run_variant(
    mode: str,
    candles: Sequence[_Candle],
    rsi_values: Sequence[Optional[float]],
    events: Sequence[_LiquidationEvent],
    config: BaselineConfig,
) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    position: Optional[dict[str, Any]] = None
    pending_entry: Optional[dict[str, Any]] = None
    eligible_signals = 0
    rejected_signals = 0

    for index, candle in enumerate(candles):
        candle_data = {
            "index": index,
            "ts": candle.ts,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }
        if pending_entry is not None:
            quantity = config.risk_per_trade_usd / (candle.open * config.stop_loss_pct)
            position = {
                "entry_index": index,
                "entry_ts": candle.ts,
                "entry_price": candle.open,
                "quantity": quantity,
                "entry_notional": quantity * candle.open,
                "take_profit": candle.open * (1.0 + config.take_profit_pct),
                "stop_loss": candle.open * (1.0 - config.stop_loss_pct),
            }
            pending_entry = None

        if position is not None:
            # Gaps are known at the open; max hold exits at the open before any
            # unknown intrabar path. Otherwise SL wins if both levels touch.
            if _at_or_above(candle.open, position["take_profit"]):
                trades.append(_close_trade(position, candle_data, candle.open, "take_profit_gap_open", config))
                position = None
            elif _at_or_below(candle.open, position["stop_loss"]):
                trades.append(_close_trade(position, candle_data, candle.open, "stop_loss_gap_open", config))
                position = None
            elif index - position["entry_index"] >= config.max_hold_bars:
                trades.append(_close_trade(position, candle_data, candle.open, "max_hold_open", config))
                position = None
            elif _at_or_below(candle.low, position["stop_loss"]) and _at_or_above(candle.high, position["take_profit"]):
                trades.append(_close_trade(position, candle_data, position["stop_loss"], "stop_loss_same_bar_priority", config))
                position = None
            elif _at_or_below(candle.low, position["stop_loss"]):
                trades.append(_close_trade(position, candle_data, position["stop_loss"], "stop_loss", config))
                position = None
            elif _at_or_above(candle.high, position["take_profit"]):
                trades.append(_close_trade(position, candle_data, position["take_profit"], "take_profit", config))
                position = None

        rsi = rsi_values[index]
        if position is not None or pending_entry is not None or rsi is None or rsi > config.rsi_oversold:
            continue
        if index == len(candles) - 1:
            continue

        qualifying_events = _eligible_events(events, candle, config) if mode == "treatment" else []
        accepted = mode == "control" or bool(qualifying_events)
        decision = {
            "mode": mode,
            "signal_ts": candle.ts,
            "signal_price": candle.close,
            "rsi": rsi,
            "entry_ts": candles[index + 1].ts,
            "entry_price": candles[index + 1].open,
            "eligible_event_count": len(qualifying_events),
            "decision": "scheduled" if accepted else "rejected",
            "reason": "rsi_oversold" if accepted and mode == "control" else (
                "eligible_liquidation" if accepted else "no_eligible_liquidation"
            ),
        }
        decisions.append(decision)
        if mode == "treatment":
            if accepted:
                eligible_signals += 1
            else:
                rejected_signals += 1
        if accepted:
            pending_entry = decision

    open_position = None
    if position is not None:
        open_position = {
            "entry_ts": position["entry_ts"],
            "entry_price": position["entry_price"],
            "take_profit": position["take_profit"],
            "stop_loss": position["stop_loss"],
            "bars_held": len(candles) - 1 - position["entry_index"],
        }
    return {
        "decisions": decisions,
        "trades": trades,
        "open_position": open_position,
        "summary": _summary(trades, eligible_signals, rejected_signals),
    }


def run_paper_baseline(
    candles: Sequence[Mapping[str, Any]],
    liquidation_events: Sequence[Mapping[str, Any]],
    config: Optional[BaselineConfig] = None,
) -> dict[str, Any]:
    """Simulate paper-only control and gated treatment variants.

    Events without a finite, explicit ``notional_usd`` are ignored. An eligible
    treatment event must have already occurred in the lookback window, meet the
    minimum normalized notional, and have price at or below the signal close.
    Quantity is sized so a stop-level exit loses ``risk_per_trade_usd`` before
    fees; all PnL and fees use actual notionals.
    Positions still open at end-of-data are reported but excluded from summary.
    """

    active_config = config or BaselineConfig()
    _validate_config(active_config)
    normalized_candles = _normalize_candles(candles)
    computed_rsi = calculate_rsi(candles, active_config.rsi_period)
    rsi_values = [
        candle.rsi if candle.rsi is not None else computed_rsi[index]
        for index, candle in enumerate(normalized_candles)
    ]
    normalized_events = _normalize_events(liquidation_events)
    return {
        "label": PAPER_LABEL,
        "config": asdict(active_config),
        "control": _run_variant("control", normalized_candles, rsi_values, normalized_events, active_config),
        "treatment": _run_variant("treatment", normalized_candles, rsi_values, normalized_events, active_config),
    }


def _aggregate_variants(variants: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate closed trades from reported validation windows only."""

    trades = [trade for variant in variants for trade in variant["trades"]]
    eligible = sum(variant["summary"]["eligible_treatment_signals"] for variant in variants)
    rejected = sum(variant["summary"]["rejected_treatment_signals"] for variant in variants)
    return _summary(trades, eligible, rejected)


def run_walk_forward_validation(
    candles: Sequence[Mapping[str, Any]],
    liquidation_events: Sequence[Mapping[str, Any]],
    split_config: WalkForwardConfig,
    config: Optional[BaselineConfig] = None,
) -> dict[str, Any]:
    """Run no-tuning chronological validation followed by one untouched holdout.

    The development portion is only a named boundary: this function does not
    select, fit, or tune any parameter. Each validation window and the holdout
    start with no position. Events later than a segment's final candle are
    removed before that segment is simulated, in addition to the normal
    signal-time eligibility check.
    """

    active_config = config or BaselineConfig()
    _validate_config(active_config)
    _validate_walk_forward_config(split_config)
    normalized_candles = _normalize_candles(candles)
    total_bars = len(normalized_candles)
    required_bars = (
        split_config.train_bars + split_config.validation_bars + split_config.holdout_bars
    )
    if total_bars < required_bars:
        raise ValueError(
            "insufficient candles for walk-forward validation: "
            f"need at least {required_bars}, got {total_bars}"
        )

    holdout_start = total_bars - split_config.holdout_bars
    validation_starts = list(
        range(
            split_config.train_bars,
            holdout_start - split_config.validation_bars + 1,
            split_config.step_bars,
        )
    )
    if not validation_starts:
        raise ValueError("insufficient candles to produce a validation window before holdout")

    computed_rsi = calculate_rsi(candles, active_config.rsi_period)
    rsi_values = [
        candle.rsi if candle.rsi is not None else computed_rsi[index]
        for index, candle in enumerate(normalized_candles)
    ]
    normalized_events = _normalize_events(liquidation_events)

    def run_segment(start_index: int, end_index: int) -> dict[str, Any]:
        segment_candles = normalized_candles[start_index:end_index]
        # No event after this segment can affect its results, even accidentally.
        segment_events = [event for event in normalized_events if event.ts <= segment_candles[-1].ts]
        segment_rsi = rsi_values[start_index:end_index]
        return {
            "control": _run_variant(
                "control", segment_candles, segment_rsi, segment_events, active_config
            ),
            "treatment": _run_variant(
                "treatment", segment_candles, segment_rsi, segment_events, active_config
            ),
        }

    walk_forward: list[dict[str, Any]] = []
    for window_index, start_index in enumerate(validation_starts):
        end_index = start_index + split_config.validation_bars
        result = run_segment(start_index, end_index)
        walk_forward.append(
            {
                "window_index": window_index,
                "boundaries": {"start_index": start_index, "end_index": end_index},
                **result,
            }
        )

    holdout_result = run_segment(holdout_start, total_bars)
    control_windows = [window["control"] for window in walk_forward]
    treatment_windows = [window["treatment"] for window in walk_forward]
    return {
        "label": PAPER_LABEL,
        "config": asdict(active_config),
        "split_config": asdict(split_config),
        "parameters_fitted_or_tuned": False,
        "methodology": (
            "No parameters were fitted or tuned by this harness. Validation windows are "
            "chronological and the final holdout is evaluated exactly once."
        ),
        "split_boundaries": {
            "development": {"start_index": 0, "end_index": split_config.train_bars},
            "validation_region": {"start_index": split_config.train_bars, "end_index": holdout_start},
            "holdout": {"start_index": holdout_start, "end_index": total_bars},
        },
        "counts": {
            "input_candles": total_bars,
            "walk_forward_windows": len(walk_forward),
            "holdout_evaluations": 1,
        },
        "walk_forward": walk_forward,
        "aggregate": {
            "control": _aggregate_variants(control_windows),
            "treatment": _aggregate_variants(treatment_windows),
        },
        "holdout": {
            "boundaries": {"start_index": holdout_start, "end_index": total_bars},
            **holdout_result,
        },
    }


def _read_candles(path: Path) -> list[Mapping[str, Any]]:
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, list):
        raise ValueError("candle JSON must contain a list of candle mappings")
    return payload


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, Mapping):
                raise ValueError(f"liquidation JSONL line {line_number} must be a mapping")
            events.append(record)
    return events


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Read local files and write stable paper-only JSON; never fetches network data."""

    parser = argparse.ArgumentParser(description="PAPER/SIMULATION ONLY RSI baseline comparison")
    parser.add_argument("--candles", required=True, type=Path, help="path to candle JSON array")
    parser.add_argument("--liquidations", required=True, type=Path, help="path to normalized liquidation JSONL")
    parser.add_argument("--output", required=True, type=Path, help="path for result JSON")
    parser.add_argument("--rsi-period", type=int, default=8)
    parser.add_argument("--rsi-oversold", type=float, default=40.0)
    parser.add_argument("--event-lookback-ms", type=int, default=300_000)
    parser.add_argument("--min-liquidation-notional-usd", type=float, default=100_000.0)
    parser.add_argument("--take-profit-pct", type=float, default=0.01)
    parser.add_argument("--stop-loss-pct", type=float, default=0.01)
    parser.add_argument("--risk-per-trade-usd", type=float, default=1.0)
    parser.add_argument("--round-trip-fee-bps", type=float, default=10.0)
    parser.add_argument("--max-hold-bars", type=int, default=12)
    parser.add_argument("--train-bars", type=int, help="walk-forward development bars")
    parser.add_argument("--validation-bars", type=int, help="walk-forward validation window bars")
    parser.add_argument("--holdout-bars", type=int, help="walk-forward final untouched holdout bars")
    parser.add_argument("--step-bars", type=int, help="walk-forward validation window step bars")
    args = parser.parse_args(argv)
    config = BaselineConfig(
        rsi_period=args.rsi_period,
        rsi_oversold=args.rsi_oversold,
        event_lookback_ms=args.event_lookback_ms,
        min_liquidation_notional_usd=args.min_liquidation_notional_usd,
        take_profit_pct=args.take_profit_pct,
        stop_loss_pct=args.stop_loss_pct,
        risk_per_trade_usd=args.risk_per_trade_usd,
        round_trip_fee_bps=args.round_trip_fee_bps,
        max_hold_bars=args.max_hold_bars,
    )
    split_values = (args.train_bars, args.validation_bars, args.holdout_bars, args.step_bars)
    if any(value is not None for value in split_values) and any(value is None for value in split_values):
        parser.error("--train-bars, --validation-bars, --holdout-bars, and --step-bars must be supplied together")
    candle_data = _read_candles(args.candles)
    liquidation_data = _read_jsonl(args.liquidations)
    if all(value is not None for value in split_values):
        result = run_walk_forward_validation(
            candle_data,
            liquidation_data,
            WalkForwardConfig(
                train_bars=args.train_bars,
                validation_bars=args.validation_bars,
                holdout_bars=args.holdout_bars,
                step_bars=args.step_bars,
            ),
            config,
        )
    else:
        result = run_paper_baseline(candle_data, liquidation_data, config)
    with args.output.open("w", encoding="utf-8") as destination:
        json.dump(result, destination, indent=2, sort_keys=True, allow_nan=False)
        destination.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
