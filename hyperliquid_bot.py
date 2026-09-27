"""
ETH Leverage Flush Trading Bot - Hyperliquid Version

Trades on Hyperliquid perpetuals based on leverage flush strategy using OKX liquidation data.
"""

import argparse
import os
import time
import logging
import math
import secrets
import threading
from datetime import datetime
from typing import Dict, Any, Optional, List
from collections import defaultdict, deque

import requests
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config_dex as config
from indicators import calculate_rsi, calculate_ema, calculate_atr
from cascade_detector import CascadeDetector, RiskLevel
from liquidation_recorder import LiquidationRecorder
from macro_regime import get_macro_regime, format_regime_summary
from markov_regime import get_regime_signal as get_markov_signal
from long_only_policy import (
    CostModel, EntryContext, LongOnlyPolicy, PolicyConfig, compute_position_size,
    estimate_cumulative_funding_cost_pct,
)

# =============================================================================
# FLUSH BOT STATE & API
# =============================================================================

class BotState:
    """Thread-safe state store for the UI."""
    def __init__(self):
        self.price = 0.0
        self.balance = 0.0
        self.position = 'none'
        self.signal = 'INIT'
        self.pnl_pct = 0.0
        self.address = ""
        self.rsi = 50.0
        self.ema = 0.0
        self.funding_rate = 0.0
        self.cascade_risk = "NORMAL"
        self.cascade_score = 0.0
        self.cascade_reasons = []
        self.regime = "range"
        self.perps_positions = []
        self.spot_positions = []
        self.pnl_history = deque(maxlen=100)
        self.ws_liquidation_count = 0
        self.ws_liquidation_by_exchange = defaultdict(int)
        self.ws_last_liquidation = None
        self.ws_streams = {}
        self.liquidation_buffer_count = 0
        self.last_update = datetime.now().isoformat()
        self.logs = deque(maxlen=50)
        self._lock = threading.Lock()

    def update(self, price, balance, position, signal, pnl=0.0, address=None, rsi=50.0, ema=0.0, funding_rate=0.0, cascade_risk="NORMAL", cascade_score=0.0, cascade_reasons=None, perps_positions=None, spot_positions=None, regime="range"):
        with self._lock:
            self.price = price
            self.balance = balance
            self.position = position
            self.signal = signal
            self.pnl_pct = pnl
            self.rsi = rsi
            self.ema = ema
            self.funding_rate = funding_rate
            self.cascade_risk = cascade_risk
            self.cascade_score = cascade_score
            if perps_positions is not None:
                self.perps_positions = perps_positions
            if spot_positions is not None:
                self.spot_positions = spot_positions
            if cascade_reasons is not None:
                self.cascade_reasons = cascade_reasons
            self.regime = regime
            if address:
                self.address = address
            
            # Maintain PnL history
            self.pnl_history.append(pnl)
            
            self.last_update = datetime.now().isoformat()

    def add_log(self, msg: str):
        with self._lock:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.logs.appendleft({"time": timestamp, "msg": msg})  # Newest first

    def record_ws_liquidation(self, event, buffer_count: int):
        """Track non-sensitive WebSocket liquidation telemetry for ops validation."""
        with self._lock:
            self.ws_liquidation_count += 1
            self.ws_liquidation_by_exchange[event.exchange] += 1
            self.liquidation_buffer_count = buffer_count
            self.ws_last_liquidation = {
                "exchange": event.exchange,
                "symbol": event.symbol,
                "price": event.price,
                "size_usd": round(event.size_usd, 2),
                "side": event.side,
                "timestamp": event.timestamp,
                "received_at": datetime.now().isoformat(),
            }

    def record_ws_stream_status(self, stream: str, status: dict):
        """Track WebSocket connection status without exposing credentials/account data."""
        with self._lock:
            previous = self.ws_streams.get(stream, {})
            self.ws_streams[stream] = {**previous, **status}

    def get_snapshot(self):
        with self._lock:
            return {
                "price": self.price,
                "balance": self.balance,
                "position": self.position,
                "signal": self.signal,
                "pnl": self.pnl_pct,
                "address": self.address,
                "rsi": self.rsi,
                "ema": self.ema,
                "funding_rate": self.funding_rate,
                "cascade_risk": self.cascade_risk,
                "cascade_score": self.cascade_score,
                "cascade_reasons": self.cascade_reasons,
                "regime": self.regime,
                "perps_positions": self.perps_positions,
                "spot_positions": self.spot_positions,
                "pnl_history": list(self.pnl_history),
                "ws_liquidation_count": self.ws_liquidation_count,
                "ws_liquidation_by_exchange": dict(self.ws_liquidation_by_exchange),
                "ws_last_liquidation": self.ws_last_liquidation,
                "ws_streams": dict(self.ws_streams),
                "liquidation_buffer_count": self.liquidation_buffer_count,
                "logs": list(self.logs)
            }

    def get_public_snapshot(self):
        """Return deterministic synthetic state; never expose account/ops data."""
        return {
            "demo_data": True,
            "price": 2500.0,
            "balance": 10000.0,
            "position": "none",
            "signal": "WAIT",
            "pnl": 0.0,
            "rsi": 50.0,
            "ema": 2480.0,
            "funding_rate": 0.00001,
            "cascade_risk": "NORMAL",
            "cascade_score": 12.0,
            "cascade_reasons": ["Synthetic demonstration data"],
            "regime": "demo",
            "perps_positions": [],
            "spot_positions": [],
            "logs": [
                {"time": "--:--:--", "msg": "Synthetic demo dashboard — no account data"}
            ],
            "execution_mode": "DEMO",
            "dry_run": True,
            "new_entries_paused": True,
            "live_exits_enabled": False,
        }

bot_state = BotState()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def require_operational_api_token(
    authorization: Optional[str] = Header(default=None),
    x_api_token: Optional[str] = Header(default=None),
) -> None:
    """Protect account/log endpoints; /health remains public."""
    expected = os.getenv("OPERATIONAL_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Operational API token not configured")
    supplied = x_api_token or ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _tail_file(path, lines: int) -> tuple[list[str], int]:
    """Bounded tail read; avoids loading whole persistent logs into memory."""
    from collections import deque as _deque
    total = 0
    tail = _deque(maxlen=lines)
    with open(path, "r", errors="replace") as f:
        for line in f:
            total += 1
            tail.append(line.rstrip("\n"))
    return list(tail), total


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/api/status", dependencies=[Depends(require_operational_api_token)])
async def get_status():
    return bot_state.get_snapshot()

@app.get("/api/public-status")
async def get_public_status():
    return bot_state.get_public_snapshot()


def _build_pnl_history():
    """Fetch all-time PnL from Hyperliquid fills without returning account identifiers."""
    address = bot_state.address
    if not address or address == "Not Configured":
        return {"data": [], "total_pnl": 0}
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "userFills", "user": address, "aggregateByTime": False},
            timeout=10
        )
        fills = resp.json()
        if not isinstance(fills, list):
            return {"data": [], "total_pnl": 0}

        fills.sort(key=lambda f: f.get("time", 0))
        cumulative = 0.0
        data = []
        for f in fills:
            pnl = float(f.get("closedPnl", 0))
            fee = float(f.get("fee", 0))
            cumulative += pnl - fee
            data.append({
                "t": f.get("time"),
                "pnl": round(cumulative, 4),
                "coin": f.get("coin"),
                "dir": f.get("dir"),
                "raw_pnl": round(pnl, 4),
                "fee": round(fee, 4),
            })
        return {"data": data, "total_pnl": round(cumulative, 4)}
    except Exception as e:
        return {"data": [], "total_pnl": 0, "error": str(e)}


@app.get("/api/public-pnl-history")
async def get_public_pnl_history():
    """Synthetic public response; real fill history requires authentication."""
    return {"demo_data": True, "data": [], "total_pnl": 0.0}

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/api/log", dependencies=[Depends(require_operational_api_token)])
async def get_log(lines: int = 200):
    """
    Expose persistent trade logs from the configured private data directory.
    The trade log uses one line per event in CSV format:
      timestamp, event, side, price, pnl_pct, amount_usd, balance[, strategy]
    Used by weekly_review_agent.py for automated weekly reviews.
    """
    import pathlib
    trades_path = pathlib.Path(config.DATA_DIR) / "trades.log"
    raw_path = pathlib.Path(config.DATA_DIR) / "hyperliquid_trading.log"
    result = {}
    lines = max(1, min(int(lines), 500))
    try:
        if trades_path.exists():
            result["trades"], result["trades_total"] = _tail_file(trades_path, lines)
        else:
            result["trades"] = []
            result["trades_total"] = 0
        if raw_path.exists():
            result["lines"], result["total"] = _tail_file(raw_path, lines)
        else:
            result["lines"] = []
            result["total"] = 0
        result["lines_requested"] = lines
        return result
    except Exception as e:
        return {"trades": [], "lines": [], "error": str(e)}

@app.get("/api/denials", dependencies=[Depends(require_operational_api_token)])
async def get_denials():
    """Recent entry denials with structured reasons. Used by weekly review."""
    return {"denials": list(getattr(bot_state, '_denial_ref', []))}

@app.get("/api/pnl-history", dependencies=[Depends(require_operational_api_token)])
async def get_pnl_history():
    """Fetch all-time PnL from Hyperliquid trade fills."""
    return _build_pnl_history()

def start_api_server():
    """Run the API using localhost-safe environment defaults."""
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="error")

# =============================================================================
import asyncio
from websocket_manager import WebSocketManager, LiquidationEvent

# TRADING BOT
# =============================================================================

def hyperliquid_live_actions_allowed() -> bool:
    """Require independent environment and CLI/runtime live opt-ins."""
    return bool(
        not getattr(config, "DRY_RUN", True)
        and getattr(config, "LIVE_TRADING_OPT_IN", False)
        and getattr(config, "HL_LIVE_RUNTIME_OPT_IN", False)
    )


class HyperliquidFlushBot:
    """Leverage flush-based trading bot for Hyperliquid."""
    
    def __init__(self):
        self.setup_logging()
        self.logger = logging.getLogger(__name__)
        self.base_url = constants.TESTNET_API_URL if config.IS_TESTNET else constants.MAINNET_API_URL
        # Live mode is explicitly opt-in and must have a usable signer. Never
        # initialize exchange clients for a nominal live bot with no credentials.
        if not config.DRY_RUN and not getattr(config, "LIVE_TRADING_OPT_IN", False):
             raise RuntimeError("Live execution requires explicit HL_ENABLE_LIVE_TRADING=true opt-in")
        if not config.DRY_RUN and not getattr(config, "HL_LIVE_RUNTIME_OPT_IN", False):
             raise RuntimeError("Live execution requires the explicit --live runtime opt-in")
        if not config.DRY_RUN and getattr(config, "DEMO_MODE", True):
             raise RuntimeError("Live execution requires DEMO_MODE=false")
        if not config.DRY_RUN and not getattr(config, "HL_NETWORK_VALID", False):
             raise RuntimeError("Live execution requires HL_NETWORK=testnet or HL_NETWORK=mainnet")
        if not config.DRY_RUN and not (config.HYPERLIQUID_PRIVATE_KEY or "").strip():
             raise RuntimeError("Live execution requires HYPERLIQUID_PRIVATE_KEY")
        self.info = Info(self.base_url, skip_ws=True)
        if not config.DRY_RUN:
             # Create wallet from private key (add 0x prefix if not present)
             private_key = config.HYPERLIQUID_PRIVATE_KEY.strip()
             if not private_key.startswith('0x'):
                 private_key = '0x' + private_key
             from eth_account import Account
             wallet = Account.from_key(private_key)
             self.exchange = Exchange(wallet, self.base_url)
             self.address = wallet.address
             configured_address = (config.HYPERLIQUID_ADDRESS or "").lower()
             if configured_address and configured_address != self.address.lower():
                 raise RuntimeError(
                     f"Signer/config account mismatch: signer {self.address} != configured {config.HYPERLIQUID_ADDRESS}"
                 )
             self.log(f"Bot initialized with address: {self.address}")
        else:
             self.exchange = None
             self.address = config.HYPERLIQUID_ADDRESS or "Not Configured"
             
        self.last_trade_price = None
        self.last_action = None
        # Last successfully fetched balances prevent transient read failures from
        # being interpreted as zero account equity.
        self.last_known_balances = {'USDC': 0, 'available': 0, 'spot_balances': []}
        self.last_liquidations = []
        self.liquidation_buffer = deque(maxlen=2000) # Buffer for WS liquidations
        self.last_liquidations_time = 0
        self.threshold_amount = 1000000
        self.entry_buffer_pct = 0.5
        self.take_profit_pct = config.TAKE_PROFIT_PCT
        self.stop_loss_pct = config.STOP_LOSS_PCT
        self.leverage = config.LEVERAGE
        self.symbol = config.SYMBOL  # "ETH"
        
        # Re-entry cooldown — track last TP/SL close to prevent immediate same-direction re-entry
        self.last_close_time = 0.0        # epoch seconds of last TP/SL close
        self.last_close_side = None       # 'long' or 'short' — direction that was just closed

        # Trailing stop state
        self.trailing_activated = False   # True once position hits TRAILING_TRIGGER_PCT
        self.trailing_floor = None        # Current floor price (None = not yet active)
        self.ladder_1_executed = False    # Ladder buy 1 has fired this trade
        self.ladder_2_executed = False    # Ladder buy 2 has fired this trade

        # Staircase-down circuit breaker (Fix C — Mar 20, 2026)
        # Tracks last N long closes — if all descending entry prices + all losses, pause longs
        self.long_close_history = deque(maxlen=getattr(config, 'STAIRCASE_LOOKBACK', 3))
        self.long_entry_price_tracked = None  # Entry price recorded at LONG open
        self.staircase_pause_until = 0.0      # epoch; longs blocked until this time

        # Position age tracking
        self.position_open_time = None  # epoch when current position was opened

        # Entry denial log — structured reasons for blocked entries (Phase 19)
        self.denial_log = deque(maxlen=50)

        # VPIN state (Mar 20, 2026) — rolling trade buffer for order flow toxicity
        self.trade_buffer = deque(maxlen=getattr(config, 'VPIN_WINDOW', 500))
        self.current_vpin = None

        # Macro regime (FRED-based, Mar 20, 2026) — cached every 6h
        self.macro_snapshot = None
        self.last_macro_check = 0.0

        # Regime tracking — determines which strategy is active
        self.active_regime = 'range'      # current market/signal regime
        # Immutable per-position risk metadata — set at entry and held until close.
        self.position_strategy = None
        self.position_stop_loss_pct = None
        self.position_take_profit_pct = None

        # Initialize WebSocket Manager
        self.ws_manager = WebSocketManager(self.logger, status_callback=bot_state.record_ws_stream_status)
        self.ws_manager.register_callback(self.on_liquidation_event)

        # Funding Rate Cache
        self.last_funding_time = 0
        self.cached_funding_rate = 0.0

        # Action Items State (OI, RSI tracking)
        self.cached_oi = 0.0
        self.prev_oi = None
        self.prev_rsi = None
        self.prev_price = None

        # Cascade Detector — early warning system for leverage cascades
        self.cascade_detector = CascadeDetector(self.info, self.logger)
        
        # Liquidation Recorder — local persistence for long-term historical data
        self.recorder = LiquidationRecorder(storage_file=config.LIQUIDATION_FILE, logger=self.logger)

    def on_liquidation_event(self, event: LiquidationEvent):
        """Handle incoming liquidation event from WebSocket."""
        # Clean up old liquidations first (older than 24h)
        current_time = time.time()
        while self.liquidation_buffer and (current_time - self.liquidation_buffer[0]['ts'] > 86400):
            self.liquidation_buffer.popleft()
            
        self.liquidation_buffer.append({
            "bkPx": event.price,
            "sz": event.size_usd / event.price / 0.1, # Approx size in contracts for compatibility
            "side": event.side, # 'long' or 'short' (liquidated side)
            "ts": event.timestamp,
            "source": event.exchange
        })
        bot_state.record_ws_liquidation(event, len(self.liquidation_buffer))
        self.logger.debug(f"⚡ WS Liquidation: {event.side.upper()} ${event.size_usd:.0f} @ ${event.price}")

    def start_websocket_loop(self):
        """Run WebSocket manager in a separate asyncio loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.ws_manager.start())
        
    def setup_logging(self):
        # Configure logging to file and also to bot_state for UI
        logging.basicConfig(
            level=getattr(logging, config.LOG_LEVEL),
            format='%(asctime)s | %(levelname)s | %(message)s',
            handlers=[
                logging.FileHandler(config.LOG_FILE),
                logging.StreamHandler()
            ]
        )
    
    TRADE_LOG_PATH = os.path.join(config.DATA_DIR, "trades.log")

    def _log_trade(self, event: str, side: str, price: float, pnl_pct: float = 0.0, amount_usd: float = 0.0, strategy: str = ""):
        """Append a trade event to the persistent trade log on the Fly volume.
        Format: ISO timestamp, event, side, price, pnl_pct, amount_usd, balance, strategy
        strategy tag: flush | trend-follow | rsi | regime-exit | cascade-exit | unknown
        The location is controlled by BOT_DATA_DIR.
        """
        try:
            os.makedirs(os.path.dirname(self.TRADE_LOG_PATH), exist_ok=True)
            balance = bot_state.balance
            strategy_field = strategy or "unknown"
            line = (
                f"{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')},"
                f"{event},{side.upper()},{price:.2f},{pnl_pct:+.4f},"
                f"{amount_usd:.2f},{balance:.2f},{strategy_field}\n"
            )
            with open(self.TRADE_LOG_PATH, 'a') as f:
                f.write(line)
        except Exception as e:
            self.log(f"⚠️ Trade log write failed: {e}", 'error')

    def log(self, msg: str, level: str = 'info'):
        """Helper to log to both file/console and UI."""
        if level == 'error':
            self.logger.error(msg)
        else:
            self.logger.info(msg)
        bot_state.add_log(msg)

    def _log_denial(self, strategy: str, gate: str, reason: str, context: dict = None):
        """Log a structured entry denial for weekly review analysis."""
        entry = {
            "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": strategy,
            "gate": gate,
            "reason": reason,
        }
        if context:
            entry["context"] = context
        self.denial_log.append(entry)
        self.log(f"   🚫 DENIED [{gate}] {strategy}: {reason}")

    def notify(self, msg: str):
        """Send an optional operator notification without blocking trading."""
        if not getattr(config, 'TELEGRAM_NOTIFY', False):
            return
        token = getattr(config, 'TELEGRAM_BOT_TOKEN', '')
        chat_id = getattr(config, 'TELEGRAM_CHAT_ID', '')
        if not token or not chat_id:
            return
        import threading, urllib.request, json as _json
        def _send():
            try:
                payload = _json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                self.logger.warning(f"Telegram notify failed: {e}")
        threading.Thread(target=_send, daemon=True).start()

    def get_current_price(self) -> Optional[float]:
        """Get current mid price from Hyperliquid (WS preferred)."""
        # Try WebSocket price first
        ws_price = self.ws_manager.get_price(self.symbol)
        if ws_price:
            return ws_price
            
        try:
            mids = self.info.all_mids()
            return float(mids.get(self.symbol))
        except Exception as e:
            self.log(f"Error fetching price: {e}", 'error')
            return None
    
    def get_funding_rate(self) -> float:
        """Get current hourly funding rate for symbol (cached 60s)."""
        if time.time() - self.last_funding_time < 60 and self.cached_funding_rate is not None:
             return self.cached_funding_rate
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                 # Fetch meta and asset contexts
                 meta, ctxs = self.info.meta_and_asset_ctxs()
                 # Find our symbol (coin name matches universe name)
                 # Index in ctxs corresponds to index in universe?
                 # Docs say: "The index of the asset in the universe is the index in the ctxs list"
                 universe = meta['universe']
                 found_idx = -1
                 for i, coin in enumerate(universe):
                     if coin['name'] == self.symbol:
                         found_idx = i
                         break
                 
                 if found_idx != -1:
                     funding = float(ctxs[found_idx]['funding'])
                     self.cached_funding_rate = funding
                     
                     if 'openInterest' in ctxs[found_idx]:
                         self.cached_oi = float(ctxs[found_idx]['openInterest'])

                     self.last_funding_time = time.time()
                     return funding
                 else:
                     self.log(f"Symbol {self.symbol} not found in universe", 'error')
                     return 0.0
            except Exception as e:
                 if attempt < max_retries - 1:
                     time.sleep(1)  # small delay before retry
                     continue
                 self.log(f"Error fetching funding rate after {max_retries} attempts: {e}", 'error')
                 return self.cached_funding_rate if self.cached_funding_rate is not None else 0.0
        return self.cached_funding_rate if self.cached_funding_rate is not None else 0.0

    def _get_session_vwap(self) -> Optional[float]:
        """Calculate Session VWAP (since 00:00 UTC) using 1h candles."""
        try:
            now_dt = datetime.utcnow()
            # Start of current UTC day
            start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(now_dt.timestamp() * 1000)
            
            # Fetch 1h candles since start of day
            candles = self.info.candles_snapshot(self.symbol, "1h", start_ms, end_ms)
            if not candles:
                return None
                
            total_vol = 0.0
            total_vol_price = 0.0
            for c in candles:
                # v = base volume, c = close, h = high, l = low
                typ_price = (float(c['h']) + float(c['l']) + float(c['c'])) / 3.0
                vol = float(c['v'])
                total_vol += vol
                total_vol_price += typ_price * vol
                
            if total_vol > 0:
                return total_vol_price / total_vol
            return None
        except Exception as e:
            self.logger.error(f"Error calculating VWAP: {e}")
            return None

    def get_balances(self) -> Dict[str, Any]:
        """Get account balances and available margin from Hyperliquid."""
        if not config.HYPERLIQUID_ADDRESS:
            return {'USDC': 0, 'available': 0, 'spot_balances': []}
            
        try:
            # 1. Get Perps state for equity and withdrawable margin
            state = self.info.user_state(config.HYPERLIQUID_ADDRESS)
            perp_equity = float(state.get('marginSummary', {}).get('accountValue', 0))
            available_margin = float(state.get('withdrawable', 0))
            
            # 2. Get Spot balance
            spot_state = self.info.spot_user_state(config.HYPERLIQUID_ADDRESS)
            spot_usdc = 0
            spot_usdc_hold = 0
            spot_balances = []

            for balance in spot_state.get('balances', []):
                coin = balance.get('coin')
                total = float(balance.get('total', 0))
                if total > 0:
                    spot_balances.append({
                        "coin": coin,
                        "total": total,
                        "hold": float(balance.get('hold', 0))
                    })
                if coin == 'USDC':
                    spot_usdc = total
                    spot_usdc_hold = float(balance.get('hold', 0))

            # Spot 'total' includes 'hold' (margin locked for isolated perp positions),
            # which is already counted in perp_equity. Subtract to avoid double-counting.
            spot_usdc_free = spot_usdc - spot_usdc_hold
            total_usdc = perp_equity + spot_usdc_free

            result = {
                'USDC': total_usdc,
                'available': available_margin + spot_usdc_free,
                'spot_balances': spot_balances,
                'perp_equity': perp_equity,
                'stale': False,
            }
            self.last_known_balances = result
            return result
        except Exception as e:
            import traceback
            self.log(f"Error fetching balances: {type(e).__name__}: {e}\n{traceback.format_exc()}", 'error')
            # Fall back to last known-good balances rather than $0 — a transient
            # API error must not be mistaken for an empty account and trip
            # MIN_EQUITY_FLOOR (entries get blocked + the dashboard shows $0.00).
            return {**self.last_known_balances, 'stale': True}
    
    def get_position_details(self) -> List[Dict[str, Any]]:
        """Get all current position details and record whether the read was known."""
        all_positions = []
        self._position_state_known = True
        if not config.HYPERLIQUID_ADDRESS:
            return all_positions
            
        try:
            state = self.info.user_state(config.HYPERLIQUID_ADDRESS)
            for pos in state.get('assetPositions', []):
                p = pos['position']
                szi = float(p['szi'])
                if szi != 0:
                    all_positions.append({
                        'coin': p['coin'],
                        'side': 'long' if szi > 0 else 'short',
                        'size': abs(szi),
                        'entry_price': float(p['entryPx']),
                        'unrealized_pnl': float(p.get('unrealizedPnl', 0)),
                        'leverage': p.get('leverage', {}).get('value', 0) if isinstance(p.get('leverage'), dict) else 0
                    })
            return all_positions
        except Exception as e:
            self._position_state_known = False
            self.log(f"Error fetching position details: {e}", 'error')
            return all_positions
    
    def get_position(self) -> str:
        """Get current position for symbol."""
        positions = self.get_position_details()
        for pos in positions:
            if pos['coin'] == self.symbol:
                return pos['side']
        return 'none'
    
    def _get_symbol_position_detail(self) -> Optional[Dict[str, Any]]:
        """Return a known current exchange position detail, if present."""
        try:
            positions = self.get_position_details()
            if not getattr(self, '_position_state_known', True):
                self.log("Could not reconcile position detail: exchange state is unknown", 'error')
                return None
            return next((p for p in positions if p.get('coin') == self.symbol), None)
        except Exception as e:
            self.log(f"Could not reconcile position detail: {e}", 'error')
            return None

    def _record_filled_entry_state(self, side: str, fallback_price: float) -> None:
        """Record entry state from exchange truth, falling back only if unavailable."""
        pos = self._get_symbol_position_detail()
        if pos and pos.get('side') == side and pos.get('entry_price', 0) > 0:
            self.last_trade_price = float(pos['entry_price'])
        else:
            self.last_trade_price = fallback_price
        self.last_action = side.upper()

    def _set_position_risk_metadata(self, strategy: str, stop_loss_pct: float = None) -> None:
        """Freeze the entry stop distance used by sizing and the exchange stop."""
        self.position_strategy = strategy
        if strategy == "trend-follow":
            default_stop = getattr(config, 'TREND_FOLLOW_SL_PCT', 2.5)
            self.position_take_profit_pct = getattr(config, 'TREND_FOLLOW_TP_PCT', 6.0)
        else:
            default_stop = getattr(config, 'STOP_LOSS_PCT', 4.0)
            self.position_take_profit_pct = getattr(config, 'TAKE_PROFIT_PCT', 12.0)
        self.position_stop_loss_pct = (
            max(float(stop_loss_pct), 0.0) if stop_loss_pct is not None else default_stop
        )

    def _authoritative_stop_price(self, entry_price: float, side: str, stop_loss_pct: float) -> float:
        """Calculate the single rounded stop price used for risk and protection."""
        raw_stop = (
            entry_price * (1 - stop_loss_pct / 100)
            if side == 'long'
            else entry_price * (1 + stop_loss_pct / 100)
        )
        return self._round_price(raw_stop)

    def _enforce_long_only_signal(self, signal: str) -> str:
        """Centrally deny every short entry signal while preserving short exits."""
        if getattr(config, "LONG_ONLY_MODE", False) and signal == "SHORT":
            self.log("🚫 SHORT entry denied by LONG_ONLY_MODE")
            return "HOLD"
        return signal

    def _evaluate_long_entry_policy(
        self, price: float, stop_distance_pct: float, feed_healthy: bool,
        funding_cost_pct: float = None,
    ):
        """Evaluate measured costs; funding_cost_pct is an hourly rate converted
        over the configured expected holding horizon, not a raw hourly percent.
        Missing research inputs fail closed.
        """
        expected_move = getattr(config, "RESEARCH_EXPECTED_GROSS_MOVE_PCT", None)
        feed_age = getattr(config, "RESEARCH_EXPECTED_MOVE_FEED_AGE_SECONDS", None)
        if (
            not getattr(config, "RESEARCH_EXPECTED_MOVE_INPUT_ENABLED", False)
            or expected_move is None
            or feed_age is None
        ):
            self._log_denial("long-only", "EXPECTED_MOVE", "expected move/age input unavailable; fail-closed")
            return None
        policy = LongOnlyPolicy(PolicyConfig())
        costs = CostModel(
            round_trip_fee_pct=getattr(config, "ROUND_TRIP_FEE_PCT", 0.0),
            slippage_pct=getattr(config, "SLIPPAGE_PCT", 0.0),
            funding_pct=(
                estimate_cumulative_funding_cost_pct(
                    funding_cost_pct,
                    getattr(config, "FUNDING_HORIZON_HOURS", 24.0),
                )
                if funding_cost_pct is not None
                else getattr(config, "FUNDING_COST_PCT", 0.0)
            ),
            adverse_selection_pct=getattr(config, "ADVERSE_SELECTION_PCT", 0.0),
        )
        decision = policy.evaluate(
            EntryContext(
                side="long",
                expected_gross_move_pct=expected_move,
                stop_distance_pct=stop_distance_pct,
                feed_age_seconds=feed_age,
                required_feeds_healthy=feed_healthy,
            ),
            costs,
        )
        if not decision.accepted:
            self._log_denial("long-only", "AFTER_COST_POLICY", decision.reason, {
                "expected_net_move_pct": decision.expected_net_move_pct,
                "cost_pct": decision.cost_pct,
                "reward_risk": decision.reward_risk,
            })
            return None
        return decision

    def _place_protective_stop(self, side: str) -> bool:
        """Install a native stop only after a known position is reconciled.

        Exchange reads and order placement can transiently fail immediately after
        a fill.  Retry the complete read/place/reconcile cycle, and fail closed
        after the bounded attempts.
        """
        if config.DRY_RUN:
            return True
        if not hyperliquid_live_actions_allowed() or not self.exchange:
            self.log("Protective stop blocked: live opt-ins are not satisfied", "error")
            return False
        max_attempts = 3
        for attempt in range(max_attempts):
            pos = self._get_symbol_position_detail()
            if not pos or pos.get('side') != side:
                self.log(
                    f"⚠️ Cannot place protective stop: position state unavailable "
                    f"(attempt {attempt + 1}/{max_attempts})",
                    'error',
                )
            else:
                try:
                    entry_price = float(pos.get('entry_price', 0))
                    size = round(float(pos.get('size', 0)), 4)
                    stop_pct = self.position_stop_loss_pct or getattr(config, 'STOP_LOSS_PCT', 4.0)
                    if entry_price <= 0 or size <= 0:
                        raise ValueError(f"invalid position {pos}")
                    stop_price = self._authoritative_stop_price(entry_price, side, stop_pct)
                    is_buy = side == 'short'  # reduce-only close: buy shorts, sell longs
                    result = self.exchange.order(
                        self.symbol,
                        is_buy,
                        size,
                        stop_price,
                        order_type={"trigger": {"triggerPx": stop_price, "isMarket": True, "tpsl": "sl"}},
                        reduce_only=True,
                    )
                    statuses = result.get("response", {}).get("data", {}).get("statuses", []) if isinstance(result, dict) else []
                    accepted_order_id = None
                    if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
                        resting = statuses[0].get("resting")
                        if isinstance(resting, dict):
                            order_id = resting.get("oid")
                            if (
                                isinstance(order_id, (int, str))
                                and not isinstance(order_id, bool)
                                and str(order_id).strip()
                            ):
                                accepted_order_id = order_id
                    if (
                        isinstance(result, dict)
                        and result.get("status") == "ok"
                        and accepted_order_id is not None
                    ):
                        # A successful order response is only accepted while the
                        # exchange has returned a real resting protective-order
                        # identifier and the position remains reconciled.
                        confirmed = self._get_symbol_position_detail()
                        if confirmed and confirmed.get('side') == side and float(confirmed.get('size', 0)) > 0:
                            self.log(
                                f"🛡️ Protective stop placed @ ${stop_price:.2f} "
                                f"({side}, size {size}, oid {accepted_order_id})"
                            )
                            return True
                        self.log("⚠️ Protective stop response received but position reconciliation is unknown", 'error')
                    else:
                        self.log(f"⚠️ Protective stop rejected or malformed: {result}", 'error')
                except Exception as e:
                    self.log(f"⚠️ Protective stop placement failed: {e}", 'error')
            if attempt + 1 < max_attempts:
                time.sleep(1)
        return False

    def _normalize_stop_inputs(
        self, side: str, entry_price: float, stop_price: float = None,
        stop_loss_pct: float = None,
    ) -> Optional[tuple]:
        """Return one rounded stop and its matching distance, or reject ambiguity."""
        try:
            entry_price = float(entry_price)
            if not math.isfinite(entry_price) or entry_price <= 0:
                raise ValueError("invalid entry price")
            if stop_loss_pct is None and stop_price is None:
                stop_loss_pct = (
                    getattr(config, "TREND_FOLLOW_SL_PCT", 2.5)
                    if self.active_regime == "uptrend"
                    else getattr(config, "STOP_LOSS_PCT", 4.0)
                )
            if stop_loss_pct is not None:
                stop_loss_pct = float(stop_loss_pct)
                if not math.isfinite(stop_loss_pct) or stop_loss_pct < 0:
                    raise ValueError("invalid stop loss percentage")
            if stop_price is not None:
                supplied_stop = float(stop_price)
                if not math.isfinite(supplied_stop):
                    raise ValueError("invalid stop price")
                if (side == "long" and supplied_stop >= entry_price) or (
                    side == "short" and supplied_stop <= entry_price
                ):
                    raise ValueError("stop price is not protective for entry side")
                if stop_loss_pct is not None:
                    pct_stop = (
                        entry_price * (1 - stop_loss_pct / 100)
                        if side == "long"
                        else entry_price * (1 + stop_loss_pct / 100)
                    )
                    tick_size = 0.1 if self.symbol == "ETH" else (1.0 if self.symbol == "BTC" else 0.01)
                    if abs(supplied_stop - pct_stop) > (tick_size / 2) + 1e-9:
                        self.log(
                            f"🚫 Entry denied: stop_price ${supplied_stop:.8f} conflicts with "
                            f"stop_loss_pct {stop_loss_pct:.8f}%", "error"
                        )
                        return None
                authoritative_stop = self._round_price(supplied_stop)
            else:
                authoritative_stop = self._authoritative_stop_price(entry_price, side, stop_loss_pct)

            if (side == "long" and authoritative_stop >= entry_price) or (
                side == "short" and authoritative_stop <= entry_price
            ):
                raise ValueError("normalized stop price is not protective")
            authoritative_pct = abs(entry_price - authoritative_stop) / entry_price * 100
            return authoritative_stop, authoritative_pct
        except (TypeError, ValueError, OverflowError) as exc:
            self.log(f"🚫 Entry denied: invalid stop inputs ({exc})", "error")
            return None

    def open_position(self, side: str, amount_usd: float, price: float, *, equity: float = None, stop_price: float = None, stop_loss_pct: float = None) -> bool:
        """Open a fixed-risk position using a post-only limit order only."""
        # analyze_and_trade performs multiple exchange reads. A later transient
        # failure must never fall through to this final order seam with an empty
        # position result; exits remain available through close_position().
        if not getattr(self, '_position_state_known', True):
            self.log("🚫 Entry denied: exchange position state is unknown", "error")
            return False
        if getattr(config, "LONG_ONLY_MODE", False) and side != "long":
            self.log(f"🚫 {side.upper()} entry denied by LONG_ONLY_MODE")
            return False
        if self.leverage != 1:
            self.log("🚫 Entry denied: long-only baseline requires isolated 1x", "error")
            return False
        if getattr(config, 'PAUSE_NEW_ENTRIES', False):
            self.log(f"⏸️ New entries paused — blocking {side.upper()} open")
            return False

        is_buy = side == 'long'
        # amount_usd is retained for log/API compatibility only. Entry quantity
        # is fixed-risk and not derived from available margin or TRADE_PERCENT.
        equity = getattr(self, "equity_for_sizing", 0.0) if equity is None else equity
        normalized_stops = self._normalize_stop_inputs(side, price, stop_price, stop_loss_pct)
        if normalized_stops is None:
            return False
        stop_price, stop_loss_pct = normalized_stops
        size = compute_position_size(
            equity=equity,
            entry_price=price,
            stop_price=stop_price,
            risk_fraction=getattr(config, "RISK_PER_TRADE_FRACTION", 0.005),
            notional_cap_fraction=getattr(config, "NOTIONAL_CAP_FRACTION", 0.25),
        )
        # Hyperliquid ETH requires max 4 decimal places
        size = round(abs(size), 4)
        if size <= 0:
            self.log("🚫 Fixed-risk sizing produced no valid quantity")
            return False
        
        # Calculate aggressive limit price (inside the spread for faster fill)
        # For LONG: bid slightly above best bid
        # For SHORT: ask slightly below best ask
        limit_price = self._calculate_limit_price(price, is_buy)
        
        amount_usd = size * limit_price
        self.log(f"🟢 OPEN {side.upper()} - Notional: ${amount_usd:.2f} @ ${limit_price:.2f} (fixed-risk quantity: {size})")
        
        if config.DRY_RUN:
            self.log("[DRY RUN] Position open simulated")
            self.last_trade_price = limit_price
            self.last_action = side.upper()
            return True
        if not hyperliquid_live_actions_allowed():
            self.log("🚫 Entry denied: live environment and --live opt-ins are required", "error")
            return False

        def abort_entry() -> bool:
            self._cancel_entry_orders()
            return False

        # Enforce account leverage before every entry — isolated margin at config.LEVERAGE (1x).
        # At 1x isolated, liquidation = ~$0 (unreachable). Without this, the HL account retains
        # whatever leverage was set manually (observed 20x → liquidation only ~$17 below hard SL).
        try:
            leverage_result = self.exchange.update_leverage(self.leverage, self.symbol, is_cross=False)
            if isinstance(leverage_result, dict) and leverage_result.get("status") not in (None, "ok"):
                self.log(f"⚠️ Could not confirm leverage: {leverage_result} — aborting entry", 'error')
                return abort_entry()
            self.log(f"   🔧 Leverage set: {self.leverage}x isolated")
        except Exception as lev_err:
            self.log(f"⚠️ Could not set leverage: {lev_err} — aborting entry", 'error')
            return abort_entry()

        try:
            # Place limit order with ALO (Add Liquidity Only) TIF for maker rebates
            order_result = self.exchange.order(
                self.symbol,
                is_buy,
                size,
                limit_price,
                order_type={"limit": {"tif": "Alo"}},  # Post-only for maker rebates
                reduce_only=False
            )
            
            if order_result["status"] == "ok":
                # Extract order ID for tracking
                statuses = order_result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and statuses[0].get("filled"):
                    # Order filled immediately (rare for ALO, but possible if book moved)
                    strategy = "trend-follow" if self.active_regime == "uptrend" else "flush"
                    self._record_filled_entry_state(side, limit_price)
                    self._set_position_risk_metadata(strategy, stop_loss_pct)
                    if not self._place_protective_stop(side):
                        self.close_position()
                        return abort_entry()
                    self.log(f"✅ Order filled immediately at ${limit_price:.2f}")
                    return True
                else:
                    # Order posted to book - wait for fill or chase
                    order_id = statuses[0].get("resting", {}).get("oid") if statuses else None
                    if order_id:
                        self.log(f"📋 Limit order posted (OID: {order_id}), waiting for fill...")
                        # Start chase mechanism in background
                        filled = self._chase_order(order_id, side, size, limit_price, max_attempts=3)
                        if filled:
                            strategy = "trend-follow" if self.active_regime == "uptrend" else "flush"
                            self._record_filled_entry_state(side, limit_price)
                            self._set_position_risk_metadata(strategy, stop_loss_pct)
                            if not self._place_protective_stop(side):
                                self.close_position()
                                return abort_entry()
                            return True
                        else:
                            # A cancel/fill race can leave a partial position even
                            # though the requested entry was not completed. Reconcile
                            # and protect it before reporting failure.
                            partial_position = self._get_symbol_position_detail()
                            if partial_position and partial_position.get("side") == side:
                                strategy = "trend-follow" if self.active_regime == "uptrend" else "flush"
                                self._set_position_risk_metadata(strategy, stop_loss_pct)
                                if not self._place_protective_stop(side):
                                    self.close_position()
                                else:
                                    self.log("🛡️ Protective stop installed for partial chase fill")
                            self.log("❌ Order not filled after chase attempts", 'error')
                            return abort_entry()
                    else:
                        self.log(f"⚠️ Order posted but no OID returned: {order_result}", 'error')
                        return abort_entry()
            else:
                self.log(f"Open failed: {order_result}", 'error')
                return abort_entry()
        except Exception as e:
            self.log(f"Open error: {e}", 'error')
            return abort_entry()

    def _round_price(self, price: float) -> float:
        """Round price to exchange tick size (1.0 for BTC, 0.1 for ETH)."""
        if self.symbol == "ETH":
            tick_size = 0.1
        elif self.symbol == "BTC":
            tick_size = 1.0
        else:
            # General rule: at most 5 significant figures for most HL assets
            # But default to 0.01 for others if not specified
            tick_size = 0.01

        rounded = round(price / tick_size) * tick_size
        
        # Use appropriate decimal places for formatting
        if tick_size >= 1.0:
            return float(f"{rounded:.0f}")
        elif tick_size >= 0.1:
            return float(f"{rounded:.1f}")
        else:
            return float(f"{rounded:.2f}")

    def _calculate_limit_price(self, mid_price: float, is_buy: bool) -> float:
        """Calculate aggressive limit price inside the spread."""
        spread_buffer = 0.001  # 0.1% tighter than mid for faster fill
        
        if is_buy:
            raw_price = mid_price * (1 - spread_buffer)
        else:
            raw_price = mid_price * (1 + spread_buffer)
            
        return self._round_price(raw_price)

    def _reconciled_entry_size(self, side: str) -> float:
        """Return exchange-reported size for this entry side, or zero."""
        try:
            detail = self._get_symbol_position_detail()
            if detail and detail.get("side") == side:
                return max(0.0, float(detail.get("size", 0.0)))
        except (TypeError, ValueError):
            self.log("⚠️ Could not parse reconciled entry size", "error")
        return 0.0

    def _chase_order(self, order_id: int, side: str, size: float, initial_price: float, max_attempts: int = 3) -> bool:
        """Chase an ALO order while reconciling fills around every cancellation.

        A cancel can race a fill.  Therefore every cancel is confirmed absent and
        the exchange position is measured before any replacement is sized.
        """
        if not config.DRY_RUN and not hyperliquid_live_actions_allowed():
            self.log("Chase blocked: live environment and --live opt-ins are required", "error")
            return False
        is_buy = side == 'long'
        original_size = float(size)

        def fail_entry() -> bool:
            # Best effort cleanup prevents an errored replacement from filling
            # in a later cycle. The caller still fails closed if reads/cancels fail.
            self._cancel_entry_orders()
            return False

        for attempt in range(max_attempts):
            time.sleep(2)
            try:
                open_orders = self.info.open_orders(self.address)
                is_open = any(str(o.get('oid')) == str(order_id) for o in open_orders)
                if not is_open:
                    actual_size = self._reconciled_entry_size(side)
                    if actual_size > 0:
                        self.log(f"✅ Reconciled {actual_size} filled during chase (attempt {attempt + 1})")
                        return True if actual_size >= original_size else fail_entry()
                    self.log(f"⚠️ Order {order_id} no longer in book and no position was reconciled")
                    return fail_entry()

                self.log(f"🔄 Repricing order (attempt {attempt + 1}/{max_attempts})")
                cancel_result = self.exchange.cancel(self.symbol, order_id)
                if not isinstance(cancel_result, dict) or cancel_result.get("status") != "ok":
                    self._reconciled_entry_size(side)
                    self.log(f"⚠️ Cancel failed; aborting chase: {cancel_result}", 'error')
                    return fail_entry()

                # Confirm the cancellation before reusing the order's quantity.
                post_cancel_orders = self.info.open_orders(self.address)
                if any(str(o.get('oid')) == str(order_id) for o in post_cancel_orders):
                    self._reconciled_entry_size(side)
                    self.log(f"⚠️ Order {order_id} still open after cancel; aborting chase", 'error')
                    return fail_entry()

                filled_size = self._reconciled_entry_size(side)
                remaining_size = round(original_size - filled_size, 4)
                if remaining_size <= 0:
                    self.log(f"✅ Original quantity reconciled as filled ({filled_size})")
                    return True

                current_price = self.get_current_price()
                if not current_price:
                    return fail_entry()
                aggression = 0.002 * (attempt + 1)
                raw_price = current_price * (1 - aggression if is_buy else 1 + aggression)
                new_limit_price = self._round_price(raw_price)
                new_order = self.exchange.order(
                    self.symbol,
                    is_buy,
                    remaining_size,
                    new_limit_price,
                    order_type={"limit": {"tif": "Alo"}},
                    reduce_only=False,
                )
                if not isinstance(new_order, dict) or new_order.get("status") != "ok":
                    self.log(f"⚠️ Exchange order call failed: {new_order}", 'error')
                    return fail_entry()
                statuses = new_order.get("response", {}).get("data", {}).get("statuses", [])
                if not statuses or not isinstance(statuses[0], dict):
                    return fail_entry()
                if statuses[0].get("filled"):
                    actual_size = self._reconciled_entry_size(side)
                    return True if actual_size >= original_size else fail_entry()
                order_id = statuses[0].get("resting", {}).get("oid")
                if not order_id:
                    if "error" in statuses[0]:
                        self.log(f"⚠️ New order failed: {statuses[0]['error']}", 'error')
                    return fail_entry()
            except Exception as e:
                self._reconciled_entry_size(side)
                self.log(f"Chase error: {e}", 'error')
                return fail_entry()

        # Cancel and verify the final replacement. Reconcile position even when
        # cancellation fails so the caller can install protection for a partial fill.
        if order_id:
            try:
                cancel_result = self.exchange.cancel(self.symbol, order_id)
                if not isinstance(cancel_result, dict) or cancel_result.get("status") != "ok":
                    self._reconciled_entry_size(side)
                    self.log(f"⚠️ Final post-only cancel failed: {cancel_result}", 'error')
                    return fail_entry()
                open_orders = self.info.open_orders(self.address)
                if any(str(o.get('oid')) == str(order_id) for o in open_orders):
                    self._reconciled_entry_size(side)
                    self.log(f"⚠️ Final order {order_id} still open after cancel; aborting entry", 'error')
                    return fail_entry()
                actual_size = self._reconciled_entry_size(side)
                if actual_size >= original_size:
                    return True
                return fail_entry()
            except Exception as e:
                self._reconciled_entry_size(side)
                self.log(f"Final post-only cancel errored: {e}", 'error')
                return fail_entry()

        self.log("⚠️ Post-only chase exhausted after verified cancel; aborting entry", 'error')
        return fail_entry()

    def _cancel_entry_orders(self) -> bool:
        """Cancel and verify this symbol's non-reduce-only orders only."""
        if config.DRY_RUN:
            return True
        if not hyperliquid_live_actions_allowed() or not self.exchange:
            self.log("Order cancellation blocked: live opt-ins are not satisfied", "error")
            return False
        try:
            open_orders = self.info.open_orders(self.address)
            symbol_orders = [
                o for o in open_orders
                if o.get("coin") == self.symbol and not bool(o.get("reduceOnly", False))
            ]
            for order in symbol_orders:
                result = self.exchange.cancel(self.symbol, order["oid"])
                if not isinstance(result, dict) or result.get("status") != "ok":
                    self.log(f"⚠️ Cancel entry order OID {order['oid']} failed: {result}", "error")
                    return False
                self.log(f"🧹 Cancelled entry order OID {order['oid']}")
            remaining = self.info.open_orders(self.address)
            stale = [
                o for o in remaining
                if o.get("coin") == self.symbol and not bool(o.get("reduceOnly", False))
            ]
            if stale:
                self.log(f"⚠️ Entry orders remain after cancellation: {stale}", "error")
                return False
            return True
        except Exception as exc:
            self.log(f"⚠️ Cancel entry orders failed: {exc}", "error")
            return False

    def _cancel_all_open_orders(self) -> bool:
        """Cancel stale symbol entry orders while preserving protective stops."""
        return self._cancel_entry_orders()

    def close_position(self) -> bool:
        """Close current position, treating unknown exchange state as failure."""
        if not config.DRY_RUN and not hyperliquid_live_actions_allowed():
            self.log("Close blocked: live environment and --live opt-ins are required", "error")
            return False
        position = self.get_position()
        if not getattr(self, '_position_state_known', True):
            self.log("⚠️ Cannot close: exchange position state is unknown", 'error')
            return False
        if not self._cancel_entry_orders():
            self.log("Close denied: residual entry orders could not be cleared", "error")
            return False
        if position == 'none':
            self.log("No position to close")
            return True
        
        price = self.get_current_price()
        if not price:
            return False
        
        self.log(f"🔴 CLOSE {position.upper()} @ ${price:.2f}")
        
        if self.last_trade_price:
            if position == 'long':
                pnl_pct = ((price / self.last_trade_price) - 1) * 100
            else:
                pnl_pct = ((self.last_trade_price / price) - 1) * 100
            self.log(f"   P&L: {pnl_pct:+.2f}%")
        
        if config.DRY_RUN:
            self.log("[DRY RUN] Position close simulated")
            self.last_trade_price = None
            self.last_action = 'CLOSE'
            return True
        
        try:
            result = self.exchange.market_close(self.symbol)
            if result.get("status") != "ok":
                self._cancel_entry_orders()
                self.log(f"Close failed: {result}", 'error')
                return False

            time.sleep(3)
            if not self._cancel_entry_orders():
                self.log("Close failed: residual entry orders remain", "error")
                return False
            verified_position = self.get_position()
            if not getattr(self, '_position_state_known', True):
                self.log("Close failed verification: exchange position state is unknown", 'error')
                return False
            if verified_position != 'none':
                self.log(f"Close failed verification: exchange still reports {verified_position.upper()} position", 'error')
                return False

            self.last_trade_price = None
            self.last_action = 'CLOSE'
            return True
        except Exception as e:
            self._cancel_entry_orders()
            self.log(f"Close error: {e}", 'error')
            return False
    
    def fetch_okx_liquidations(self) -> list:
        """Fetch recent liquidations from OKX API for ETH-USDT-SWAP."""
        base_url = "https://www.okx.com"
        endpoint = "/api/v5/public/liquidation-orders"
        params = {
            "instType": "SWAP",
            "uly": "ETH-USDT",
            "state": "filled",
            "limit": "100"
        }
        
        liquidations = []
        after = None
        start_time = int(time.time() * 1000) - (24 * 3600 * 1000)
        
        while True:
            if after:
                params["after"] = after
            try:
                response = requests.get(base_url + endpoint, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data["code"] != "0":
                    self.log(f"OKX API error: {data['msg']}", 'error')
                    break
                
                items = data["data"]
                if not items:
                    break
                
                last_item_with_details = None
                for item in items:
                    details = item.get("details", [])
                    if details:
                         last_item_with_details = item
                    for detail in details:
                        ts = int(detail["ts"])
                        if ts < start_time:
                            return liquidations
                        liquidations.append({
                            "bkPx": float(detail["bkPx"]),
                            "sz": float(detail["sz"]),
                            "side": detail["side"],
                            "ts": ts
                        })
                
                if last_item_with_details and "details" in last_item_with_details and last_item_with_details["details"]:
                    after = last_item_with_details["details"][-1]["ts"]
                else:
                    break
                
            except Exception as e:
                self.log(f"Error fetching OKX liquidations: {e}", 'error')
                break
            
            time.sleep(0.5)
        
        return liquidations
    
    def analyze_liquidations(self, liquidations: list, current_price: float, is_upper: bool = False) -> list:
        """Analyze liquidations to find significant clusters."""
        clusters = defaultdict(lambda: {"total_usd": 0, "count": 0, "prices": []})
        
        contract_size = 0.1
        
        for liq in liquidations:
            price = liq["bkPx"]
            if (is_upper and price <= current_price) or (not is_upper and price >= current_price):
                continue
            usd_value = liq["sz"] * price * contract_size
            bin_price = round(price / 10) * 10
            clusters[bin_price]["total_usd"] += usd_value
            clusters[bin_price]["count"] += 1
            clusters[bin_price]["prices"].append(price)
        
        significant_levels = []
        for bin_price, info in clusters.items():
            if info["total_usd"] > self.threshold_amount / 10:  # Adjust as needed
                avg_price = sum(info["prices"]) / info["count"] if info["count"] else bin_price
                significant_levels.append(avg_price)
        
        return sorted(significant_levels, reverse=is_upper)
    
    def analyze_and_trade(self) -> Dict[str, Any]:
        result = {
            'timestamp': datetime.now().isoformat(),
            'price': None,
            'signal': None,
            'action_taken': None,
            'balances': None
        }
        
        current_price = self.get_current_price()
        if not current_price:
            self.log("Could not fetch current price", 'error')
            return result

        # Update macro regime from FRED (cached every 6h — non-blocking on failure)
        self._update_macro_regime()

        # Update VPIN from recent trade flow (non-blocking — logs debug on failure)
        self._update_vpin()

        # Calculate technical filters
        rsi_val = 50.0
        ema_val = current_price
        ema_short_val = current_price  # EMA-50 for regime detection
        atr_val = 0.0
        closing_prices = []
        funding_rate = self.get_funding_rate() # Also updates self.cached_oi
        funding_pct = funding_rate * 100 # Convert to % for display
        current_oi = self.cached_oi
        
        try:
            # Fetch context for indicators
            end_time = int(time.time() * 1000)
            # Fetch enough for EMA 200 (need ~300 candles for stability)
            start_time = end_time - (3600 * 1000 * 300) 
            candles = self.info.candles_snapshot(self.symbol, config.TIMEFRAME, start_time, end_time)
            
            if candles:
                closing_prices = [float(c['c']) for c in candles]
                high_prices = [float(c['h']) for c in candles]
                low_prices = [float(c['l']) for c in candles]
                
                if len(closing_prices) >= config.EMA_PERIOD:
                    rsi_val = calculate_rsi(closing_prices, config.RSI_PERIOD)
                    ema_val = calculate_ema(closing_prices, config.EMA_PERIOD)
                    ema_short_val = calculate_ema(closing_prices, config.EMA_SHORT_PERIOD)
                    atr_val = calculate_atr(high_prices, low_prices, closing_prices, config.ATR_PERIOD)
                    
                    # Track RSI divergence
                    bullish_divergence = False
                    if getattr(config, 'LOG_RSI_DIVERGENCE', False) and self.prev_rsi is not None and self.prev_price is not None:
                        if current_price < self.prev_price and rsi_val > self.prev_rsi:
                            bullish_divergence = True
                    self.prev_rsi = rsi_val

                    self.logger.debug(f"Indicators: RSI={rsi_val}, EMA50={ema_short_val}, EMA200={ema_val}, ATR={atr_val:.4f}")
                else:
                    self.logger.warning(f"Not enough candles for indicators. Got {len(closing_prices)}")
        except Exception as e:
            self.logger.error(f"Error calculating indicators: {e}")

        # Detect market regime
        # uptrend:   EMA-50 > EMA-200 AND price > EMA-200 (golden cross, price above trend)
        # downtrend: EMA-50 < EMA-200 (death cross territory)
        # range:     everything else (EMA-50 ≈ EMA-200, mixed signals)
        in_downtrend = ema_short_val < ema_val
        in_uptrend = (ema_short_val > ema_val) and (current_price > ema_val)
        regime = 'uptrend' if in_uptrend else ('downtrend' if in_downtrend else 'range')

        # Volume confirmation gate
        vol_confirmed = True  # Default to True if we can't compute
        try:
            if candles and len(candles) >= 21:
                volumes = [float(c['v']) for c in candles]
                avg_vol_20 = sum(volumes[-21:-1]) / 20  # 20-candle trailing average (exclude current)
                current_vol = volumes[-1]
                vol_threshold = avg_vol_20 * config.VOLUME_CONFIRMATION_MULT
                vol_confirmed = current_vol >= vol_threshold
                self.logger.debug(f"Volume: current={current_vol:.0f}, avg20={avg_vol_20:.0f}, threshold={vol_threshold:.0f}, confirmed={vol_confirmed}")
        except Exception:
            pass  # Fall back to True

        balances = self.get_balances()
        position = self.get_position()
        usdc_balance = balances.get('USDC', 0)
        
        result['price'] = current_price
        result['balances'] = balances
        if not getattr(self, '_position_state_known', True):
            self._log_denial(
                "execution", "POSITION_STATE_UNKNOWN",
                "Could not confirm exchange position state; entries and exits are blocked",
            )
            result['action_taken'] = 'HOLD_POSITION_STATE_UNKNOWN'
            self.log("🚫 Trading halted: exchange position state is unknown", "error")
            return result
        
        # ── OI & Funding Diagnostics ──────────────────────────────
        oi_log_str = ""
        oi_rising = False  # Computed before prev_oi update; used in SHORT filter below
        if current_oi > 0:
            if self.prev_oi is not None and self.prev_oi > 0:
                oi_delta = current_oi - self.prev_oi
                oi_delta_pct = (oi_delta / self.prev_oi) * 100
                oi_rising = oi_delta > 0
                oi_log_str = f" | OI: {current_oi:.0f} ({oi_delta_pct:+.2f}%)"
            else:
                oi_log_str = f" | OI: {current_oi:.0f}"
            self.prev_oi = current_oi

        if funding_rate < getattr(config, 'NEGATIVE_FUNDING_LOG_THRESHOLD', -0.0001):
            self.logger.info(f"📊 Funding NEGATIVE ({funding_pct:.4f}%) → crowded shorts → LONG-favorable context")
        
        # ── Cascade Risk Check ──────────────────────────────────
        cascade_signal = self.cascade_detector.evaluate()
        cascade_risk = cascade_signal.risk_level if cascade_signal else RiskLevel.NORMAL
        cascade_score = cascade_signal.risk_score if cascade_signal else 0.0
        
        if cascade_risk != RiskLevel.NORMAL:
            reasons_str = ', '.join(cascade_signal.reasons[:2]) if cascade_signal else ''
            self.log(f"⚠️ CASCADE RISK: {cascade_risk.value} (score={cascade_score:.0f}) | {reasons_str}")
        
        trade_params = self.cascade_detector.get_trading_params(
            base_size_pct=0,  # sizing is fixed-risk; cascade telemetry cannot size entries
            base_sl_mult=config.ATR_MULTIPLIER
        )

        # ── Markov Regime Signal ─────────────────────────────────
        is_trend_follow_entry = False  # distinguishes flush (mean-reversion) from trend-follow (momentum) for Markov sizing
        markov_signal = None
        if getattr(config, 'USE_MARKOV_FILTER', False) and closing_prices:
            try:
                markov_signal = get_markov_signal(
                    closing_prices,
                    window=getattr(config, 'MARKOV_WINDOW', 20),
                    threshold=getattr(config, 'MARKOV_THRESHOLD', 0.02),
                )
                if markov_signal:
                    self.logger.debug(
                        f"Markov: regime={markov_signal['current_regime']}, "
                        f"signal={markov_signal['signal']:.3f}, "
                        f"stay={markov_signal['stay_prob']:.3f}, "
                        f"bear_base={markov_signal['bear_baseline']:.3f}, "
                        f"size_scalar={markov_signal['size_scalar']:.3f}"
                    )
            except Exception as e:
                self.logger.warning(f"Markov signal failed: {e}")

        self.logger.info(f"─" * 60)
        self.logger.info(f"ETH: ${current_price:.2f}")
        self.logger.info(f"Position: {position.upper()} | USDC: ${usdc_balance:.2f} | Funding: {funding_pct:.6f}%{oi_log_str} | Cascade: {cascade_risk.value} ({cascade_score:.0f}/100)")
        
        if time.time() - self.last_liquidations_time > 3600:
            # Refresh REST liquidations hourly
            okx_liqs = self.fetch_okx_liquidations()
            self.last_liquidations = okx_liqs
            self.last_liquidations_time = time.time()
            
            # Record fetched liquidations to local file
            self.recorder.save_liquidations(okx_liqs)
            
        # Merge REST liquidations with live WebSocket liquidations
        current_liquidations = list(self.last_liquidations)
        if self.liquidation_buffer:
            current_liquidations.extend(list(self.liquidation_buffer))
        
        signal = "HOLD"
        description = "Waiting for flush signal"
        _lower_levels_all = []
        _upper_levels_all = []

        if current_liquidations:
            lower_levels = self.analyze_liquidations(current_liquidations, current_price, is_upper=False)
            upper_levels = self.analyze_liquidations(current_liquidations, current_price, is_upper=True)
            _lower_levels_all = lower_levels
            _upper_levels_all = upper_levels

            if lower_levels:
                blue_bottom = min(lower_levels)
                entry_threshold = blue_bottom * (1 + self.entry_buffer_pct / 100)
                if current_price > entry_threshold:
                    # Log proximity to nearest lower cluster for diagnostics
                    nearest_lower = max(lower_levels)  # closest below current price
                    gap_to_nearest = (current_price - nearest_lower) / current_price * 100
                    gap_to_trigger = (current_price - entry_threshold) / current_price * 100
                    div_str = " | ⚠️ BULLISH RSI DIVERGENCE" if bullish_divergence else ""
                    self.logger.debug(
                        f"📊 LONG proximity: nearest cluster=${nearest_lower:.0f} ({gap_to_nearest:.1f}% below), "
                        f"trigger=${entry_threshold:.0f} (need -{gap_to_trigger:.1f}% drop){div_str}"
                    )
                if current_price <= entry_threshold:
                    # LONG filters
                    can_long = True
                    # Fix 1: Regime-conditional trend filter — only block in confirmed downtrend
                    if config.USE_TREND_FILTER and in_downtrend and current_price < ema_val:
                        self.log(f"   🚫 Signal Filtered: Price ${current_price:.2f} < EMA200 ${ema_val:.2f} & EMA50 ${ema_short_val:.2f} < EMA200 (Confirmed downtrend)")
                        can_long = False
                    # Fix 3: Range-bear long filter (Mar 20, 2026)
                    # Range regime means EMA50 ≥ EMA200 — Fix 1 never fires. But price can still be below
                    # EMA200 and declining (staircase-down). Apply stricter RSI threshold in this case.
                    if config.USE_TREND_FILTER and not in_downtrend and current_price < ema_val:
                        range_bear_threshold = getattr(config, 'RSI_OVERSOLD_RANGE_BEAR', 32)
                        if rsi_val > range_bear_threshold:
                            self.log(f"   🚫 Signal Filtered: Range-Bear — price ${current_price:.2f} < EMA200 ${ema_val:.2f}, RSI {rsi_val:.1f} > {range_bear_threshold} (need deeper oversold below EMA200)")
                            can_long = False
                    # Fix 2: Stricter RSI in downtrend (30 vs 40)
                    rsi_threshold = config.RSI_OVERSOLD_DOWNTREND if in_downtrend else config.RSI_OVERSOLD
                    if config.USE_RSI_FILTER and rsi_val > rsi_threshold:
                        regime_label = f"downtrend, threshold={rsi_threshold}" if in_downtrend else f"normal, threshold={rsi_threshold}"
                        self.log(f"   🚫 Signal Filtered: RSI {rsi_val} not oversold ({regime_label})")
                        can_long = False
                    if funding_rate > config.MAX_FUNDING_RATE:
                        self.log(f"   🚫 Signal Filtered: High Positive Funding ({funding_pct:.4f}%) - Expensive to Long")
                        can_long = False
                    # Macro regime filter — block longs in global fear/credit-stress regime
                    if can_long and getattr(config, 'USE_MACRO_FILTER', False) and self.macro_snapshot:
                        macro_regime = self.macro_snapshot.get('regime', 'RISK_ON')
                        if macro_regime == 'RISK_OFF' and getattr(config, 'MACRO_RISK_OFF_BLOCK_LONG', True):
                            vix_str = f"VIX={self.macro_snapshot.get('vix', 'N/A')}"
                            yld_str = f"10Y2Y={self.macro_snapshot.get('yield_spread', 'N/A')}"
                            self.log(f"   🌍 Signal Filtered: Macro RISK_OFF ({vix_str}, {yld_str}) — blocking long in fear regime")
                            can_long = False
                    if can_long and not trade_params["allow_long"]:
                        self.log(f"   🛡️ Long blocked by cascade detector ({cascade_risk.value})")
                        can_long = False
                    if can_long and config.USE_VOLUME_FILTER and not vol_confirmed:
                        self.log(f"   🚫 Signal Filtered: Low volume — no flush conviction")
                        can_long = False
                    # VPIN filter: high toxicity = informed one-sided flow — do not fade
                    if can_long and getattr(config, 'USE_VPIN_FILTER', False) and self.current_vpin is not None:
                        vpin_extreme = getattr(config, 'VPIN_EXTREME_THRESHOLD', 0.80)
                        vpin_danger = getattr(config, 'VPIN_DANGER_THRESHOLD', 0.65)
                        if self.current_vpin >= vpin_extreme:
                            self.log(f"   🚫 Signal Filtered: VPIN {self.current_vpin:.3f} ≥ {vpin_extreme} — extreme one-sided flow, all entries blocked")
                            can_long = False
                        elif self.current_vpin >= vpin_danger:
                            self.log(f"   🚫 Signal Filtered: VPIN {self.current_vpin:.3f} ≥ {vpin_danger} — informed flow detected, skip flush long")
                            can_long = False

                    # Markov stability gate — flush is mean-reversion: bear regime block does NOT apply.
                    # Mean reversion fires IN bear/oversold regimes by design. Only gate on stay_prob:
                    # an unstable/transitioning regime (low stay_prob) is bad for any entry.
                    if can_long and markov_signal and getattr(config, 'USE_MARKOV_FILTER', False):
                        min_stay = getattr(config, 'MARKOV_MIN_STAY_PROB', 0.55)
                        if markov_signal['stay_prob'] < min_stay:
                            self.log(f"   📊 Markov blocked (flush): stay_prob={markov_signal['stay_prob']:.3f} < {min_stay} — regime transitioning, skip flush entry")
                            can_long = False
                        else:
                            self.log(f"   📊 Markov OK (flush): regime={markov_signal['current_regime']}, stay={markov_signal['stay_prob']:.3f}, bear_base={markov_signal['bear_baseline']:.3f} (bear-block skipped — mean-reversion)")

                    if can_long:
                        signal = "LONG"
                        is_trend_follow_entry = False
                        self.active_regime = regime  # flush entry — track current regime
                        description = f"Price entered lower liquidation zone at ~${blue_bottom:.2f}"
            
            if not upper_levels and signal == "HOLD":
                self.logger.debug(f"📊 SHORT proximity: no upper liquidation clusters above ${current_price:.0f}")

            if upper_levels and signal == "HOLD":
                blue_top = max(upper_levels)
                entry_threshold = blue_top * (1 - self.entry_buffer_pct / 100)
                if current_price < entry_threshold:
                    nearest_upper = min(upper_levels)  # closest above current price
                    gap_to_nearest = (nearest_upper - current_price) / current_price * 100
                    gap_to_trigger = (entry_threshold - current_price) / current_price * 100
                    self.logger.debug(
                        f"📊 SHORT proximity: nearest cluster=${nearest_upper:.0f} ({gap_to_nearest:.1f}% above), "
                        f"trigger=${entry_threshold:.0f} (need +{gap_to_trigger:.1f}% rally)"
                    )
                if current_price >= entry_threshold:
                    # SHORT filters
                    can_short = True
                    # Flush-short kill switch; disabled in the public baseline.
                    if not getattr(config, 'USE_FLUSH_SHORT', True):
                        self.log("🚫 Flush-SHORT disabled by config (USE_FLUSH_SHORT=False)")
                        can_short = False
                    # Flush-short regime gate: require a confirmed downtrend.
                    if can_short and not in_downtrend:
                        self.log(f"   🚫 Flush-SHORT blocked: regime='{regime}' (EMA50 ${ema_short_val:.0f} ≥ EMA200 ${ema_val:.0f}) — only short into confirmed downtrend rallies")
                        can_short = False
                    # Above EMA-200, block shorts unless EMA50 confirms a downtrend.
                    if config.USE_TREND_FILTER and current_price > ema_val and not in_downtrend:
                        self.log(f"   🚫 Signal Filtered: Price ${current_price:.2f} > EMA-200 ${ema_val:.2f} (price above baseline in non-downtrend — short not aligned)")
                        can_short = False
                    # Short RSI threshold is context-dependent:
                    # - Confirmed downtrend (EMA50<EMA200): bounces to 50-58 are the entry, 65 is almost never reached
                    # - Range-bear (range regime, price<EMA200): gradual decline, 55 threshold
                    # - Normal/uptrend: standard 65 overbought threshold
                    if in_downtrend:
                        short_rsi_threshold = getattr(config, 'RSI_OVERBOUGHT_DOWNTREND', 52)
                        rsi_threshold_label = '[downtrend threshold]'
                    elif not in_downtrend and current_price < ema_val:
                        short_rsi_threshold = getattr(config, 'RSI_OVERBOUGHT_RANGE_BEAR', 55)
                        rsi_threshold_label = '[range-bear threshold]'
                    else:
                        short_rsi_threshold = config.RSI_OVERBOUGHT
                        rsi_threshold_label = ''
                    if config.USE_RSI_FILTER and rsi_val < short_rsi_threshold:
                        self.log(f"   🚫 Signal Filtered: RSI {rsi_val:.1f} not overbought (<{short_rsi_threshold}) {rsi_threshold_label}")
                        can_short = False
                    # RSI extreme momentum ceiling: RSI≥78 means price is still squeezing
                    # up, not reversing. Entering a short here is the worst-timed entry possible.
                    if can_short and rsi_val >= config.RSI_EXTREME_OVERBOUGHT:
                        self.log(f"   🚫 Signal Filtered: RSI {rsi_val} ≥ {config.RSI_EXTREME_OVERBOUGHT} — extreme momentum, squeeze risk, not a short setup")
                        can_short = False
                    # Block SHORT when shorts are overcrowded (extreme negative funding = squeeze risk).
                    # Note: negative funding means shorts RECEIVE funding — it's cheap to short — but
                    # extreme negative funding signals the crowd is already short, raising squeeze risk.
                    if funding_rate < -config.NEGATIVE_FUNDING_SHORT_BLOCK:
                        self.log(f"   🚫 Short blocked: Extreme negative funding ({funding_pct:.4f}%) — shorts overcrowded, squeeze risk")
                        can_short = False
                    # Block short entries when cascade risk is elevated.

                    if can_short and cascade_risk == RiskLevel.ELEVATED:
                        self.log(f"   🛡️ Short blocked: Cascade ELEVATED ({cascade_score:.0f}/100) — elevated funding stress suggests squeeze risk")
                        can_short = False
                    if can_short and config.USE_VOLUME_FILTER and not vol_confirmed:
                        self.log(f"   🚫 Signal Filtered: Low volume — no flush conviction")
                        can_short = False
                    # Block shorts when rising OI suggests continuation.
                    if can_short and config.USE_OI_FILTER and oi_rising:
                        self.log(f"   🚫 Short blocked: OI rising ({current_oi:.0f}, +δ) — continuation signal, not a reversal setup")
                        can_short = False
                    # VPIN extreme threshold: both directions blocked when flow is maximally one-sided
                    if can_short and getattr(config, 'USE_VPIN_FILTER', False) and self.current_vpin is not None:
                        vpin_extreme = getattr(config, 'VPIN_EXTREME_THRESHOLD', 0.80)
                        if self.current_vpin >= vpin_extreme:
                            self.log(f"   🚫 Short blocked: VPIN {self.current_vpin:.3f} ≥ {vpin_extreme} — extreme one-sided flow, all entries blocked")
                            can_short = False

                    # Macro risk-off blocks shorts outside confirmed downtrends.
                    # In RISK_OFF, policy/news events (tariff pauses, Fed pivots) trigger violent
                    # short squeezes. In a confirmed downtrend the macro aligns with the trade;
                    # in range or uptrend, RISK_OFF relief rallies kill shorts.
                    if can_short and getattr(config, 'USE_MACRO_FILTER', False) and self.macro_snapshot:
                        macro_regime = self.macro_snapshot.get('regime', 'RISK_ON')
                        if macro_regime == 'RISK_OFF' and not in_downtrend:
                            vix_str = f"VIX={self.macro_snapshot.get('vix', 'N/A')}"
                            self.log(f"   🌍 Short blocked: Macro RISK_OFF ({vix_str}) in non-downtrend — relief rally / squeeze risk")
                            can_short = False

                    if can_short:
                        signal = "SHORT"
                        self.active_regime = regime
                        description = f"Price entered upper liquidation zone at ~${blue_top:.2f}"
        
        # ── Market State Summary (when no signal fires) ───────────
        if signal == "HOLD":
            # Effective RSI thresholds — must mirror filter logic above to avoid misleading logs
            _range_bear = not in_downtrend and current_price < ema_val
            if _range_bear:
                rsi_need_long = getattr(config, 'RSI_OVERSOLD_RANGE_BEAR', 32)
                rsi_need_short = getattr(config, 'RSI_OVERBOUGHT_RANGE_BEAR', 55)
            elif in_downtrend:
                rsi_need_long = config.RSI_OVERSOLD_DOWNTREND
                rsi_need_short = config.RSI_OVERBOUGHT
            else:
                rsi_need_long = config.RSI_OVERSOLD
                rsi_need_short = config.RSI_OVERBOUGHT
            regime_label = regime + (" [range-bear]" if _range_bear else "")
            nearest_lower = max(_lower_levels_all) if _lower_levels_all else None
            nearest_upper = min(_upper_levels_all) if _upper_levels_all else None
            lower_str = (f"${nearest_lower:.0f} ({(current_price - nearest_lower) / current_price * 100:.1f}% below)"
                         if nearest_lower else "none in range")
            upper_str = (f"${nearest_upper:.0f} ({(nearest_upper - current_price) / current_price * 100:.1f}% above)"
                         if nearest_upper else "none in range")
            long_blocked = "RSI not oversold" if rsi_val > rsi_need_long else ("no lower cluster at price" if not _lower_levels_all else "price above trigger")
            short_blocked = ("not in downtrend" if not in_downtrend
                             else ("price above EMA-200" if (config.USE_TREND_FILTER and current_price > ema_val)
                             else ("OI rising" if (config.USE_OI_FILTER and oi_rising)
                             else ("RSI not overbought" if rsi_val < rsi_need_short
                             else ("no upper cluster" if not _upper_levels_all else "price below trigger")))))
            self.logger.info(
                f"📊 Regime={regime_label} | RSI={rsi_val:.1f} "
                f"(LONG needs <{rsi_need_long}, SHORT needs >{rsi_need_short}) | "
                f"vol_ok={vol_confirmed}"
            )
            self.logger.info(
                f"📊 Clusters: lower={lower_str} | upper={upper_str}"
            )
            self.logger.info(
                f"📊 Blocked: LONG={long_blocked} | SHORT={short_blocked}"
            )

        # ── Cascade Ride Short ───────────────────────────────────
        if (signal == "HOLD" and cascade_risk == RiskLevel.CRITICAL
                and not getattr(config, "LONG_ONLY_MODE", False)):
            cascade_trend = self.cascade_detector.get_trend()
            if cascade_trend == "RISING" and current_price < ema_val:
                signal = "SHORT"
                description = f"Cascade short: risk={cascade_score:.0f}/100, trend={cascade_trend}"

        # ── Uptrend Pullback Strategy ─────────────────────────────
        # Active only in confirmed uptrend (EMA-50 > EMA-200, price > EMA-200).
        # Enters longs when price pulls back to within 1 ATR of EMA-50 and RSI is cooling.
        # Does NOT require a liquidation cluster — EMA-50 is the dynamic support.
        if signal == "HOLD" and regime == 'uptrend' and atr_val > 0 and getattr(config, 'USE_TREND_FOLLOW_STRATEGY', False):
            proximity = abs(current_price - ema_short_val)
            proximity_threshold = atr_val * getattr(config, 'TREND_FOLLOW_EMA_PROXIMITY_ATR', 1.0)
            rsi_min = getattr(config, 'TREND_FOLLOW_RSI_MIN', 38)
            rsi_max = getattr(config, 'TREND_FOLLOW_RSI_MAX', 55)

            if proximity <= proximity_threshold:
                self.log(f"📈 Uptrend pullback zone: ${current_price:.0f} within {proximity:.1f} of EMA50 ${ema_short_val:.0f} (ATR={atr_val:.1f})")
                can_tf_long = True
                if not (rsi_min <= rsi_val <= rsi_max):
                    self.log(f"   🚫 Trend-follow blocked: RSI {rsi_val:.1f} outside cooling window [{rsi_min}–{rsi_max}]")
                    can_tf_long = False
                if can_tf_long and funding_rate > config.MAX_FUNDING_RATE:
                    self.log(f"   🚫 Trend-follow blocked: High funding ({funding_pct:.4f}%)")
                    can_tf_long = False
                if can_tf_long and not trade_params["allow_long"]:
                    self.log(f"   🛡️ Trend-follow blocked: Cascade ({cascade_risk.value})")
                    can_tf_long = False
                # Fix: Require price at least 0.5% above EMA-200 before trend-follow fires.
                # Regime 'uptrend' requires price > EMA-200, but the EMA can lag during a fast
                # drop when hourly and daily trend context diverge.
                if can_tf_long and current_price < ema_val * 1.005:
                    self.log(f"   🚫 Trend-follow blocked: Price ${current_price:.2f} within 0.5% of EMA-200 ${ema_val:.2f} — crossover zone guard")
                    can_tf_long = False
                # P0+P1: Trend-follow is momentum — gate harder than flush.
                # Requires confirmed BULL Markov state (not just non-bear), tighter stay_prob,
                # and macro RISK_ON (not just non-RISK_OFF). Momentum fails on autopilot; only
                # run it when all three regime layers agree it's genuinely trending.
                if can_tf_long and markov_signal and getattr(config, 'USE_MARKOV_FILTER', False):
                    min_stay_tf = getattr(config, 'MARKOV_MIN_STAY_PROB_TREND_FOLLOW', 0.60)
                    if markov_signal['current_regime'] != 'BULL':
                        self.log(f"   📊 Markov blocked (trend-follow): Markov regime={markov_signal['current_regime']} ≠ BULL — momentum requires confirmed BULL state")
                        can_tf_long = False
                    elif markov_signal['size_scalar'] <= 0.0:
                        self.log(f"   📊 Markov blocked (trend-follow): bear_baseline={markov_signal['bear_baseline']:.3f} ≥ 0.60 — bear regime dominant")
                        can_tf_long = False
                    elif markov_signal['stay_prob'] < min_stay_tf:
                        self.log(f"   📊 Markov blocked (trend-follow): stay_prob={markov_signal['stay_prob']:.3f} < {min_stay_tf} — BULL unstable, skip momentum entry")
                        can_tf_long = False
                # P0: Trend-follow also requires macro RISK_ON — CAUTION or RISK_OFF regimes
                # have macro headwinds that break momentum; flush (mean-reversion) is unaffected.
                if can_tf_long and getattr(config, 'USE_MACRO_FILTER', False) and self.macro_snapshot:
                    macro_regime = self.macro_snapshot.get('regime', 'RISK_ON')
                    if macro_regime != 'RISK_ON':
                        self.log(f"   🌍 Trend-follow blocked: macro={macro_regime} (requires RISK_ON — momentum is macro-sensitive)")
                        can_tf_long = False
                # Volume filter intentionally skipped for trend-follow — EMA proximity
                # is the conviction signal; flush entries still require volume confirmation
                if can_tf_long:
                    signal = "LONG"
                    is_trend_follow_entry = True
                    self.active_regime = 'uptrend'
                    description = f"Trend-follow: EMA50 pullback @ ${ema_short_val:.0f} (gap={proximity:.1f}, ATR={atr_val:.1f})"
            else:
                self.log(f"📈 Uptrend active — no pullback yet: ${current_price:.0f} is ${proximity:.1f} from EMA50 ${ema_short_val:.0f} (need within ${proximity_threshold:.1f}, ATR={atr_val:.1f})")

        signal = self._enforce_long_only_signal(signal)

        current_pnl = 0.0
        # Recover entry price if missing but position exists
        pos_list = self.get_position_details()
        our_pos = next((p for p in pos_list if p['coin'] == self.symbol), None)
        position = our_pos['side'] if our_pos else 'none'
        entry_price = self.last_trade_price or (our_pos['entry_price'] if our_pos else 0.0)

        # DYNAMIC RISK MANAGEMENT (ATR)
        # Calculate dynamic stops based on current volatility
        # Standard: SL = 2 * ATR, TP = 3 * ATR (1.5R)
        current_sl_pct = config.STOP_LOSS_PCT  # Default fallback
        current_tp_pct = config.TAKE_PROFIT_PCT # Default fallback
        
        if atr_val > 0:
            # Convert ATR value to percentage of current price
            # e.g. ATR $20 on $2000 price = 1%
            # SL = 2 * 1% = 2%
            atr_pct = (atr_val / current_price) * 100
            current_sl_pct = round(atr_pct * config.ATR_MULTIPLIER, 2)
            current_tp_pct = round(current_sl_pct * 1.5, 2) # Target 1.5 Reward:Risk
            
            # Regime-aware clamps: trend-follow trades use tighter exits
            if self.active_regime == 'uptrend':
                tf_sl = getattr(config, 'TREND_FOLLOW_SL_PCT', 2.5)
                tf_tp = getattr(config, 'TREND_FOLLOW_TP_PCT', 6.0)
                current_sl_pct = max(0.5, min(current_sl_pct, tf_sl))
                current_tp_pct = max(0.75, min(current_tp_pct, tf_tp))
            else:
                current_sl_pct = max(0.5, min(current_sl_pct, 10.0))
                current_tp_pct = max(0.75, min(current_tp_pct, 15.0))
            
            # self.log(f"🛡️ Dynamic Risk: ATR=${atr_val:.2f} -> SL: {current_sl_pct}% | TP: {current_tp_pct}%")

        # An open position keeps the exact stop distance selected at entry.
        # Current ATR/regime changes must not diverge software exits from the
        # exchange-native stop already protecting that position.
        if position != 'none' and self.position_stop_loss_pct is not None:
            current_sl_pct = self.position_stop_loss_pct
            if self.position_take_profit_pct is not None:
                current_tp_pct = self.position_take_profit_pct

        # Check TP/SL if holding
        if position != 'none' and entry_price > 0:
            if position == 'long':
                pnl_pct = ((current_price / entry_price) - 1) * 100
            else:  # short
                pnl_pct = (1 - (current_price / entry_price)) * 100
            current_pnl = pnl_pct

            # ── Regime-change exit ──────────────────────────────────────────
            # If a position is now directionally opposed to the confirmed regime, exit
            # immediately rather than waiting for SL. Prevents holding a short through
            # an uptrend rally (as happened: SHORT held from $2,043 → $2,130 = -4.29%).
            if signal == 'HOLD':
                if position == 'short' and in_uptrend:
                    signal = "CLOSE"
                    description = (
                        f"Regime exit: SHORT closed — market confirmed UPTREND "
                        f"(EMA50 ${ema_short_val:.0f} > EMA200 ${ema_val:.0f}, "
                        f"price ${current_price:.0f} > EMA200). PnL: {pnl_pct:.2f}%"
                    )
                    self.log(f"   🔄 REGIME EXIT: Short closed — regime flipped to uptrend")
                elif position == 'long' and in_downtrend and current_price < ema_val:
                    signal = "CLOSE"
                    description = (
                        f"Regime exit: LONG closed — confirmed downtrend "
                        f"(EMA50 ${ema_short_val:.0f} < EMA200 ${ema_val:.0f}, "
                        f"price ${current_price:.0f} < EMA200). PnL: {pnl_pct:.2f}%"
                    )
                    self.log(f"   🔄 REGIME EXIT: Long closed — confirmed downtrend")

            # ── Cascade-elevated exit for short ──────────────────────────────
            # CASCADE=ELEVATED signals meme-token funding squeeze is building.
            # We block NEW shorts in ELEVATED, but existing shorts should also exit —
            # a funding squeeze propagates to ETH shorts as longs scramble to hedge.
            if signal == 'HOLD' and position == 'short' and cascade_risk == RiskLevel.ELEVATED:
                signal = "CLOSE"
                description = (
                    f"Cascade exit: SHORT closed — CASCADE {cascade_risk.value} "
                    f"(score={cascade_score:.0f}/100) — squeeze risk active. "
                    f"PnL: {pnl_pct:.2f}%"
                )
                self.log(f"   🛡️ CASCADE EXIT: Closing short — squeeze precursor detected")

            # RSI mean-reversion exit — fires before fixed TP (momentum exhaustion)
            if getattr(config, 'USE_RSI_EXIT', False) and signal == 'HOLD':
                rsi_exit_ob = getattr(config, 'RSI_EXIT_OVERBOUGHT', 69)
                rsi_exit_os = getattr(config, 'RSI_EXIT_OVERSOLD', 31)
                if position == 'long' and rsi_val >= rsi_exit_ob:
                    signal = "CLOSE"
                    description = f"RSI exit: RSI {rsi_val:.1f} ≥ {rsi_exit_ob} — overbought mean-reversion exit (+{pnl_pct:.2f}%)"
                elif position == 'short' and rsi_val <= rsi_exit_os:
                    signal = "CLOSE"
                    description = f"RSI exit: RSI {rsi_val:.1f} ≤ {rsi_exit_os} — oversold mean-reversion exit (+{pnl_pct:.2f}%)"

            if pnl_pct >= current_tp_pct:
                signal = "CLOSE"
                description = f"Take profit at +{pnl_pct:.2f}% (Target: {current_tp_pct}%)"
            elif pnl_pct <= -current_sl_pct:
                signal = "CLOSE"
                description = f"Stop loss at {pnl_pct:.2f}% (Stop: -{current_sl_pct}%)"
        result['signal'] = signal
        
        self.log(f"Signal: {signal}")
        self.log(f"{description}")
        
        # We already have our_pos and pos_list from earlier in the method
        
        # UPDATE UI STATE
        cascade_reasons = cascade_signal.reasons if cascade_signal else []
        bot_state.update(
            price=current_price,
            balance=usdc_balance,
            position=position,
            signal=signal,
            pnl=current_pnl,
            address=self.address,
            rsi=rsi_val,
            ema=ema_val,
            funding_rate=funding_pct,
            cascade_risk=cascade_risk.value if hasattr(cascade_risk, 'value') else str(cascade_risk),
            cascade_score=cascade_score,
            cascade_reasons=cascade_reasons,
            perps_positions=pos_list,
            spot_positions=balances.get('spot_balances', []),
            regime=regime,
        )

        # Entry quantity is computed below from fixed risk and notional caps.
        # Legacy TRADE_PERCENT/available-margin sizing is intentionally unused.
        trade_amount = 0.0

        if signal in ["LONG", "SHORT"]:
            # Live re-query to avoid stale position state from top of method
            current_pos = self.get_position()
            desired_side = 'long' if signal == 'LONG' else 'short'

            # ── PRE-ENTRY CHECKLIST (Phase 19 — structured denial logging) ──
            strategy_tag = "trend-follow" if self.active_regime == "uptrend" else "flush"

            # Gate 1: Balance fetch failed
            if balances.get('stale'):
                self._log_denial(strategy_tag, "BALANCE_FETCH_ERROR",
                    f"Could not confirm equity (last known: ${usdc_balance:.2f})",
                    {"last_known_equity": usdc_balance})
                result['action_taken'] = 'HOLD_BALANCE_FETCH_ERROR'
                return result

            # Gate 2: Minimum equity floor
            min_equity = getattr(config, 'MIN_EQUITY_FLOOR', 20.0)
            if usdc_balance < min_equity:
                self._log_denial(strategy_tag, "EQUITY_FLOOR",
                    f"Account ${usdc_balance:.2f} < floor ${min_equity:.2f}",
                    {"equity": usdc_balance, "floor": min_equity})
                result['action_taken'] = 'HOLD_EQUITY_FLOOR'
                return result

            # Gate 3: Re-entry cooldown
            reentry_cooldown = getattr(config, 'REENTRY_COOLDOWN_SECONDS', 600)
            cooldown_remaining = reentry_cooldown - (time.time() - self.last_close_time)
            if self.last_close_side == desired_side and cooldown_remaining > 0:
                self._log_denial(strategy_tag, "COOLDOWN",
                    f"{desired_side.upper()} re-entry blocked for {cooldown_remaining:.0f}s",
                    {"side": desired_side, "cooldown_remaining_s": round(cooldown_remaining)})
                result['action_taken'] = 'HOLD_COOLDOWN'
                return result

            # Gate 4: Staircase-down circuit breaker
            if desired_side == 'long':
                if time.time() < self.staircase_pause_until:
                    remaining_h = (self.staircase_pause_until - time.time()) / 3600
                    self._log_denial(strategy_tag, "STAIRCASE",
                        f"LONG blocked: staircase-down active, {remaining_h:.1f}h remaining",
                        {"remaining_hours": round(remaining_h, 1)})
                    result['action_taken'] = 'HOLD_STAIRCASE'
                    return result
                if self._check_staircase():
                    pause_hours = getattr(config, 'STAIRCASE_PAUSE_HOURS', 4)
                    self.staircase_pause_until = time.time() + pause_hours * 3600
                    self.long_close_history.clear()
                    self._log_denial(strategy_tag, "STAIRCASE",
                        f"{getattr(config, 'STAIRCASE_LOOKBACK', 3)} consecutive descending long losses — pausing {pause_hours}h",
                        {"lookback": getattr(config, 'STAIRCASE_LOOKBACK', 3), "pause_hours": pause_hours})
                    self.notify(f"⚠️ *STAIRCASE CIRCUIT BREAKER* — Consecutive losing longs on descent detected. LONGs paused for {pause_hours}h.")
                    result['action_taken'] = 'HOLD_STAIRCASE'
                    return result

            # Economic gate and fixed-risk sizing are required for every long entry.
            if desired_side == "long":
                stop_distance_pct = max(float(current_sl_pct), 0.0)
                policy_decision = self._evaluate_long_entry_policy(
                    current_price, stop_distance_pct, bool(current_price and closing_prices),
                    funding_cost_pct=funding_rate,
                )
                if policy_decision is None:
                    result['action_taken'] = 'HOLD_AFTER_COST_POLICY'
                    return result
                stop_price = current_price * (1 - stop_distance_pct / 100)
                entry_quantity = compute_position_size(
                    equity=usdc_balance,
                    entry_price=current_price,
                    stop_price=stop_price,
                    risk_fraction=getattr(config, "RISK_PER_TRADE_FRACTION", 0.005),
                    notional_cap_fraction=getattr(config, "NOTIONAL_CAP_FRACTION", 0.25),
                )
                trade_amount = entry_quantity * current_price
                if entry_quantity <= 0 or trade_amount < config.MIN_TRADE_USDC:
                    self._log_denial(strategy_tag, "FIXED_RISK_SIZING", "quantity below exchange minimum", {
                        "quantity": entry_quantity, "notional": trade_amount,
                    })
                    result['action_taken'] = 'HOLD_FIXED_RISK_SIZING'
                    return result

            # 2. Check if we already have the position we want
            if (signal == "LONG" and current_pos == "long") or (signal == "SHORT" and current_pos == "short"):
                self.log(f"   ⏸️  HOLD - Position already active ({current_pos.upper()})")
                result['action_taken'] = 'HOLD_POSITION_ACTIVE'
                return result

            # 3. Handle flipping position (reversal)
            if current_pos != 'none':
                if (signal == "LONG" and current_pos == 'short') or (signal == "SHORT" and current_pos == 'long'):
                    if self.close_position():
                        self.log("Closed opposite position")
                        # Long-only sizing remains fixed-risk; do not recalculate from margin.
                        balances = self.get_balances()
                    else:
                        result['action_taken'] = 'CLOSE_FAILED'
                        return result
            
            # 4. Open selected position
            if trade_amount > 0:
                # Fix: Cancel any stale ALO orders before opening. A failed chase can
                # leave a resting maker order on the book that fills in a later cycle,
                # stacking on top of the new entry.
                if not self._cancel_all_open_orders():
                    result['action_taken'] = 'HOLD_STALE_ORDERS'
                    self.log("🚫 Entry aborted: could not confirm all symbol orders absent", 'error')
                    return result
                side = 'long' if signal == "LONG" else 'short'
                stop_loss_pct = max(float(current_sl_pct), 0.0)
                stop_price = self._authoritative_stop_price(current_price, side, stop_loss_pct)
                if self.open_position(
                    side, trade_amount, current_price, equity=usdc_balance,
                    stop_price=stop_price, stop_loss_pct=stop_loss_pct,
                ):
                    result['action_taken'] = f'OPEN_{side.upper()}'
                    strategy_tag = "trend-follow" if self.active_regime == "uptrend" else "flush"
                    self._log_trade(f'OPEN_{side.upper()}', side, current_price, 0.0, trade_amount, strategy=strategy_tag)
                    self.position_open_time = time.time()
                    # Fix C: Track entry price for staircase detection
                    if side == 'long':
                        self.long_entry_price_tracked = current_price
                    emoji = "🟢" if side == "long" else "🔴"
                    self.notify(
                        f"{emoji} *OPENED {side.upper()}* — ETH ${current_price:.2f}\n"
                        f"Size: ${trade_amount:.2f} | Strategy: {strategy_tag}\n"
                        f"RSI: {rsi_val:.1f} | Regime: {regime}\n"
                        f"_{description}_"
                    )
                else:
                    result['action_taken'] = 'OPEN_FAILED'
            else:
                self.log(f"   ⏸️  HOLD - Insufficient available margin (${available_usdc:.2f})")
                result['action_taken'] = 'HOLD_NO_FUNDS'
                
        elif signal == "CLOSE":
            if self.close_position():
                # Fix 1: Record close time and direction for re-entry cooldown
                self.last_close_time = time.time()
                self.last_close_side = position  # position is 'long' or 'short'
                # Fix C: Record long close history for staircase detection
                if position == 'long':
                    self.long_close_history.append({'entry': self.long_entry_price_tracked, 'pnl': current_pnl})
                    self.long_entry_price_tracked = None
                self._reset_trailing_state()
                result['action_taken'] = 'CLOSE'
                is_tp = current_pnl >= 0
                emoji = "✅" if is_tp else "🛑"
                label = "TAKE PROFIT" if is_tp else "STOP LOSS"
                # Tag close reason for per-strategy analysis
                close_reason = "regime-exit" if "Regime exit" in description else (
                    "cascade-exit" if "Cascade exit" in description else (
                    "rsi-exit" if "RSI exit" in description else (
                    "tp" if is_tp else "sl")))
                self._log_trade('CLOSE_TP' if is_tp else 'CLOSE_SL', position, current_price, current_pnl, strategy=close_reason)
                self.notify(
                    f"{emoji} *{label}* — ETH ${current_price:.2f}\n"
                    f"PnL: `{current_pnl:+.2f}%` | Side: {position.upper()}\n"
                    f"_{description}_"
                )
            else:
                result['action_taken'] = 'CLOSE_FAILED'
        else:
            self.log(f"   ⏸️  HOLD")
            result['action_taken'] = 'HOLD'
            
        self.prev_price = current_price
        return result
    
    def _check_tp_sl(self) -> bool:
        """Fast TP/SL check for open positions. Returns True if a close was triggered."""
        position = self.get_position()
        if position == 'none':
            return False
        
        pos_list = self.get_position_details()
        our_pos = next((p for p in pos_list if p['coin'] == self.symbol), None)
        if not our_pos:
            return False
        
        entry_price = self.last_trade_price or our_pos.get('entry_price', 0)
        if entry_price <= 0:
            return False

        current_price = self.get_current_price()
        if not current_price:
            return False

        # ── Max time-in-trade (Phase 19 — agent guardrails) ─────────────
        max_hours = getattr(config, 'MAX_POSITION_HOURS', 168)
        if self.position_open_time and max_hours > 0:
            hours_open = (time.time() - self.position_open_time) / 3600
            if hours_open > max_hours:
                if position == 'long':
                    pnl_pct = ((current_price / entry_price) - 1) * 100
                else:
                    pnl_pct = (1 - (current_price / entry_price)) * 100
                self.log(f"⏰ MAX TIME: position open {hours_open:.1f}h > limit {max_hours}h — force closing")
                if self.close_position():
                    self.last_close_time = time.time()
                    self.last_close_side = position
                    if position == 'long':
                        self.long_close_history.append({'entry': self.long_entry_price_tracked, 'pnl': pnl_pct})
                        self.long_entry_price_tracked = None
                    self._log_trade('CLOSE_MAX_TIME', position, current_price, pnl_pct, strategy="max-time")
                    self._reset_trailing_state()
                    self.notify(
                        f"⏰ *MAX TIME CLOSE* — ETH ${current_price:.2f}\n"
                        f"PnL: `{pnl_pct:.2f}%` | {position.upper()} | {hours_open:.1f}h open"
                    )
                    return True
                return False

        # Calculate dynamic ATR-based stops. If an immutable per-position stop
        # exists, it is the authoritative cap even if active_regime later drifts.
        current_sl_pct = self.position_stop_loss_pct or config.STOP_LOSS_PCT
        current_tp_pct = self.position_take_profit_pct or config.TAKE_PROFIT_PCT
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (3600 * 1000 * 50)  # Fewer candles needed for ATR only
            candles = self.info.candles_snapshot(self.symbol, config.TIMEFRAME, start_time, end_time)
            if candles and len(candles) > config.ATR_PERIOD + 1:
                high_prices = [float(c['h']) for c in candles]
                low_prices = [float(c['l']) for c in candles]
                closing_prices = [float(c['c']) for c in candles]
                atr_val = calculate_atr(high_prices, low_prices, closing_prices, config.ATR_PERIOD)
                if atr_val > 0:
                    atr_pct = (atr_val / current_price) * 100
                    # The entry stop is immutable and authoritative whenever
                    # metadata exists. ATR may still inform the take-profit for
                    # legacy/recovered positions without frozen metadata.
                    if self.position_stop_loss_pct is None:
                        current_sl_pct = round(atr_pct * config.ATR_MULTIPLIER, 2)
                    current_tp_pct = round((current_sl_pct if self.position_stop_loss_pct is not None else atr_pct * config.ATR_MULTIPLIER) * 1.5, 2)
                    strategy = self.position_strategy or ("trend-follow" if self.active_regime == 'uptrend' else self.active_regime)
                    if strategy == 'trend-follow':
                        tf_sl = self.position_stop_loss_pct or getattr(config, 'TREND_FOLLOW_SL_PCT', 2.5)
                        tf_tp = self.position_take_profit_pct or getattr(config, 'TREND_FOLLOW_TP_PCT', 6.0)
                        if self.position_stop_loss_pct is None:
                            current_sl_pct = max(0.5, min(current_sl_pct, tf_sl))
                        current_tp_pct = max(0.75, min(current_tp_pct, tf_tp))
                    else:
                        if self.position_stop_loss_pct is None:
                            current_sl_pct = max(0.5, min(current_sl_pct, 10.0))
                        current_tp_pct = max(0.75, min(current_tp_pct, 15.0))
        except Exception:
            pass  # Fall back to config defaults
        
        if position == 'long':
            pnl_pct = ((current_price / entry_price) - 1) * 100
        else:
            pnl_pct = (1 - (current_price / entry_price)) * 100

        use_trail      = getattr(config, 'USE_TRAILING_STOP', False)
        trigger_pct    = getattr(config, 'TRAILING_TRIGGER_PCT', 8.0)
        floor_pct      = getattr(config, 'TRAILING_FLOOR_PCT', 4.0)

        # ── HARD STOP LOSS — always checked first, trailing or not ────────────
        if pnl_pct <= -current_sl_pct:
            self.log(f"⚡ FAST SL: {pnl_pct:.2f}% (stop: -{current_sl_pct}%) @ ${current_price:.2f}")
            if self.close_position():
                self.last_close_time = time.time()
                self.last_close_side = position
                if position == 'long':
                    self.long_close_history.append({'entry': self.long_entry_price_tracked, 'pnl': pnl_pct})
                    self.long_entry_price_tracked = None
                sl_strategy = self.position_strategy or ("trend-follow" if self.active_regime == "uptrend" else self.active_regime)
                self._log_trade('CLOSE_SL', position, current_price, pnl_pct, strategy=sl_strategy)
                self._reset_trailing_state()
                self.notify(
                    f"🛑 *STOP LOSS* — ETH ${current_price:.2f}\n"
                    f"PnL: `{pnl_pct:.2f}%` (stop -{current_sl_pct}%) | {position.upper()}"
                )
            return True

        # ── TRAILING STOP (replaces fixed TP when USE_TRAILING_STOP=True) ────
        if use_trail:
            # Sign-aware floor price (long: floor below; short: ceiling above)
            if position == 'long':
                new_floor = current_price * (1 - floor_pct / 100)
                floor_hit = self.trailing_floor is not None and current_price <= self.trailing_floor
                floor_moves_better = self.trailing_floor is None or new_floor > self.trailing_floor
            else:
                new_floor = current_price * (1 + floor_pct / 100)
                floor_hit = self.trailing_floor is not None and current_price >= self.trailing_floor
                floor_moves_better = self.trailing_floor is None or new_floor < self.trailing_floor

            # Activate trailing once trigger is reached
            if not self.trailing_activated and pnl_pct >= trigger_pct:
                self.trailing_floor = new_floor
                self.trailing_activated = True
                self.log(
                    f"🎯 TRAILING STOP ACTIVATED — floor ${self.trailing_floor:.2f} "
                    f"(+{pnl_pct:.2f}% | trigger {trigger_pct}%)"
                )

            # Ratchet floor in profitable direction
            if self.trailing_activated and floor_moves_better:
                old = self.trailing_floor
                self.trailing_floor = new_floor
                if abs(new_floor - old) > 1:  # Only log meaningful moves
                    self.log(f"⬆️  Trailing floor: ${old:.2f} → ${self.trailing_floor:.2f}")

            # Close on floor breach
            if floor_hit:
                self.log(
                    f"🎯 TRAILING STOP HIT — ${current_price:.2f} crossed floor "
                    f"${self.trailing_floor:.2f} | PnL {pnl_pct:+.2f}%"
                )
                if self.close_position():
                    self.last_close_time = time.time()
                    self.last_close_side = position
                    if position == 'long':
                        self.long_close_history.append({'entry': self.long_entry_price_tracked, 'pnl': pnl_pct})
                        self.long_entry_price_tracked = None
                    trail_strategy = self.position_strategy or ("trend-follow" if self.active_regime == "uptrend" else self.active_regime)
                    self._log_trade('CLOSE_TRAIL', position, current_price, pnl_pct, strategy=trail_strategy)
                    triggered_floor = self.trailing_floor  # capture before reset clears it
                    self._reset_trailing_state()
                    self.notify(
                        f"🎯 *TRAILING STOP* — ETH ${current_price:.2f}\n"
                        f"PnL: `{pnl_pct:+.2f}%` | Floor: ${triggered_floor:.2f} | {position.upper()}"
                    )
                return True

            # Trailing active but floor not hit — skip fixed TP, let it run
            if self.trailing_activated:
                return False

        # ── FIXED TAKE PROFIT — only when trailing stop is not active ─────────
        if pnl_pct >= current_tp_pct:
            self.log(f"⚡ FAST TP: +{pnl_pct:.2f}% (target: {current_tp_pct}%) @ ${current_price:.2f}")
            if self.close_position():
                self.last_close_time = time.time()
                self.last_close_side = position
                if position == 'long':
                    self.long_close_history.append({'entry': self.long_entry_price_tracked, 'pnl': pnl_pct})
                    self.long_entry_price_tracked = None
                fixed_tp_strategy = self.position_strategy or ("trend-follow" if self.active_regime == "uptrend" else self.active_regime)
                self._log_trade('CLOSE_TP', position, current_price, pnl_pct, strategy=fixed_tp_strategy)
                self._reset_trailing_state()
                self.notify(
                    f"✅ *TAKE PROFIT* — ETH ${current_price:.2f}\n"
                    f"PnL: `+{pnl_pct:.2f}%` (target {current_tp_pct}%) | {position.upper()}"
                )
            return True

        return False

    def _update_macro_regime(self):
        """Refresh macro regime from FRED if cache is stale (>MACRO_CHECK_INTERVAL_HOURS old).
        Updates self.macro_snapshot. No-op if FRED_API_KEY not configured.
        """
        if not getattr(config, 'USE_MACRO_FILTER', False):
            return
        fred_key = getattr(config, 'FRED_API_KEY', '')
        if not fred_key:
            return
        interval_s = getattr(config, 'MACRO_CHECK_INTERVAL_HOURS', 6) * 3600
        if time.time() - self.last_macro_check < interval_s:
            return  # Cache still fresh
        try:
            self.macro_snapshot = get_macro_regime(fred_key)
            self.last_macro_check = time.time()
            self.log(f"🌍 Macro regime: {format_regime_summary(self.macro_snapshot)}")
        except Exception as e:
            self.logger.warning(f"Macro regime update failed: {e}")

    def _update_vpin(self):
        """Fetch recent trades from HL and update the VPIN estimate.
        Side 'A' = aggressor bought (taker buy) → buy volume.
        Side 'B' = aggressor sold (taker sell) → sell volume.
        """
        if not getattr(config, 'USE_VPIN_FILTER', False):
            return
        try:
            resp = requests.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "recentTrades", "coin": self.symbol},
                timeout=5
            )
            trades = resp.json()
            for t in trades:
                side = t.get('side', '')
                sz = float(t.get('sz', 0))
                if side == 'A':
                    self.trade_buffer.append(('buy', sz))
                elif side == 'B':
                    self.trade_buffer.append(('sell', sz))
            self.current_vpin = self._compute_vpin()
            if self.current_vpin is not None:
                self.logger.debug(f"📊 VPIN: {self.current_vpin:.3f} (buffer={len(self.trade_buffer)} trades)")
        except Exception as e:
            self.logger.debug(f"VPIN update failed: {e}")

    def _compute_vpin(self) -> Optional[float]:
        """Compute VPIN from trade buffer using equal-volume bucket method."""
        bucket_size = getattr(config, 'VPIN_BUCKET_SIZE', 50)
        buf = list(self.trade_buffer)
        if len(buf) < bucket_size:
            return None
        n_buckets = len(buf) // bucket_size
        vpin_vals = []
        for i in range(n_buckets):
            chunk = buf[i * bucket_size:(i + 1) * bucket_size]
            v_buy = sum(sz for side, sz in chunk if side == 'buy')
            v_sell = sum(sz for side, sz in chunk if side == 'sell')
            total = v_buy + v_sell
            if total > 0:
                vpin_vals.append(abs(v_buy - v_sell) / total)
        return sum(vpin_vals) / len(vpin_vals) if vpin_vals else None

    def _check_staircase(self) -> bool:
        """Return True if the last N long closes form a staircase-down loss pattern.
        Pattern: all entry prices descending AND all PnLs negative.
        Indicates bot is averaging into a falling market — pause longs.
        """
        lookback = getattr(config, 'STAIRCASE_LOOKBACK', 3)
        if len(self.long_close_history) < lookback:
            return False
        entries = [h['entry'] for h in self.long_close_history if h['entry'] is not None]
        pnls = [h['pnl'] for h in self.long_close_history]
        if len(entries) < lookback:
            return False
        all_descending = all(entries[i] > entries[i + 1] for i in range(len(entries) - 1))
        all_losses = all(p < 0 for p in pnls)
        return all_descending and all_losses

    def trade_loop(self):
        """Main trading loop running in a separate thread."""
        self.log("Trading loop started")
        
        # Initial checks
        try:
            balances = self.get_balances()
            price = self.get_current_price() or 0
            position = self.get_position()
            self.log(f"Balances: {balances}")
            self.log(f"Current Position: {position.upper()}")
        except Exception as e:
            self.log(f"Initialization error: {e}", 'error')

        while True:
            try:
                self.analyze_and_trade()
                
                # Fix 3: Fast TP/SL inner loop — check stops every 30s
                # between full signal evaluations
                elapsed = 0
                while elapsed < config.CHECK_INTERVAL_SECONDS:
                    time.sleep(config.TP_SL_CHECK_INTERVAL)
                    elapsed += config.TP_SL_CHECK_INTERVAL
                    if self._check_tp_sl():
                        break  # Position closed, run full analysis immediately
                
            except Exception as e:
                self.log(f"Unexpected error in trade loop: {e}", 'error')
                time.sleep(10)

    def _reset_trailing_state(self):
        """Reset trailing stop state. Call whenever a position closes."""
        self.trailing_activated = False
        self.trailing_floor = None
        self.ladder_1_executed = False
        self.ladder_2_executed = False
        self.position_open_time = None
        self.position_strategy = None
        self.position_stop_loss_pct = None
        self.position_take_profit_pct = None

    def _recover_open_position_log(self):
        """On startup: detect on-exchange position without a logged OPEN entry.
        If found, write a recovery OPEN entry so the audit trail is complete.
        This catches the race-condition where the machine restarted after the
        exchange accepted the order but before _log_trade completed.
        """
        try:
            pos_list = self.get_position_details()
            our_pos = next((p for p in pos_list if p['coin'] == self.symbol), None)
            if not our_pos:
                return
            side = our_pos['side']  # 'long' or 'short'
            entry_price = our_pos.get('entry_price', 0)
            size = our_pos.get('size', 0)
            if not entry_price or not size:
                return
            # Check if last line of trade log is an unmatched OPEN
            try:
                with open(self.TRADE_LOG_PATH, 'r') as f:
                    lines = [l.strip() for l in f.readlines() if l.strip()]
            except FileNotFoundError:
                lines = []
            last_line = lines[-1] if lines else ""
            last_event = last_line.split(",")[1] if last_line and len(last_line.split(",")) > 1 else ""
            if last_event.startswith("OPEN_"):
                # Position is logged — recover open time from the OPEN timestamp
                try:
                    open_ts = last_line.split(",")[0]
                    from datetime import timezone as _tz
                    open_dt = datetime.strptime(open_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
                    self.position_open_time = open_dt.timestamp()
                    hours_ago = (time.time() - self.position_open_time) / 3600
                    self.log(f"⏱️ Recovered position age: opened {hours_ago:.1f}h ago")
                except Exception:
                    self.position_open_time = time.time()
                return
            # Log a recovery OPEN entry
            amount_usd = abs(size) * entry_price
            event = f"OPEN_{side.upper()}"
            self.log(f"⚠️ Recovery: found unlogged {event} @ ${entry_price:.2f} — writing recovery entry")
            self._log_trade(event, side, entry_price, 0.0, amount_usd, strategy="recovery")
            self.last_trade_price = entry_price
            self.position_open_time = time.time()
        except Exception as e:
            self.log(f"⚠️ Position recovery check failed: {e}", 'error')

    def run_startup_validation(self) -> bool:
        """
        Pre-flight check before trading starts.
        Logs current regime, equity, macro state, and active strategies.
        Returns True if safe to proceed. Called once before trade thread launches.
        """
        self.log("=" * 70)
        self.log("  STARTUP VALIDATION — PRE-FLIGHT CHECK")
        self.log("=" * 70)

        ok = True

        # 1. Mode / network
        self.log(f"  Mode:          {'DRY RUN (paper)' if config.DRY_RUN else '🔴 LIVE — REAL MONEY'}")
        self.log(f"  Network:       {'TESTNET' if config.IS_TESTNET else 'MAINNET'}")

        # 2. Equity vs floor
        try:
            balances = self.get_balances()
            usdc = balances.get('USDC', 0.0)
            floor = getattr(config, 'MIN_EQUITY_FLOOR', 20.0)
            equity_ok = usdc >= floor
            status = "✅" if equity_ok else "❌ BELOW FLOOR — entries blocked"
            self.log(f"  Equity:        ${usdc:.2f} (floor ${floor:.2f}) {status}")
            if not equity_ok:
                ok = False
        except Exception as e:
            self.log(f"  Equity:        FETCH FAILED — {e}")

        # 3. Macro regime (FRED)
        try:
            fred_key = getattr(config, 'FRED_API_KEY', '')
            if fred_key:
                snapshot = get_macro_regime(fred_key)
                self.log(f"  Macro:         {format_regime_summary(snapshot)}")
                for sig in snapshot.get("signals", []):
                    self.log(f"                 → {sig}")
            else:
                self.log("  Macro:         ⚠️  FRED_API_KEY not set — macro filter inactive")
        except Exception as e:
            self.log(f"  Macro:         FETCH FAILED — {e}")

        # 4. Price + EMA + RSI regime (mirrors analyze_and_trade candle fetch)
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (3600 * 1000 * 300)
            candles = self.info.candles_snapshot(self.symbol, config.TIMEFRAME, start_time, end_time)
            if candles and len(candles) >= config.EMA_PERIOD:
                closes = [float(c['c']) for c in candles]
                price  = closes[-1]
                ema50  = calculate_ema(closes, config.EMA_SHORT_PERIOD)
                ema200 = calculate_ema(closes, config.EMA_PERIOD)
                rsi    = calculate_rsi(closes, config.RSI_PERIOD)
                buf    = ema50 * 1.005
                if ema50 > ema200 and price > buf:
                    price_regime = "UPTREND   (trend-follow eligible)"
                elif ema50 < ema200:
                    price_regime = "DOWNTREND (flush-LONG only, no trend-follow)"
                else:
                    price_regime = "RANGE     (range-bear thresholds active)"
                self.log(f"  ETH Price:     ${price:,.2f}")
                self.log(f"  EMA-50:        ${ema50:,.2f}")
                self.log(f"  EMA-200:       ${ema200:,.2f}")
                self.log(f"  RSI({config.RSI_PERIOD}):         {rsi:.1f}")
                self.log(f"  Price Regime:  {price_regime}")
            else:
                self.log("  Price/EMA:     ⚠️  insufficient candles — check timeframe and connection")
        except Exception as e:
            self.log(f"  Price/EMA:     FETCH FAILED — {e}")

        # 5. Active strategies
        self.log("  Strategies:")
        self.log(f"    Flush-LONG:    ON (always)")
        self.log(f"    Flush-SHORT:   {'ON' if getattr(config, 'USE_FLUSH_SHORT', False) else 'OFF'}")
        self.log(f"    Trend-Follow:  {'ON' if getattr(config, 'USE_TREND_FOLLOW_STRATEGY', True) else 'OFF'}")
        self.log(f"    Trailing Stop: {'ON' if getattr(config, 'USE_TRAILING_STOP', True) else 'OFF'}")
        self.log(f"    Macro Filter:  {'ON' if getattr(config, 'USE_MACRO_FILTER', True) else 'OFF'}")
        self.log(f"    VPIN Filter:   {'ON' if getattr(config, 'USE_VPIN_FILTER', True) else 'OFF'}")

        # 6. Risk parameter snapshot
        self.log("  Risk:")
        self.log(f"    Risk/Notional: {getattr(config, 'RISK_PER_TRADE_FRACTION', 0.005):.2%} / {getattr(config, 'NOTIONAL_CAP_FRACTION', 0.25):.0%}")
        self.log(f"    Hard SL:       {getattr(config, 'STOP_LOSS_PCT', 4.0)}%")
        self.log(f"    Trailing:      trigger +{getattr(config, 'TRAILING_TRIGGER_PCT', 3.0)}% / floor -{getattr(config, 'TRAILING_FLOOR_PCT', 1.5)}%")
        self.log(f"    Equity Floor:  ${getattr(config, 'MIN_EQUITY_FLOOR', 20.0)}")
        self.log(f"    Max Position:  {getattr(config, 'MAX_POSITION_HOURS', 168)}h")

        self.log("=" * 70)
        verdict = "✅ PRE-FLIGHT PASSED — starting trade loop" if ok else "⚠️  PRE-FLIGHT ISSUES — check above before proceeding"
        self.log(f"  {verdict}")
        self.log("=" * 70)
        time.sleep(3)  # Pause so operator can read the validation in logs before first trade
        return ok

    def daily_review_loop(self):
        """
        Runs weekly_review_agent.py once per day at 08:00 UTC as a subprocess.
        Isolated process — a review crash cannot affect the trading bot.
        Only active if ANTHROPIC_API_KEY is present in the environment.
        """
        import subprocess, os
        from datetime import datetime, timezone, timedelta

        if not getattr(config, "DAILY_REVIEW_ENABLED", False):
            self.log("Daily review: disabled (set DAILY_REVIEW_ENABLED=true to opt in)")
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.log("Daily review: API credential not set — review thread inactive")
            return

        self.log("Daily review thread started (fires at 08:00 UTC daily)")

        while True:
            now = datetime.now(tz=timezone.utc)
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            sleep_secs = (next_run - now).total_seconds()
            self.log(f"Daily review: next run in {sleep_secs/3600:.1f}h at {next_run.strftime('%Y-%m-%d %H:%M UTC')}")
            time.sleep(sleep_secs)

            self.log("Daily review: starting 4-agent review...")
            try:
                result = subprocess.run(
                    ["python3", "weekly_review_agent.py"],
                    capture_output=True, text=True, timeout=300,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                )
                if result.returncode == 0:
                    self.log("Daily review: completed OK")
                else:
                    self.log(f"Daily review: exit {result.returncode} | {result.stderr[-300:]}", "error")
            except subprocess.TimeoutExpired:
                self.log("Daily review: timed out after 5 min", "error")
            except Exception as e:
                self.log(f"Daily review: failed — {e}", "error")

    def run(self):
        self.log("=" * 60)
        self.log("ETH LEVERAGE FLUSH TRADING BOT - HYPERLIQUID MODE")
        self.log("=" * 60)
        self.log(f"Symbol: {self.symbol}")
        self.log(f"Leverage: {self.leverage}x")
        self.log(f"Mode: {'DRY RUN' if config.DRY_RUN else '🔴 LIVE TRADING'} | {'TESTNET' if config.IS_TESTNET else 'MAINNET'}")

        # Recover any unlogged OPEN positions from a previous crash/restart
        self._recover_open_position_log()

        # Pre-flight: validate regime, equity, and strategy state before trading
        if not self.run_startup_validation():
            self.log("Startup validation failed — trading/API threads not started", "error")
            return

        # Start Trading Loop in Background Thread
        trade_thread = threading.Thread(target=self.trade_loop, daemon=True)
        trade_thread.start()
        self.log("Trading thread started")

        # Start WebSocket Loop in Background Thread
        ws_thread = threading.Thread(target=self.start_websocket_loop, daemon=True)
        ws_thread.start()
        self.log("WebSocket thread started")

        # Start Daily Review Thread (cloud-side self-improvement, no Mac required)
        review_thread = threading.Thread(target=self.daily_review_loop, daemon=True)
        review_thread.start()
        self.log("Daily review thread started")

        # Start API Server in Main Thread (Blocking)
        self.log(f"Starting UI Server on {config.API_HOST}:{config.API_PORT}...")
        try:
            uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="info")
        except Exception as e:
            self.logger.error(f"Failed to start web server: {e}")

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Hyperliquid bot and synthetic dashboard")
    parser.add_argument("--live", action="store_true", help="enable live orders only with HL_ENABLE_LIVE_TRADING=true")
    args = parser.parse_args(argv)

    # Environment opt-in alone is insufficient. Every process starts paper-only
    # until this invocation explicitly supplies --live.
    config.HL_LIVE_RUNTIME_OPT_IN = False
    config.DRY_RUN = True
    if args.live:
        if not getattr(config, "LIVE_TRADING_OPT_IN", False):
            parser.error("--live requires HL_ENABLE_LIVE_TRADING=true")
        if getattr(config, "DEMO_MODE", True):
            parser.error("--live requires DEMO_MODE=false")
        config.HL_LIVE_RUNTIME_OPT_IN = True
        config.DRY_RUN = False

    if config.DEMO_MODE:
        logging.basicConfig(level=logging.INFO)
        logging.info("Starting credential-free synthetic demo dashboard")
        uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="info")
    else:
        bot = HyperliquidFlushBot()
        bot_state._denial_ref = bot.denial_log
        bot.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        logging.critical(f"Fatal startup error: {e}")
        raise
