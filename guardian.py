"""
Guardian Process — Independent Position Safety Monitor

Runs as a separate process from the main bot. Polls HyperLiquid every 30s and
force-closes positions that violate hard safety limits. Survives bot crashes.

Safety checks:
  1. Equity below floor → force close
  2. Position open longer than MAX_POSITION_HOURS → force close
  3. Cumulative funding cost exceeds threshold → force close

Inspired by Coinbase Agentic Wallet pattern: spending policies enforced at
infrastructure layer, independent of application logic.

Usage:
  python guardian.py                  # runs standalone
  Launched as a second process in Dockerfile or Fly.io Procfile
"""

import os
import time
import argparse
import json
import logging
import urllib.request
from datetime import datetime, timezone

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from security_manager import SecurityManager
import config_dex as config

# ── Config ──────────────────────────────────────────────────────────────────
SYMBOL = config.SYMBOL
CHECK_INTERVAL = 30  # seconds

# Safety thresholds — these are HARD limits, not suggestions
MIN_EQUITY_FLOOR = float(os.getenv("GUARDIAN_EQUITY_FLOOR", "15.0"))
MAX_POSITION_HOURS = float(os.getenv("GUARDIAN_MAX_POSITION_HOURS", "168"))  # 7 days
MAX_FUNDING_COST_PCT = float(os.getenv("GUARDIAN_MAX_FUNDING_PCT", "2.0"))

# Telegram — same source as main bot
TELEGRAM_BOT_TOKEN = getattr(config, 'TELEGRAM_BOT_TOKEN', '') or os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = getattr(config, 'TELEGRAM_CHAT_ID', '') or os.getenv("TELEGRAM_CHAT_ID", "")

# ── Logging ─────────────────────────────────────────────────────────────────
_DATA_DIR = config.DATA_DIR
os.makedirs(_DATA_DIR, exist_ok=True)
LOG_PATH = os.path.join(_DATA_DIR, "guardian.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GUARDIAN] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, mode='a'),
    ],
)
log = logging.getLogger("guardian")
UNKNOWN_POSITION = object()


def live_actions_allowed() -> bool:
    """Require independent guardian and trading opt-ins on a valid network."""
    return bool(
        getattr(config, "GUARDIAN_ENABLE_LIVE_ACTIONS", False)
        and getattr(config, "GUARDIAN_LIVE_RUNTIME_OPT_IN", False)
        and getattr(config, "LIVE_TRADING_OPT_IN", False)
        and not getattr(config, "DRY_RUN", True)
        and not getattr(config, "DEMO_MODE", True)
        and getattr(config, "HL_NETWORK_VALID", False)
    )


def notify(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")


def get_exchange_and_info():
    if not live_actions_allowed():
        log.info("Guardian live actions disabled; no exchange client created")
        return None

    private_key = SecurityManager.get_key("HYPERLIQUID_PRIVATE_KEY")
    if not private_key:
        log.error("Guardian enabled but signing credential is missing; exiting without action")
        return None
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    base_url = constants.TESTNET_API_URL if config.IS_TESTNET else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    from eth_account import Account
    wallet = Account.from_key(private_key)
    configured_address = (config.HYPERLIQUID_ADDRESS or "").lower()
    if configured_address and configured_address != wallet.address.lower():
        log.error("Guardian signer/account mismatch; exiting without action")
        return None
    exchange = Exchange(wallet, base_url)
    log.info("Guardian initialized for configured account")
    return exchange, info, wallet.address


def get_position(info, address):
    try:
        state = info.user_state(address)
        for pos in state.get("assetPositions", []):
            p = pos["position"]
            szi = float(p["szi"])
            if szi != 0 and p["coin"] == SYMBOL:
                return {
                    "side": "long" if szi > 0 else "short",
                    "size": abs(szi),
                    "entry_price": float(p["entryPx"]),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                    "cum_funding": float(p.get("cumFunding", {}).get("sinceOpen", 0))
                        if isinstance(p.get("cumFunding"), dict) else 0.0,
                }
        return None
    except Exception as e:
        log.error(f"Position fetch failed: {e}")
        return UNKNOWN_POSITION


def get_equity(info, address):
    try:
        state = info.user_state(address)
        margin = state.get("marginSummary", {})
        return float(margin.get("accountValue", 0))
    except Exception as e:
        log.error(f"Equity fetch failed: {e}")
        return None


def force_close(exchange, info, address, reason: str):
    if not live_actions_allowed():
        log.error("Guardian close blocked: independent live-action opt-in is not satisfied")
        return False
    pos = get_position(info, address)
    if pos is UNKNOWN_POSITION:
        log.error("Force close requested but position state is UNKNOWN")
        notify(f"🚨 *GUARDIAN FORCE CLOSE BLOCKED*\nReason: {reason}\nPosition state unknown")
        return False
    if not pos:
        log.info("Force close requested but no position found")
        return False

    try:
        result = exchange.market_close(SYMBOL)
        if isinstance(result, dict) and result.get("status") not in (None, "ok"):
            log.error(f"Force close rejected — reason: {reason} | result: {result}")
            notify(f"🚨 *GUARDIAN FORCE CLOSE REJECTED*\nReason: {reason}\nResult: {result}")
            return False

        # Verify the position is actually gone before reporting success.
        for _ in range(3):
            post_close = get_position(info, address)
            if post_close is UNKNOWN_POSITION:
                log.error("Force close verification failed: position state UNKNOWN")
                return False
            if not post_close:
                log.warning(f"FORCE CLOSE verified flat — reason: {reason} | result: {result}")
                notify(
                    f"🛡️ *GUARDIAN FORCE CLOSE*\n"
                    f"Reason: {reason}\n"
                    f"Side: {pos['side'].upper()} | Size: {pos['size']}\n"
                    f"Entry: ${pos['entry_price']:.2f} | uPnL: ${pos['unrealized_pnl']:.2f}"
                )
                return True
            time.sleep(2)

        log.error(f"Force close not verified flat — reason: {reason}")
        notify(f"🚨 *GUARDIAN FORCE CLOSE NOT VERIFIED*\nReason: {reason}\nPosition still present")
        return False
    except Exception as e:
        log.error(f"Force close FAILED: {e}")
        notify(f"🚨 *GUARDIAN FORCE CLOSE FAILED*\nReason: {reason}\nError: {e}")
        return False


def recover_position_age():
    """Try to recover position open time from the bot's trade log."""
    trade_log = f"{_DATA_DIR}/trades.log"
    try:
        with open(trade_log, 'r') as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if not lines:
            return None
        last_line = lines[-1]
        parts = last_line.split(",")
        if len(parts) > 1 and parts[1].startswith("OPEN_"):
            open_ts = parts[0]
            open_dt = datetime.strptime(open_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_h = (time.time() - open_dt.timestamp()) / 3600
            log.info(f"Recovered position age from trade log: {age_h:.1f}h ago")
            return open_dt.timestamp()
    except Exception as e:
        log.warning(f"Could not recover position age: {e}")
    return None


def run():
    clients = get_exchange_and_info()
    if clients is None:
        return
    exchange, info, address = clients

    position_open_since = recover_position_age()
    last_heartbeat = 0

    log.info("=" * 50)
    log.info("GUARDIAN PROCESS — INDEPENDENT SAFETY MONITOR")
    log.info(f"  Equity floor:       ${MIN_EQUITY_FLOOR}")
    log.info(f"  Max position hours: {MAX_POSITION_HOURS}h")
    log.info(f"  Max funding cost:   {MAX_FUNDING_COST_PCT}%")
    log.info(f"  Check interval:     {CHECK_INTERVAL}s")
    log.info("=" * 50)

    while True:
        try:
            pos = get_position(info, address)
            equity = get_equity(info, address)

            if pos is UNKNOWN_POSITION:
                log.error("Position state UNKNOWN — preserving age tracker and skipping safety actions this cycle")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Track position age ──────────────────────────────────────
            if pos:
                if position_open_since is None:
                    position_open_since = time.time()
                    log.info(f"Position detected: {pos['side'].upper()} {pos['size']} ETH @ ${pos['entry_price']:.2f}")
                hours_open = (time.time() - position_open_since) / 3600
            else:
                if position_open_since is not None:
                    log.info("Position closed — resetting age tracker")
                position_open_since = None
                hours_open = 0

            # ── Check 1: Equity floor ───────────────────────────────────
            if equity is not None and equity < MIN_EQUITY_FLOOR and pos:
                force_close(exchange, info, address,
                            f"Equity ${equity:.2f} < floor ${MIN_EQUITY_FLOOR}")

            # ── Check 2: Max position duration ──────────────────────────
            elif pos and hours_open > MAX_POSITION_HOURS:
                force_close(exchange, info, address,
                            f"Position open {hours_open:.1f}h > max {MAX_POSITION_HOURS}h")

            # ── Check 3: Funding cost bleed ─────────────────────────────
            elif pos and pos.get("cum_funding", 0) != 0:
                entry_value = pos["size"] * pos["entry_price"]
                if entry_value > 0:
                    funding_pct = abs(pos["cum_funding"]) / entry_value * 100
                    if funding_pct > MAX_FUNDING_COST_PCT:
                        force_close(exchange, info, address,
                                    f"Cumulative funding {funding_pct:.2f}% > max {MAX_FUNDING_COST_PCT}%")

            # ── Heartbeat log (every 30 min) ────────────────────────────
            now = time.time()
            if now - last_heartbeat > 1800:
                status = "no position"
                if pos:
                    status = f"{pos['side'].upper()} {pos['size']} ETH, {hours_open:.1f}h open, uPnL ${pos['unrealized_pnl']:.2f}"
                eq_str = f"${equity:.2f}" if equity else "unknown"
                log.info(f"Heartbeat — equity: {eq_str} | {status}")
                last_heartbeat = now

        except Exception as e:
            log.error(f"Guardian loop error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Independent Hyperliquid risk guardian")
    parser.add_argument("--live-actions", action="store_true", help="allow closes only with both guardian and trading environment opt-ins")
    args = parser.parse_args()
    config.GUARDIAN_LIVE_RUNTIME_OPT_IN = bool(args.live_actions)
    run()
