#!/usr/bin/env python3
"""
Autonomous Multi-Agent Weekly Review — HL ETH Leverage Flush Bot
Inspired by FinRobot's investment group pattern (AI4Finance-Foundation/FinRobot).

Agent roles:
  1. Risk Analyst      — drawdown, Sharpe, position sizing compliance
  2. Strategy Analyst  — signal quality, filter effectiveness, win/loss by strategy
  3. Market Analyst    — regime detection accuracy, ETH market structure
  4. CIO               — synthesis, self-critique (reflection), parameter decision

Self-critique loop (from FinRobot's reflection_with_llm pattern):
  After CIO produces initial synthesis, a second reflection pass checks for
  confirmation bias, hindsight bias, and insufficient-data errors before
  committing any config change.
"""

import anthropic
import ast
import json
import re
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─── PATHS ────────────────────────────────────────────────────────────────────

REPO = Path(__file__).parent
LOG_FILE         = REPO / "logs" / "hyperliquid_trading.log"
LESSONS_FILE     = REPO / "LESSONS.md"
CONFIG_FILE      = REPO / "config_dex.py"
LIQUIDATION_FILE = REPO / "data" / "okx_liquidations.jsonl"
STRATEGY_FILE    = REPO / "strategy.yaml"
REVIEW_STATUS_URL = os.getenv("REVIEW_STATUS_URL", "")

TODAY  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

# Model tiers — analysts use Haiku (fast+cheap), CIO uses Sonnet, reflection uses Opus (final gate)
MODEL_ANALYST  = "claude-haiku-4-5-20251001"
MODEL_CIO      = "claude-sonnet-4-6"
MODEL_CRITIQUE = "claude-opus-4-6"

client = anthropic.Anthropic()

# ─── DATA EXTRACTION ──────────────────────────────────────────────────────────

REVIEW_LOG_URL = os.getenv("REVIEW_LOG_URL", "")

# Cutoff: only analyse trades from this many days ago (one full week + buffer)
REVIEW_WINDOW_DAYS = 8

def _authorized_request(url: str) -> urllib.request.Request:
    headers = {}
    token = os.getenv("OPERATIONAL_API_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _fetch_fly_data() -> tuple[list[str], str]:
    """
    Fetch structured trade CSV lines + raw log from the live Fly.io /api/log endpoint.
    Returns (trade_lines, raw_log_text).
    """
    try:
        with urllib.request.urlopen(_authorized_request(REVIEW_LOG_URL), timeout=15) as resp:
            data = json.loads(resp.read())
        trades = data.get("trades", [])
        raw    = "\n".join(data.get("lines", []))
        return trades, raw
    except Exception:
        pass

    # Fallback: /api/status buffer only
    try:
        with urllib.request.urlopen(_authorized_request(REVIEW_STATUS_URL), timeout=10) as resp:
            data = json.loads(resp.read())
        entries = data.get("logs", [])
        raw = "\n".join(f"{e.get('time','')} | {e.get('msg','')}" for e in entries)
        return [], raw
    except Exception as e:
        return [], f"(log fetch failed: {e})"


def load_log(tail: int = 800) -> str:
    _, raw = _fetch_fly_data()
    if raw:
        return raw
    # Local fallback (stale after Mar 2026 — bot moved to Fly)
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(errors="replace").splitlines()
        return "\n".join(lines[-tail:])
    return "(no log data available)"


def parse_trades(log: str) -> list[dict]:
    """
    Parse closed trades from /data/trades.log CSV lines fetched via /api/log.
    Format: timestamp,event,side,price,pnl_pct,amount_usd,balance[,strategy]
    Only returns CLOSE events within REVIEW_WINDOW_DAYS.
    Falls back to regex parsing of raw log if no structured CSV available.
    """
    from datetime import timedelta
    trade_lines, _ = _fetch_fly_data()

    # ── Structured CSV path (preferred) ────────────────────────────────────────
    if trade_lines:
        trades: list[dict] = []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=REVIEW_WINDOW_DAYS)

        open_by_side: dict[str, dict] = {}  # side → last OPEN record

        for raw in trade_lines:
            parts = raw.strip().split(",")
            if len(parts) < 6:
                continue
            try:
                ts_str, event, side = parts[0], parts[1], parts[2]
                price    = float(parts[3])
                pnl_pct  = float(parts[4])
                balance  = float(parts[6]) if len(parts) > 6 else 0.0
                strategy = parts[7].strip() if len(parts) > 7 else "unknown"

                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

                if event.startswith("OPEN"):
                    open_by_side[side] = {
                        "direction":   side,
                        "entry_price": price,
                        "open_time":   ts_str,
                        "strategy":    strategy,
                    }
                elif event.startswith("CLOSE") and ts >= cutoff:
                    trade = dict(open_by_side.get(side, {}))
                    # CLOSE strategy fields contain the exit reason, not the
                    # entry strategy. Preserve OPEN attribution for analysis.
                    entry_strategy = trade.get("strategy", strategy)
                    trade.update({
                        "close_price": price,
                        "pnl_pct":     round(pnl_pct, 2),
                        "win":         pnl_pct > 0,
                        "exit_reason": event,
                        "close_time":  ts_str,
                        "balance_after": balance,
                        "strategy":    entry_strategy,
                    })
                    trades.append(trade)
            except (ValueError, IndexError):
                continue
        return trades

    # ── Regex fallback (raw log) ────────────────────────────────────────────────
    trades_rx: list[dict] = []
    current: dict | None = None
    for line in log.splitlines():
        ll = line.lower()
        m_open = re.search(r"(long|short)\s+opened.*?\$([\d,.]+).*?size[:\s]*([\d.]+)", ll)
        if m_open:
            current = {
                "direction":   m_open.group(1).upper(),
                "entry_price": float(m_open.group(2).replace(",", "")),
                "size":        float(m_open.group(3)),
                "open_time":   line[:19] if len(line) > 19 else "unknown",
                "strategy":    "trend-follow" if "trend" in ll else "flush",
            }
            continue
        if current is None:
            continue
        if "take profit" in ll or "tp hit" in ll:
            m_px = re.search(r"\$([\d,.]+)", line)
            _close(current, trades_rx, m_px, "TP")
            current = None
        elif "stop loss" in ll or "sl hit" in ll:
            m_px = re.search(r"\$([\d,.]+)", line)
            _close(current, trades_rx, m_px, "SL")
            current = None

    return trades_rx


def _close(trade: dict, trades: list, m_px, reason: str):
    close_px = float(m_px.group(1).replace(",", "")) if m_px else 0.0
    trade["close_price"] = close_px
    trade["exit_reason"] = reason
    entry = trade.get("entry_price", 0)
    if entry > 0 and close_px > 0:
        pnl = (close_px - entry) / entry * 100
        if trade["direction"] == "SHORT":
            pnl = -pnl
        trade["pnl_pct"] = round(pnl, 2)
        trade["win"] = pnl > 0
    trades.append(trade)


def compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "trade_count": 0, "win_rate": 0, "avg_win": 0,
            "avg_loss": 0, "total_pnl_pct": 0, "max_loss": 0,
            "by_strategy": {"flush": {"count": 0, "wins": 0},
                            "trend-follow": {"count": 0, "wins": 0}},
        }
    wins   = [t for t in trades if t.get("win")]
    losses = [t for t in trades if not t.get("win")]
    pnls   = [t["pnl_pct"] for t in trades if "pnl_pct" in t]

    def strat(name):
        ts = [t for t in trades if t.get("strategy") == name]
        return {"count": len(ts), "wins": sum(1 for t in ts if t.get("win"))}

    return {
        "trade_count":   len(trades),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "avg_win":       round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0,
        "avg_loss":      round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0,
        "total_pnl_pct": round(sum(pnls), 2),
        "max_loss":      round(min(pnls), 2) if pnls else 0,
        "by_strategy":   {"flush": strat("flush"), "trend-follow": strat("trend-follow")},
    }


def compute_kelly() -> dict:
    """
    Compute Kelly criterion per strategy from the full trade log on Fly.io.
    Uses ALL available history (not just REVIEW_WINDOW_DAYS) for statistical validity.
    Returns dict: strategy → {trades, win_rate, kelly_pct, half_kelly_pct, profit_factor, has_edge}
    """
    from collections import defaultdict
    trade_lines, _ = _fetch_fly_data()
    if not trade_lines:
        return {}

    trades_by_strat: dict[str, list[float]] = defaultdict(list)
    pending: dict[str, str] = {}

    for raw in trade_lines:
        parts = raw.strip().split(",")
        if len(parts) < 5:
            continue
        try:
            event   = parts[1]
            side    = parts[2]
            pnl     = float(parts[4])
            strategy = parts[7].strip() if len(parts) > 7 else ""
            strategy = strategy or "unknown"

            if event.startswith("OPEN"):
                pending[side] = strategy
            elif event.startswith("CLOSE") and pnl != 0.0:
                strat = pending.pop(side, strategy)
                trades_by_strat[strat].append(pnl)
        except (ValueError, IndexError):
            continue

    results: dict = {}
    for strat, pnls in trades_by_strat.items():
        if len(pnls) < 3:
            continue
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls)
        avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.001
        R        = avg_win / avg_loss if avg_loss > 0 else 0.0
        kelly    = (win_rate - (1 - win_rate) / R) if R > 0 else -1.0
        pf       = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 0.0
        results[strat] = {
            "trades":         len(pnls),
            "win_rate_pct":   round(win_rate * 100, 1),
            "avg_win_pct":    round(avg_win, 3),
            "avg_loss_pct":   round(avg_loss, 3),
            "rr_ratio":       round(R, 2),
            "profit_factor":  round(pf, 2),
            "kelly_pct":      round(kelly * 100, 1),
            "half_kelly_pct": round(kelly * 50, 1),
            "has_edge":       kelly > 0.0,
        }
    return results


def load_liquidation_clusters() -> str:
    if not LIQUIDATION_FILE.exists():
        return "(liquidation data unavailable — cloud-only artifact)"
    lines = LIQUIDATION_FILE.read_text(errors="replace").splitlines()[-300:]
    clusters: dict[int, float] = {}
    for raw in lines:
        try:
            rec = json.loads(raw)
            px  = round(float(rec.get("price", 0)) / 50) * 50
            val = float(rec.get("notional", rec.get("value", rec.get("usd_size", 0))))
            clusters[px] = clusters.get(px, 0) + val
        except Exception:
            continue
    top = sorted(clusters.items(), key=lambda x: x[1], reverse=True)[:10]
    return json.dumps([{"price": p, "notional_usd": round(n)} for p, n in top], indent=2)


# ─── AGENT FACTORY ────────────────────────────────────────────────────────────

def call(system: str, user: str, max_tokens: int = 600, model: str = MODEL_ANALYST) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


# ─── AGENT 1 — RISK ANALYST ───────────────────────────────────────────────────

def risk_analyst(trades, metrics, config, log, kelly: dict) -> str:
    kelly_summary = json.dumps(kelly, indent=2) if kelly else "(insufficient trade data)"
    return call(
        system=(
            "You are a quantitative risk analyst reviewing a crypto perpetual trading bot. "
            "Be direct and numerical. Flag violations clearly. 250 words max."
        ),
        user=f"""Weekly closed trades:
{json.dumps(trades, indent=2) if trades else "No closed trades this week."}

Performance metrics:
{json.dumps(metrics, indent=2)}

Kelly Criterion analysis (ALL-TIME, per strategy):
{kelly_summary}
Kelly interpretation: positive kelly_pct = strategy has mathematical edge.
Negative kelly_pct = strategy destroys capital long-term. has_edge=false = disable or restructure.
Half-Kelly is the recommended max position size as % of equity risk per trade.

Risk parameters from config:
STOP_LOSS_PCT = {_extract(config, 'STOP_LOSS_PCT')}
TAKE_PROFIT_PCT = {_extract(config, 'TAKE_PROFIT_PCT')}
TRADE_PERCENT = {_extract(config, 'TRADE_PERCENT')}
LEVERAGE = {_extract(config, 'LEVERAGE')}
REENTRY_COOLDOWN_SECONDS = {_extract(config, 'REENTRY_COOLDOWN_SECONDS')}
TREND_FOLLOW_SL_PCT = {_extract(config, 'TREND_FOLLOW_SL_PCT')}
TREND_FOLLOW_TP_PCT = {_extract(config, 'TREND_FOLLOW_TP_PCT')}
RSI_EXIT_OVERBOUGHT = {_extract(config, 'RSI_EXIT_OVERBOUGHT')}

Recent log (last 2000 chars):
```
{log[-2000:]}
```

Assess:
1. Max drawdown vs STOP_LOSS_PCT target — compliant?
2. Position sizing within TRADE_PERCENT limits?
3. Which strategies have positive Kelly? Which should be disabled?
4. Is position sizing aligned with Kelly recommendation? (actual risk per trade = SL_PCT × TRADE_PERCENT/100)
5. Risk verdict: PASS / WARNING / VIOLATION with 1-line reason.
6. If WARNING or VIOLATION — one specific corrective action.""",
        max_tokens=600,
    )


# ─── AGENT 2 — STRATEGY ANALYST ───────────────────────────────────────────────

def strategy_analyst(trades, metrics, config, log) -> str:
    strategy_doc = STRATEGY_FILE.read_text() if STRATEGY_FILE.exists() else "(no strategy.yaml)"
    return call(
        system=(
            "You are a trading strategy analyst. Focus on signal quality and filter effectiveness. "
            "Reference specific log lines when possible. 200 words max."
        ),
        user=f"""Weekly trades:
{json.dumps(trades, indent=2) if trades else "No closed trades this week."}

Performance by strategy:
{json.dumps(metrics.get('by_strategy', {}), indent=2)}

Key signal filters:
RSI_OVERSOLD = {_extract(config, 'RSI_OVERSOLD')}
RSI_OVERBOUGHT = {_extract(config, 'RSI_OVERBOUGHT')}
RSI_OVERSOLD_DOWNTREND = {_extract(config, 'RSI_OVERSOLD_DOWNTREND')}
VOLUME_CONFIRMATION_MULT = {_extract(config, 'VOLUME_CONFIRMATION_MULT')}
USE_VOLUME_FILTER = {_extract(config, 'USE_VOLUME_FILTER')}
USE_OI_FILTER = {_extract(config, 'USE_OI_FILTER')}
TREND_FOLLOW_RSI_MIN = {_extract(config, 'TREND_FOLLOW_RSI_MIN')}
TREND_FOLLOW_RSI_MAX = {_extract(config, 'TREND_FOLLOW_RSI_MAX')}
TREND_FOLLOW_EMA_PROXIMITY_ATR = {_extract(config, 'TREND_FOLLOW_EMA_PROXIMITY_ATR')}

Strategy doc:
{strategy_doc[:800]}

Log (last 3000 chars):
```
{log[-3000:]}
```

Assess:
1. Did flush or trend-follow strategy perform better this week and why?
2. Were there false entries (signals fired but price moved against)?
3. Were there missed opportunities (filters too tight)?
4. ONE specific parameter tweak with evidence — or NONE if insufficient data (<3 trades is insufficient).""",
        max_tokens=600,
    )


# ─── AGENT 3 — MARKET ANALYST ─────────────────────────────────────────────────

def market_analyst(log, clusters) -> str:
    return call(
        system=(
            "You are a market structure analyst. Relate the bot's activity to ETH's market regime "
            "and liquidation cluster data. 200 words max."
        ),
        user=f"""Bot log (last 3000 chars):
```
{log[-3000:]}
```

Top liquidation clusters by notional (from OKX/Binance/Bybit data):
{clusters}

Assess:
1. ETH regime this week (uptrend / downtrend / range) — cite log evidence (EMA, price lines).
2. Did the bot's regime detection (Regime= log lines) match actual market behavior?
3. Were liquidation clusters at useful price levels for entries? Any major clusters the bot may have missed?
4. Any macro or structural factor (high VIX, low volume, range compression) that constrained performance?""",
        max_tokens=500,
    )


# ─── AGENT 4 — CIO (SYNTHESIS + REFLECTION) ───────────────────────────────────

def cio(risk, strategy, market, lessons, config, metrics, kelly: dict) -> tuple[str, str]:
    # Initial synthesis — Sonnet (quality synthesis without Opus cost)
    synthesis = call(
        system=(
            "You are the CIO of a crypto trading system. Synthesize multi-agent findings into "
            "a clear weekly decision. 250 words max."
        ),
        user=f"""RISK ANALYST:
{risk}

STRATEGY ANALYST:
{strategy}

MARKET ANALYST:
{market}

LESSONS HISTORY (last 600 chars):
{lessons[-600:]}

WEEKLY METRICS SUMMARY:
- Trades: {metrics.get('trade_count', 0)} | Win rate: {metrics.get('win_rate', 0)}% | Total PnL: {metrics.get('total_pnl_pct', 0)}%
- Max loss: {metrics.get('max_loss', 0)}% | Avg win: {metrics.get('avg_win', 0)}% | Avg loss: {metrics.get('avg_loss', 0)}%

CURRENT KEY PARAMS:
RSI_OVERSOLD={_extract(config,'RSI_OVERSOLD')} | RSI_OVERBOUGHT={_extract(config,'RSI_OVERBOUGHT')} | STOP_LOSS_PCT={_extract(config,'STOP_LOSS_PCT')} | TAKE_PROFIT_PCT={_extract(config,'TAKE_PROFIT_PCT')} | RSI_EXIT_OVERBOUGHT={_extract(config,'RSI_EXIT_OVERBOUGHT')}
USE_TREND_FOLLOW_STRATEGY={_extract(config,'USE_TREND_FOLLOW_STRATEGY')} | USE_FLUSH_SHORT={_extract(config,'USE_FLUSH_SHORT')}

KELLY SUMMARY (all-time per strategy):
{json.dumps(kelly, indent=2)}

Produce:
1. Weekly verdict (2–3 sentences)
2. Most important finding this week (cite Kelly numbers if relevant)
3. Action: [NO CHANGE] OR [CHANGE: PARAM_NAME from OLD_VALUE to NEW_VALUE — one-line reason]
   Numeric example: [CHANGE: RSI_EXIT_OVERBOUGHT from 72 to 78]
   Boolean example: [CHANGE: USE_FLUSH_SHORT from True to False]
   Boolean example: [CHANGE: USE_TREND_FOLLOW_STRATEGY from True to False]
   Rules:
   - Only change if ≥3 trades support it. Never change keys/addresses/DRY_RUN.
   - A strategy with negative Kelly and ≥10 trades warrants disabling it (boolean change).
   - One change per cycle — choose the highest-impact one.
4. Confidence: HIGH / MEDIUM / LOW""",
        max_tokens=700,
        model=MODEL_CIO,
    )

    # Reflection / self-critique pass — Opus (final gate before any config change)
    critique = call(
        system=(
            "You are a critical reviewer checking AI analysis for reasoning biases. "
            "Be rigorous and brief. 120 words max."
        ),
        user=f"""Review this CIO analysis before it gets committed to the trading bot:

{synthesis}

Check for:
- Hindsight bias: calling obvious what was unknowable at trade time
- Confirmation bias: cherry-picking evidence that supports a pre-existing view
- Attribution bias: blaming market conditions instead of strategy flaws
- Insufficient data: proposing config changes based on <3 trades

Respond with exactly one of:
APPROVED — no significant bias found. Analysis is sound.
REVISE: [specific concern] — then rewrite ONLY the action line with a corrected recommendation.""",
        max_tokens=300,
        model=MODEL_CRITIQUE,
    )

    return synthesis, critique


# ─── CONFIG CHANGE ────────────────────────────────────────────────────────────

# Params that must never be auto-changed
_BLOCKED_PARAMS = {"HYPERLIQUID_PRIVATE_KEY", "HYPERLIQUID_ADDRESS", "WALLET_PRIVATE_KEY",
                   "DRY_RUN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "LOG_FILE",
                   "LIQUIDATION_FILE", "NETWORK"}

def apply_change(synthesis: str, critique: str, config: str) -> tuple[bool, str]:
    """
    Parse [CHANGE: PARAM from X to Y] from CIO output.
    Supports numeric params (e.g. RSI_OVERSOLD from 40 to 38) and
    boolean strategy flags (e.g. USE_FLUSH_SHORT from True to False).
    Only applies if APPROVED by critique and param is safe.
    Returns (changed, description).
    """
    if "APPROVED" not in critique.upper():
        return False, "Critique requested revision — no change applied"

    combined = synthesis + "\n" + critique

    # Boolean param change: [CHANGE: USE_FLUSH_LONG from True to False]
    m_bool = re.search(
        r"\[CHANGE:\s*([A-Z_]+)\s+from\s+(True|False)\s+to\s+(True|False)\]",
        combined, re.I,
    )
    if m_bool:
        param, old_val, new_val = m_bool.group(1).upper(), m_bool.group(2), m_bool.group(3)
        if param in _BLOCKED_PARAMS:
            return False, f"Blocked: {param} is a protected parameter"
        pattern = rf"({re.escape(param)}\s*=\s*)(True|False)"
        new_config = re.sub(pattern, rf"\g<1>{new_val}", config)
        if new_config == config:
            return False, f"Boolean pattern not matched for {param}"
        CONFIG_FILE.write_text(new_config)
        result = subprocess.run(
            ["python3", "-c", f"import ast; ast.parse(open('{CONFIG_FILE}').read())"],
            capture_output=True,
        )
        if result.returncode != 0:
            CONFIG_FILE.write_text(config)
            return False, f"Reverted: syntax error after changing {param}"
        return True, f"{param}: {old_val} → {new_val}"

    # Numeric param change: [CHANGE: RSI_OVERSOLD from 40 to 38]
    m = re.search(
        r"\[CHANGE:\s*([A-Z_]+)\s+from\s+([\d.]+)\s+to\s+([\d.]+)",
        combined, re.I,
    )
    if not m:
        return False, "No change recommended"

    param, old_val, new_val = m.group(1).upper(), m.group(2), m.group(3)

    if param in _BLOCKED_PARAMS:
        return False, f"Blocked: {param} is a protected parameter"

    pattern = rf"({re.escape(param)}\s*=\s*)[\d.]+"
    new_config = re.sub(pattern, rf"\g<1>{new_val}", config)

    if new_config == config:
        return False, f"Pattern not matched for {param} — no change applied"

    CONFIG_FILE.write_text(new_config)
    result = subprocess.run(
        ["python3", "-c", f"import ast; ast.parse(open('{CONFIG_FILE}').read())"],
        capture_output=True,
    )
    if result.returncode != 0:
        CONFIG_FILE.write_text(config)
        return False, f"Reverted: syntax error after changing {param}"

    return True, f"{param}: {old_val} → {new_val}"


# ─── LESSONS.MD ENTRY ─────────────────────────────────────────────────────────

def build_entry(trades, metrics, kelly, risk, strategy, market, synthesis, critique, change_desc) -> str:
    if trades:
        rows = "\n".join(
            f"| {t.get('direction','-')} | {t.get('strategy','-')} | "
            f"${t.get('entry_price','-')} | ${t.get('close_price','-')} | "
            f"{t.get('exit_reason','-')} | {t.get('pnl_pct','-')}% |"
            for t in trades
        )
        trade_table = (
            "| Dir | Strategy | Entry | Close | Exit | PnL% |\n"
            "|---|---|---|---|---|---|\n" + rows
        )
    else:
        trade_table = "_No closed trades this week._"

    kelly_rows = "\n".join(
        f"| {s} | {k['trades']} | {k['win_rate_pct']}% | {k['rr_ratio']}x | {k['profit_factor']} | {k['kelly_pct']:+.1f}% | {'✅' if k['has_edge'] else '❌'} |"
        for s, k in (kelly or {}).items()
    )
    kelly_table = (
        "| Strategy | Trades | Win Rate | R:R | PF | Kelly | Edge |\n"
        "|---|---|---|---|---|---|---|\n" + kelly_rows
    ) if kelly_rows else "_No Kelly data available._"

    return f"""
---

## 🤖 Autonomous Multi-Agent Review — {TODAY}

> Generated by `weekly_review_agent.py` (FinRobot-inspired 4-agent pipeline)
> Models: analysts={MODEL_ANALYST} · CIO={MODEL_CIO} · critique={MODEL_CRITIQUE}

### Trade Summary
{trade_table}

### Performance Metrics
| Metric | Value |
|---|---|
| Trades | {metrics.get('trade_count', 0)} |
| Win Rate | {metrics.get('win_rate', 0)}% |
| Avg Win | {metrics.get('avg_win', 0)}% |
| Avg Loss | {metrics.get('avg_loss', 0)}% |
| Total PnL | {metrics.get('total_pnl_pct', 0)}% |
| Max Single Loss | {metrics.get('max_loss', 0)}% |

### Kelly Criterion (All-Time)
{kelly_table}

### Risk Analyst
{risk}

### Strategy Analyst
{strategy}

### Market Analyst
{market}

### CIO Decision
{synthesis}

### Self-Critique (Reflection Pass)
{critique}

### Config Change Applied
{change_desc}

---
"""


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _extract(config: str, param: str) -> str:
    m = re.search(rf"^{re.escape(param)}\s*=\s*(.+)", config, re.M)
    return m.group(1).strip() if m else "not found"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if os.getenv("LOCAL_PRIVATE_REVIEW", "false").strip().lower() != "true":
        raise SystemExit(
            "Weekly review is local/private only; use weekly_review.sh with explicit opt-in."
        )
    print(f"=== Autonomous Weekly Review — {TODAY} ===\n")

    log      = load_log(tail=800)
    config   = CONFIG_FILE.read_text()
    lessons  = LESSONS_FILE.read_text() if LESSONS_FILE.exists() else ""
    clusters = load_liquidation_clusters()

    trades  = parse_trades(log)
    metrics = compute_metrics(trades)
    kelly   = compute_kelly()
    print(f"Trades found: {metrics['trade_count']} | Win rate: {metrics['win_rate']}%")
    for strat_name, k in kelly.items():
        edge_flag = "✅" if k["has_edge"] else "❌"
        print(f"  Kelly [{strat_name}]: {k['kelly_pct']:+.1f}% {edge_flag}  (PF {k['profit_factor']} | {k['trades']} trades)")
    print()

    print("→ Risk Analyst (with Kelly)...")
    risk = risk_analyst(trades, metrics, config, log, kelly)

    print("→ Strategy Analyst...")
    strat = strategy_analyst(trades, metrics, config, log)

    print("→ Market Analyst...")
    mkt = market_analyst(log, clusters)

    print("→ CIO (synthesis + self-critique)...")
    synthesis, critique = cio(risk, strat, mkt, lessons, config, metrics, kelly)

    print(f"\nSynthesis preview: {synthesis[:200]}...")
    print(f"Critique: {critique[:150]}\n")

    # M0 governance: proposal-only mode. LLM output must not mutate config,
    # write LESSONS.md, commit, or push. Human review applies accepted changes.
    change_desc = "Proposal-only mode — no config change applied"
    entry = build_entry(trades, metrics, kelly, risk, strat, mkt, synthesis, critique, change_desc)
    proposal_file = REPO / f"weekly_review_proposal_{TODAY}.md"
    proposal_file.write_text(entry)
    print(f"✅ Proposal written: {proposal_file}")
    print("ℹ️  No config, LESSONS.md, git commit, or push performed.\n=== Done ===")


if __name__ == "__main__":
    main()
