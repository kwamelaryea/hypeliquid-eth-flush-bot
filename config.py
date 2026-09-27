"""
Configuration for ETH RSI Trading Bot

⚠️  IMPORTANT: Set your API keys as environment variables:
    export EXCHANGE_API_KEY="your_api_key"
    export EXCHANGE_API_SECRET="your_api_secret"
    
Never hardcode API keys in this file!
"""

import os

# =============================================================================
# EXCHANGE SETTINGS
# =============================================================================
EXCHANGE = os.getenv("EXCHANGE", "binance")  # Supported by the selected CCXT adapter.

# API Credentials (from environment variables)
API_KEY = os.getenv("EXCHANGE_API_KEY", "")
API_SECRET = os.getenv("EXCHANGE_API_SECRET", "")

# Trading pair
SYMBOL = os.getenv("SYMBOL", "ETH/USDT")

# =============================================================================
# RSI SETTINGS
# =============================================================================
RSI_PERIOD = 14          # Standard RSI period
RSI_OVERSOLD = 30        # Buy signal threshold (RSI below this)
RSI_OVERBOUGHT = 70      # Sell signal threshold (RSI above this)

# =============================================================================
# TRADING SETTINGS
# =============================================================================
TRADE_AMOUNT_USDT = 100  # Amount in USDT per trade
TIMEFRAME = os.getenv("TIMEFRAME", "1h")         # Candle timeframe: 1m, 5m, 15m, 1h, 4h, 1d

# =============================================================================
# BOT SETTINGS
# =============================================================================
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
# The environment and runtime/CLI opt-ins are deliberately independent. Importing
# or directly executing bot.py always remains paper-only.
EXCHANGE_LIVE_TRADING_OPT_IN = os.getenv("EXCHANGE_ENABLE_LIVE_TRADING", "false").strip().lower() in {"1", "true", "yes", "on"}
EXCHANGE_LIVE_RUNTIME_OPT_IN = False
DRY_RUN = True

# =============================================================================
# LOGGING
# =============================================================================
LOG_FILE = "logs/trading.log"
LOG_LEVEL = "INFO"
