from security_manager import SecurityManager
import os


def env_bool(name: str, default: bool = False) -> bool:
    """Parse an opt-in boolean. Only an explicit true value enables it."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# =============================================================================
# HYPERLIQUID SETTINGS
# =============================================================================
HYPERLIQUID_PRIVATE_KEY = SecurityManager.get_key("HYPERLIQUID_PRIVATE_KEY")
HYPERLIQUID_ADDRESS = SecurityManager.get_key("HYPERLIQUID_ADDRESS")
HL_NETWORK = os.getenv("HL_NETWORK", "testnet").strip().lower()
HL_NETWORK_VALID = HL_NETWORK in {"testnet", "mainnet"}
IS_TESTNET = HL_NETWORK != "mainnet"  # invalid values remain on the safe testnet path

# =============================================================================
# DEX / UNISWAP SETTINGS
# =============================================================================
NETWORK = os.getenv("DEX_NETWORK", "arbitrum").strip().lower()
DEX_NETWORK_VALID = NETWORK in {"arbitrum"}
WALLET_PRIVATE_KEY = SecurityManager.get_key("WALLET_PRIVATE_KEY")

RPC_URLS = {
    "arbitrum": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
    "optimism": os.getenv("OPTIMISM_RPC_URL", "https://mainnet.optimism.io"),
    "polygon": os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com"),
    "base": os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
}

TOKENS = {
    "arbitrum": {
        "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
    }
}

QUOTE_TOKEN = "USDC"

UNISWAP_ROUTER = {
    "arbitrum": "0xE592427A0AEce92De3Edee1F18E0157C05861564"
}

UNISWAP_QUOTER = {
    "arbitrum": "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"
}

CHAIN_IDS = {
    "arbitrum": 42161
}

POOL_FEE = 3000        # 0.3%
SLIPPAGE_PERCENT = 1.0 # 1% slippage tolerance

# =============================================================================
# TRADING SETTINGS
# =============================================================================
LEVERAGE = 1           # Start with 1× (no leverage) to mimic spot behavior
SYMBOL = "ETH"         # Hyperliquid symbol
MIN_TRADE_USDC = 10    # Minimum amount to attempt a trade
MIN_TRADE_ETH = 0.01   # Minimum ETH for DEX trades
TRADE_PERCENT = 25     # Use most of the available balance per trade
MIN_EQUITY_FLOOR = 20.0  # Block new positions when account equity is below the safety floor.

# RSI Settings (Calculated from candles)
TIMEFRAME = "1h"
RSI_PERIOD = 8           # Responsive research setting for hourly bars.
RSI_OVERSOLD = 40        # Relaxed from 30 based on backtest
RSI_OVERBOUGHT = 65      # Raised from 60 (Mar 5): 60 is barely above neutral; 65 requires genuine overbought conviction

# RSI Mean-Reversion Exit (research setting; validate before changing)
# Exit open positions when RSI signals exhaustion — fires before fixed TP
USE_RSI_EXIT = True
RSI_EXIT_OVERBOUGHT = 78  # Research setting; validate on held-out data.
RSI_EXIT_OVERSOLD = 28    # Research setting; validate on held-out data.

# Trend & RSI Filters
USE_TREND_FILTER = True   # Regime-conditional: only blocks longs when EMA-50 < EMA-200 (confirmed downtrend)
USE_RSI_FILTER = True    # Only LONG if RSI < 40, SHORT if RSI > 60
EMA_PERIOD = 200         # Standard long-term trend indicator
EMA_SHORT_PERIOD = 50    # Short-term EMA for downtrend regime detection
ATR_PERIOD = 14          # Volatility measurement period
ATR_MULTIPLIER = 4.0     # Wider than 2x to reduce noise-triggered exits; validate prospectively.

# Funding Rate Filter
MAX_FUNDING_RATE = 0.00015          # Block LONGs if funding > 0.015% (crowded longs paying high)
NEGATIVE_FUNDING_SHORT_BLOCK = 0.002 # Block SHORTs if funding < -0.2% — at this level shorts are
                                     # overcrowded and paying longs; squeeze risk is high
NEGATIVE_FUNDING_LOG_THRESHOLD = -0.0001 # Log LONG-favorable context if funding < -0.01%

# Volume Confirmation Filter
USE_VOLUME_FILTER = True          # Require above-average volume for entry signals
VOLUME_CONFIRMATION_MULT = 1.2   # Relaxed from 1.5 — in-progress 1h candles rarely hit 1.5x full-candle avg

# RSI Downtrend Override
RSI_OVERSOLD_DOWNTREND = 35  # Relaxed from 30 — RSI<30 is genuinely rare; 35 still filters noise in downtrend

# RSI Range-Bear Overrides (Mar 20, 2026)
# When price < EMA200 in range regime (EMA50 ≥ EMA200), the standard long/short filters
# produce a long-only bias: longs trigger on any RSI dip to 40, shorts never trigger (RSI
# rarely reaches 65 in a gradual decline). Both thresholds are adjusted for this condition.
RSI_OVERSOLD_RANGE_BEAR = 32   # Longs: tighter than RSI_OVERSOLD (40) — below EMA200 needs real oversold conviction
RSI_OVERBOUGHT_RANGE_BEAR = 55 # Shorts: lower than RSI_OVERBOUGHT (65) — RSI rarely hits 65 in a gradual decline
RSI_OVERBOUGHT_DOWNTREND = 62  # Require a stronger overbought reading in downtrends.

# RSI extreme momentum guard — block shorts when RSI ≥ this value (squeeze risk)
RSI_EXTREME_OVERBOUGHT = 78  # At RSI≥78 price is still in strong momentum, not ready to reverse
LOG_RSI_DIVERGENCE = True    # Log bullish divergence at cluster approach

# Open Interest
USE_OI_FILTER = True         # Active (Mar 9): block SHORTs when OI is rising (continuation, not reversal)

# =============================================================================
# FLUSH-SHORT STRATEGY
# =============================================================================
# Disabled in the public baseline. Re-enable only after an independent, frozen,
# after-cost out-of-sample and prospective validation gate passes.
USE_FLUSH_SHORT = False
# Recovery baseline is explicitly long-only. This guards independent short paths,
# including cascade-riding logic, instead of relying on one strategy flag.
LONG_ONLY_MODE = True

# =============================================================================
# TREND-FOLLOW STRATEGY (uptrend regime only)
# =============================================================================
USE_TREND_FOLLOW_STRATEGY = False    # Enable EMA-50 pullback entries in confirmed uptrends
TREND_FOLLOW_RSI_MIN = 38           # RSI must be cooling (not oversold, not still overbought)
TREND_FOLLOW_RSI_MAX = 48           # Require a clear momentum cooldown before entry.
TREND_FOLLOW_EMA_PROXIMITY_ATR = 0.7  # Avoid entries too far from EMA-50.
TREND_FOLLOW_SIZE_PCT = 60          # Smaller size — lower-conviction than cluster flush
TREND_FOLLOW_TP_PCT = 6.0           # Tighter TP — trend-follow expects smaller moves
TREND_FOLLOW_SL_PCT = 2.5           # Tighter SL — cut quickly if pullback fails

# =============================================================================
# RISK MANAGEMENT
# =============================================================================
TAKE_PROFIT_PCT = 12   # Fixed TP % — bypassed when USE_TRAILING_STOP is True
STOP_LOSS_PCT = 4      # Hard SL — always active as emergency backstop
TP_SL_CHECK_INTERVAL = 30  # Seconds between TP/SL checks when position is open (fast loop)
REENTRY_COOLDOWN_SECONDS = 600  # 10-min block on same-direction re-entry after any TP/SL close
MAX_POSITION_HOURS = 168        # Force-close positions open longer than this (7 days). Phase 13: one loser ran 7 days to -4.29% SL.

# =============================================================================
# TRAILING STOP STRATEGY
# Replaces fixed TP with a floor that rises as price moves in our favour.
# Hard SL (STOP_LOSS_PCT) remains active as emergency backstop at all times.
# =============================================================================
USE_TRAILING_STOP = True
TRAILING_TRIGGER_PCT = 3.0   # Activate trailing once position is up this % (lowered from 8.0 — 8% was unreachable given RSI exits at 1-2%)
TRAILING_FLOOR_PCT   = 1.5   # Trailing floor sits this % below current price (lowered from 4.0 — locks in ~1.5% min profit once triggered)
                              # Example: price $2000, trigger at +3% = $2060, floor = $2060 * (1 - 0.015) = $2029 (locks ~1.45%)

# Ladder Buys — dollar-cost averaging into winning direction on dips
# DISABLED by default: significantly increases exposure on leveraged perpetual positions.
# Enable only after trailing stop is validated over 2+ weeks.
USE_LADDER_BUYS       = False
LADDER_BUY_1_DROP_PCT = 15.0  # If position is down this % from entry, add size
LADDER_BUY_1_SIZE_PCT = 30    # Add this % of original position size
LADDER_BUY_2_DROP_PCT = 25.0  # Second ladder buy threshold
LADDER_BUY_2_SIZE_PCT = 50    # Add this % of original position size

# =============================================================================
# MACRO REGIME FILTER (FRED-based — Mar 20, 2026)
# =============================================================================
# Free FRED API key: https://fred.stlouisfed.org/docs/api/api_key.html
# Set on Fly.io: fly secrets set FRED_API_KEY=<key>
FRED_API_KEY = SecurityManager.get_key("FRED_API_KEY") or os.getenv("FRED_API_KEY", "")

USE_MACRO_FILTER = True             # Enable FRED macro regime pre-filter
MACRO_CHECK_INTERVAL_HOURS = 6     # Re-fetch from FRED every 6h (data is daily, no need for more)
MACRO_RISK_OFF_BLOCK_LONG = True   # Block new LONG entries in RISK_OFF
MACRO_CAUTION_SIZE_PCT = 50        # Reduce LONG size to 50% in CAUTION

# Staircase-Down Circuit Breaker (Fix C — Mar 20, 2026)
# If the last N long closes were: (a) all descending entry prices AND (b) all losses,
# the bot is averaging into a falling market. Pause longs for STAIRCASE_PAUSE_HOURS.
STAIRCASE_LOOKBACK = 3      # Number of consecutive losing longs before pause triggers
STAIRCASE_PAUSE_HOURS = 4   # Hours to block new long entries after staircase detected

# VPIN — Volume-Synchronized Probability of Informed Trading (Mar 20, 2026)
# Detects informed one-sided order flow. High VPIN = don't fade the move.
USE_VPIN_FILTER = True
VPIN_DANGER_THRESHOLD = 0.65    # Skip flush longs when VPIN > 0.65 (informed flow active)
VPIN_EXTREME_THRESHOLD = 0.80   # Block ALL entries when VPIN > 0.80 (extreme one-sided)
VPIN_BUCKET_SIZE = 50           # Trades per VPIN bucket
VPIN_WINDOW = 500               # Rolling window size in trades

# =============================================================================
# MARKOV REGIME FILTER
# 3-state Markov chain (BULL/SIDEWAYS/BEAR) on 1h closes.
# stay_prob = P(current regime persists next candle) — low stay = unstable, skip entry.
# size_scalar = linear function of stationary P(BEAR): 1.0 at ≤0.20, 0.0 at ≥0.60.
# =============================================================================
USE_MARKOV_FILTER = True
MARKOV_WINDOW = 20          # Rolling-return window (candles) for state labeling
MARKOV_THRESHOLD = 0.02     # ±2% return threshold for BULL/BEAR state label
MARKOV_MIN_STAY_PROB = 0.55              # Min stay_prob for flush (mean-reversion) entries
MARKOV_MIN_STAY_PROB_TREND_FOLLOW = 0.60 # Tighter min stay_prob for trend-follow (momentum) entries

def get_min_gas_reserve():
    """Returns minimum native ETH required for gas based on network."""
    reserves = {
        "arbitrum": 0.005,
        "optimism": 0.005,
        "polygon": 0.5,
        "base": 0.005,
        "ethereum": 0.02
    }
    return reserves.get(NETWORK, 0.01)

# =============================================================================
# TELEGRAM NOTIFICATIONS
# =============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_NOTIFY = env_bool("TELEGRAM_NOTIFY", False)

# =============================================================================
# BOT / SERVICE SETTINGS
# =============================================================================
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
# A clean clone starts a synthetic, credential-free dashboard. Set DEMO_MODE=false
# to run the read-only/paper market collector. Neither mode can submit orders.
DEMO_MODE = env_bool("DEMO_MODE", True)
LIVE_TRADING_OPT_IN = env_bool("HL_ENABLE_LIVE_TRADING", False)
DEX_LIVE_TRADING_OPT_IN = env_bool("DEX_ENABLE_LIVE_TRADING", False)
# Set only by explicit CLI/runtime entry points; environment variables cannot set
# either value. Order seams require both the environment and runtime opt-ins.
HL_LIVE_RUNTIME_OPT_IN = False
DEX_LIVE_RUNTIME_OPT_IN = False
GUARDIAN_LIVE_RUNTIME_OPT_IN = False
DRY_RUN = not LIVE_TRADING_OPT_IN
PAUSE_NEW_ENTRIES = env_bool("PAUSE_NEW_ENTRIES", True)
GUARDIAN_ENABLE_LIVE_ACTIONS = env_bool("GUARDIAN_ENABLE_LIVE_ACTIONS", False)
DAILY_REVIEW_ENABLED = env_bool("DAILY_REVIEW_ENABLED", False)
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8080"))
DEPLOYMENT_ENV = os.getenv("DEPLOYMENT_ENV", "local")
DATA_DIR = os.path.abspath(os.path.expanduser(os.getenv("BOT_DATA_DIR", "./data")))

# Long-only baseline sizing and economic gate. These are deliberately separate
# from TRADE_PERCENT, which is legacy margin sizing and is not used for entries.
RISK_PER_TRADE_FRACTION = 0.005
NOTIONAL_CAP_FRACTION = 0.25
RESEARCH_EXPECTED_MOVE_INPUT_ENABLED = False
RESEARCH_EXPECTED_GROSS_MOVE_PCT = None
RESEARCH_EXPECTED_MOVE_FEED_AGE_SECONDS = None
ROUND_TRIP_FEE_PCT = 0.10
SLIPPAGE_PCT = 0.10
# FUNDING_COST_PCT is a cumulative percentage estimate over the expected hold,
# used when no live hourly rate is supplied. Live rates are hourly decimals and
# are converted using FUNDING_HORIZON_HOURS before the economic gate.
FUNDING_COST_PCT = 0.05
FUNDING_HORIZON_HOURS = 24.0
ADVERSE_SELECTION_PCT = 0.10
_DATA_DIR = DATA_DIR
os.makedirs(_DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(_DATA_DIR, "hyperliquid_trading.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# =============================================================================
# CASCADE DETECTOR SETTINGS
# =============================================================================
CANARY_CHECK_INTERVAL = 60  # seconds between cascade evaluations
LIQUIDATION_FILE = f"{_DATA_DIR}/okx_liquidations.jsonl"
