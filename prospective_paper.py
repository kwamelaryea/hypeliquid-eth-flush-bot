"""Append-only prospective PAPER/SIMULATION ONLY ledger for Hypothesis B.

This local-file research infrastructure deliberately contains no exchange client,
network access, credentials, or live-order code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from liquidation_exhaustion import (
    BURST_CANDLES, BURST_NOTIONAL_USD, CANDLE_INTERVAL_MS,
    CONFIRMATION_SHARE_OF_AVERAGE, MAX_HOLD_BARS, PAPER_LABEL,
    RISK_PER_TRADE_USD, ROUND_TRIP_FEE_BPS, STOP_LOSS_PCT,
    TAKE_PROFIT_PCT, evaluate_exhaustion_signal,
)

HYPOTHESIS_NAME = "liquidation_exhaustion_hypothesis_b"
HYPOTHESIS_VERSION = "1.0"
RECORD_LABEL = "PAPER/SIMULATION ONLY"


@dataclass(frozen=True)
class LedgerPaths:
    """The four append-only journals belonging to one prospective ledger."""
    candles: Path
    events: Path
    decisions: Path
    trades: Path

    @classmethod
    def in_state_dir(cls, state_dir: Path) -> "LedgerPaths":
        return cls(state_dir / "completed_candles.jsonl", state_dir / "liquidation_events.jsonl",
                   state_dir / "signal_decisions.jsonl", state_dir / "simulated_trades.jsonl")


def frozen_parameters() -> dict[str, Any]:
    """Exact fixed constants copied from the preregistered Hypothesis B module."""
    return {
        "candle_interval_ms": CANDLE_INTERVAL_MS, "burst_candles": BURST_CANDLES,
        "burst_notional_usd": BURST_NOTIONAL_USD,
        "confirmation_share_of_burst_per_candle_average": CONFIRMATION_SHARE_OF_AVERAGE,
        "take_profit_pct": TAKE_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
        "risk_per_trade_usd": RISK_PER_TRADE_USD, "round_trip_fee_bps": ROUND_TRIP_FEE_BPS,
        "max_hold_bars": MAX_HOLD_BARS, "same_bar_exit_priority": "stop_loss",
    }


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_manifest(source: str) -> dict[str, Any]:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty dataset source string")
    fixed = {"label": RECORD_LABEL, "hypothesis_name": HYPOTHESIS_NAME,
             "hypothesis_version": HYPOTHESIS_VERSION, "dataset_source": source,
             "parameters": frozen_parameters()}
    return {**fixed, "config_hash": _hash(fixed)}


def ensure_manifest(path: Path, source: str) -> dict[str, Any]:
    """Create a frozen manifest or reject parameter/source/hash drift."""
    expected = build_manifest(source)
    if path.exists():
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid prospective manifest: {path}") from error
        if not isinstance(actual, Mapping) or dict(actual) != expected:
            raise ValueError("manifest/config hash drift: refusing to process prospective data")
        return dict(actual)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(expected, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return expected


def _num(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _ts(value: Any, field: str) -> int:
    result = _num(value, field)
    if not result.is_integer() or result < 0:
        raise ValueError(f"{field} must be a non-negative integer millisecond timestamp")
    return int(result)


def _candle(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"candle {index} must be a mapping")
    complete = raw.get("complete", raw.get("completed", True))
    if not isinstance(complete, bool):
        raise ValueError(f"candle {index}.complete must be boolean when supplied")
    if not complete:
        return {}
    try:
        value = {"ts": _ts(raw["ts"], f"candle {index}.ts"),
                 "open": _num(raw["open"], f"candle {index}.open"),
                 "high": _num(raw["high"], f"candle {index}.high"),
                 "low": _num(raw["low"], f"candle {index}.low"),
                 "close": _num(raw["close"], f"candle {index}.close")}
    except KeyError as error:
        raise ValueError(f"candle {index} is missing {error.args[0]}") from error
    if min(value["open"], value["high"], value["low"], value["close"]) <= 0 or value["high"] < value["low"]:
        raise ValueError(f"candle {index} has invalid OHLC values")
    return value


def _event(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"liquidation {index} must be a mapping")
    try:
        value = {"ts": _ts(raw["ts"], f"liquidation {index}.ts"),
                 "price": _num(raw["price"], f"liquidation {index}.price"),
                 "notional_usd": _num(raw["notional_usd"], f"liquidation {index}.notional_usd")}
    except KeyError as error:
        raise ValueError(f"liquidation {index} is missing {error.args[0]}") from error
    if value["price"] <= 0 or value["notional_usd"] <= 0:
        raise ValueError(f"liquidation {index} price and notional_usd must be positive")
    for name in ("event_id", "id", "trade_id"):
        if raw.get(name) is not None:
            value["event_id"] = f"{name}:{raw[name]}"
            break
    else:
        value["event_id"] = _hash(value)
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    output = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL in {path} at line {number}") from error
        if not isinstance(item, dict):
            raise ValueError(f"JSONL record in {path} at line {number} must be an object")
        output.append(item)
    return output


def _append(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    if records:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


def _ordered(items: Sequence[Mapping[str, Any]], name: str) -> None:
    if any(next_item["ts"] < item["ts"] for item, next_item in zip(items, items[1:])):
        raise ValueError(f"{name} batch must be ordered by timestamp")


def _record(kind: str, config_hash: str, **fields: Any) -> dict[str, Any]:
    return {"label": RECORD_LABEL, "record_type": kind, "config_hash": config_hash, **fields}


def _above(value: float, level: float) -> bool:
    return value > level or math.isclose(value, level, rel_tol=1e-12, abs_tol=1e-12)


def _below(value: float, level: float) -> bool:
    return value < level or math.isclose(value, level, rel_tol=1e-12, abs_tol=1e-12)


def _entry(decision: Mapping[str, Any], candle: Mapping[str, Any], index: int) -> dict[str, Any]:
    quantity = RISK_PER_TRADE_USD / (candle["open"] * STOP_LOSS_PCT)
    return {"trade_id": f"{decision['decision_id']}@{candle['ts']}", "entry_index": index,
            "entry_ts": candle["ts"], "entry_price": candle["open"], "quantity": quantity,
            "entry_notional": quantity * candle["open"], "take_profit": candle["open"] * (1 + TAKE_PROFIT_PCT),
            "stop_loss": candle["open"] * (1 - STOP_LOSS_PCT)}


def _close(position: Mapping[str, Any], candle: Mapping[str, Any], index: int, price: float, reason: str) -> dict[str, Any]:
    rate = ROUND_TRIP_FEE_BPS / 20_000.0
    exit_notional = position["quantity"] * price
    entry_fee, exit_fee = position["entry_notional"] * rate, exit_notional * rate
    gross = position["quantity"] * (price - position["entry_price"])
    return {"trade_id": position["trade_id"], "entry_ts": position["entry_ts"], "entry_price": position["entry_price"],
            "exit_ts": candle["ts"], "exit_price": price, "exit_reason": reason, "quantity": position["quantity"],
            "entry_notional": position["entry_notional"], "exit_notional": exit_notional,
            "risk_budget_usd": RISK_PER_TRADE_USD, "bars_held": index - position["entry_index"],
            "gross_pnl": gross, "entry_fee": entry_fee, "exit_fee": exit_fee, "fees": entry_fee + exit_fee,
            "net_pnl": gross - entry_fee - exit_fee}


def _exit(position: Mapping[str, Any], candle: Mapping[str, Any], index: int) -> Optional[dict[str, Any]]:
    if _above(candle["open"], position["take_profit"]): return _close(position, candle, index, candle["open"], "take_profit_gap_open")
    if _below(candle["open"], position["stop_loss"]): return _close(position, candle, index, candle["open"], "stop_loss_gap_open")
    if index - position["entry_index"] >= MAX_HOLD_BARS: return _close(position, candle, index, candle["open"], "max_hold_open")
    if _below(candle["low"], position["stop_loss"]) and _above(candle["high"], position["take_profit"]): return _close(position, candle, index, position["stop_loss"], "stop_loss_same_bar_priority")
    if _below(candle["low"], position["stop_loss"]): return _close(position, candle, index, position["stop_loss"], "stop_loss")
    if _above(candle["high"], position["take_profit"]): return _close(position, candle, index, position["take_profit"], "take_profit")
    return None


def _summary(closed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    net = sum(trade["net_pnl"] for trade in closed)
    wins = [trade["net_pnl"] for trade in closed if trade["net_pnl"] > 0]
    losses = [trade["net_pnl"] for trade in closed if trade["net_pnl"] < 0]
    infinite = not losses and bool(wins)
    pf = None if infinite else (sum(wins) / abs(sum(losses)) if losses else 0.0)
    expectancy = net / len(closed) if closed else None
    return {"label": RECORD_LABEL, "trades": len(closed), "wins": len(wins), "net_pnl": net,
            "cumulative_paper_pnl": net, "profit_factor": pf,
            "profit_factor_status": "infinite" if infinite else "finite", "net_expectancy": expectancy,
            "prospective_count": len(closed), "holdout_prospective_count": len(closed),
            "promotion_ready": len(closed) >= 100 and (infinite or pf >= 1.20) and expectancy is not None and expectancy > 0,
            "auto_promotion": False}


def run_prospective_paper(candles: Sequence[Mapping[str, Any]], liquidation_events: Sequence[Mapping[str, Any]], *, state_dir: Path, manifest: Path, source: str, paths: Optional[LedgerPaths] = None) -> dict[str, Any]:
    """Process an ordered local batch, preserving immutable past decisions.

    Late events are journaled but cannot alter an existing decision.  Replaying
    those immutable decisions reconstructs open positions and exit records.
    """
    active = ensure_manifest(manifest, source)
    state_dir.mkdir(parents=True, exist_ok=True)
    paths = paths or LedgerPaths.in_state_dir(state_dir)
    old_candle_records, old_event_records = _jsonl(paths.candles), _jsonl(paths.events)
    old_decisions, old_trade_records = _jsonl(paths.decisions), _jsonl(paths.trades)
    if any(item.get("config_hash") != active["config_hash"] for item in old_candle_records + old_event_records + old_decisions + old_trade_records):
        raise ValueError("ledger config hash differs from manifest: refusing to process")

    incoming_candles = [item for index, raw in enumerate(candles) if (item := _candle(raw, index))]
    incoming_events = [_event(raw, index) for index, raw in enumerate(liquidation_events)]
    _ordered(incoming_candles, "candle"); _ordered(incoming_events, "liquidation")
    old_candles = [{key: item[key] for key in ("ts", "open", "high", "low", "close")} for item in old_candle_records]
    old_events = [{key: item[key] for key in ("ts", "price", "notional_usd", "event_id")} for item in old_event_records]
    candle_by_ts = {item["ts"]: item for item in old_candles}
    new_candles = []
    for candle in incoming_candles:
        if candle["ts"] in candle_by_ts:
            if candle_by_ts[candle["ts"]] != candle: raise ValueError("conflicting candle for an already persisted timestamp")
        else: candle_by_ts[candle["ts"]] = candle; new_candles.append(candle)
    all_candles = sorted(candle_by_ts.values(), key=lambda item: item["ts"])
    if any(current["ts"] - prior["ts"] != CANDLE_INTERVAL_MS for prior, current in zip(all_candles, all_candles[1:])):
        raise ValueError("completed candle ledger must be contiguous 5m candles")
    events_by_id = {item["event_id"]: item for item in old_events}
    new_events = []
    for event in incoming_events:
        previous = events_by_id.get(event["event_id"])
        if previous is None: events_by_id[event["event_id"]] = event; new_events.append(event)
        elif previous != event: raise ValueError("conflicting liquidation event identity")
    all_events = sorted(events_by_id.values(), key=lambda item: (item["ts"], item["event_id"]))
    _append(paths.candles, [_record("completed_candle", active["config_hash"], **item) for item in new_candles])
    _append(paths.events, [_record("liquidation_event", active["config_hash"], **item) for item in new_events])

    by_confirmation = {item["confirmation_ts"]: item for item in old_decisions}
    entered = {item["trade_id"] for item in old_trade_records if item.get("record_type") == "simulated_trade_entry"}
    exited = {item["trade_id"] for item in old_trade_records if item.get("record_type") == "simulated_trade_exit"}
    decisions_out, trade_out, closed = [], [], []
    position = pending = None
    cumulative = 0.0
    for index, candle in enumerate(all_candles):
        if pending is not None:
            position = _entry(pending, candle, index)
            if position["trade_id"] not in entered:
                trade_out.append(_record("simulated_trade_entry", active["config_hash"], trade_id=position["trade_id"], decision_id=pending["decision_id"], entry_ts=position["entry_ts"], entry_price=position["entry_price"], quantity=position["quantity"], entry_notional=position["entry_notional"], take_profit=position["take_profit"], stop_loss=position["stop_loss"], cumulative_paper_pnl=cumulative))
            pending = None
        if position is not None and (closed_trade := _exit(position, candle, index)) is not None:
            cumulative += closed_trade["net_pnl"]
            if closed_trade["trade_id"] not in exited: trade_out.append(_record("simulated_trade_exit", active["config_hash"], cumulative_paper_pnl=cumulative, **closed_trade))
            closed.append(closed_trade); position = None
        if index < BURST_CANDLES: continue
        decision = by_confirmation.get(candle["ts"])
        if decision is None:
            # Existing public Hypothesis B signal function; no future candle/events are passed.
            signal = evaluate_exhaustion_signal(all_candles[:index + 1], [event for event in all_events if event["ts"] <= candle["ts"]], index)
            execution = "signal_denied" if not signal["eligible"] else ("position_open" if position is not None or pending is not None else "scheduled")
            decision = _record("signal_decision", active["config_hash"], decision_id=f"hypothesis-b:{candle['ts']}", confirmation_ts=candle["ts"], eligible=signal["eligible"], reason=signal["reason"], execution_decision=execution, current_trade_count=len(closed), signal=signal)
            by_confirmation[candle["ts"]] = decision; decisions_out.append(decision)
        if decision["execution_decision"] == "scheduled": pending = decision
    _append(paths.decisions, decisions_out); _append(paths.trades, trade_out)
    open_position = None if position is None else {**{key: position[key] for key in ("trade_id", "entry_ts", "entry_price", "take_profit", "stop_loss")}, "bars_held": len(all_candles) - 1 - position["entry_index"]}
    return {"label": PAPER_LABEL, "simulation_only": True, "manifest": active, "summary": _summary(closed), "open_position": open_position,
            "new_records": {"candles": len(new_candles), "events": len(new_events), "decisions": len(decisions_out), "trade_records": len(trade_out)}}


def _read_candles(path: Path) -> list[Mapping[str, Any]]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, list): raise ValueError("candle JSON must contain an array")
    return result


def _read_events(path: Path) -> list[Mapping[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text: return []
    result = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(result, list) or not all(isinstance(item, Mapping) for item in result): raise ValueError("liquidation input must contain objects")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PAPER/SIMULATION ONLY prospective Hypothesis B ledger")
    parser.add_argument("--candles", required=True, type=Path); parser.add_argument("--liquidations", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path); parser.add_argument("--manifest", required=True, type=Path); parser.add_argument("--source", required=True)
    parser.add_argument("--candles-ledger", type=Path); parser.add_argument("--events-ledger", type=Path)
    parser.add_argument("--decisions-ledger", type=Path); parser.add_argument("--trades-ledger", type=Path)
    args = parser.parse_args(argv); defaults = LedgerPaths.in_state_dir(args.state_dir)
    paths = LedgerPaths(args.candles_ledger or defaults.candles, args.events_ledger or defaults.events, args.decisions_ledger or defaults.decisions, args.trades_ledger or defaults.trades)
    print(json.dumps(run_prospective_paper(_read_candles(args.candles), _read_events(args.liquidations), state_dir=args.state_dir, manifest=args.manifest, source=args.source, paths=paths), sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__": raise SystemExit(main())
