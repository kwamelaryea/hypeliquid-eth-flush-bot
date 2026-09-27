"""
ETH RSI Trading Bot

Buys ETH when RSI is oversold, sells when overbought.
"""

import ccxt
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import config
from rsi import calculate_rsi, get_rsi_signal


def legacy_live_actions_allowed() -> bool:
    """Require independent environment, runtime, and credential opt-ins."""
    return bool(
        not getattr(config, "DRY_RUN", True)
        and getattr(config, "EXCHANGE_LIVE_TRADING_OPT_IN", False)
        and getattr(config, "EXCHANGE_LIVE_RUNTIME_OPT_IN", False)
        and getattr(config, "API_KEY", "")
        and getattr(config, "API_SECRET", "")
    )


class RSITradingBot:
    """RSI-based trading bot for ETH."""
    
    def __init__(self):
        self.setup_logging()
        self.exchange = self._init_exchange()
        self.position = None  # Track if we have a position
        self.last_trade_price = None
        
    def setup_logging(self):
        """Configure logging."""
        logging.basicConfig(
            level=getattr(logging, config.LOG_LEVEL),
            format='%(asctime)s | %(levelname)s | %(message)s',
            handlers=[
                logging.FileHandler(config.LOG_FILE),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def _init_exchange(self) -> ccxt.Exchange:
        """Initialize the exchange connection."""
        exchange_class = getattr(ccxt, config.EXCHANGE)
        
        exchange = exchange_class({
            'apiKey': config.API_KEY,
            'secret': config.API_SECRET,
            'sandbox': config.DRY_RUN,  # Use testnet if dry run
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot'
            }
        })
        
        self.logger.info(f"Initialized {config.EXCHANGE.upper()} exchange")
        self.logger.info(f"Mode: {'DRY RUN (Paper Trading)' if config.DRY_RUN else '🔴 LIVE TRADING'}")
        
        return exchange
    
    def fetch_ohlcv(self, limit: int = 100) -> list:
        """Fetch OHLCV candle data."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(
                config.SYMBOL,
                timeframe=config.TIMEFRAME,
                limit=limit
            )
            return ohlcv
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV: {e}")
            return []
    
    def get_closing_prices(self, ohlcv: list) -> list:
        """Extract closing prices from OHLCV data."""
        # OHLCV format: [timestamp, open, high, low, close, volume]
        return [candle[4] for candle in ohlcv]
    
    def get_current_price(self) -> Optional[float]:
        """Get current ETH price."""
        try:
            ticker = self.exchange.fetch_ticker(config.SYMBOL)
            return ticker['last']
        except Exception as e:
            self.logger.error(f"Error fetching price: {e}")
            return None
    
    def get_balance(self) -> Dict[str, float]:
        """Get account balance."""
        try:
            if config.DRY_RUN:
                # Simulated balance for dry run
                return {'USDT': 10000.0, 'ETH': 1.0}
            
            balance = self.exchange.fetch_balance()
            return {
                'USDT': balance.get('USDT', {}).get('free', 0),
                'ETH': balance.get('ETH', {}).get('free', 0)
            }
        except Exception as e:
            self.logger.error(f"Error fetching balance: {e}")
            return {'USDT': 0, 'ETH': 0}
    
    def execute_buy(self, price: float) -> bool:
        """Execute a buy order."""
        amount = config.TRADE_AMOUNT_USDT / price
        
        self.logger.info(f"🟢 BUY SIGNAL - Executing buy order")
        self.logger.info(f"   Amount: {amount:.6f} ETH @ ${price:.2f}")
        
        if not legacy_live_actions_allowed():
            self.logger.info("   [DRY RUN] Order simulated - live opt-ins not satisfied")
            self.position = 'long'
            self.last_trade_price = price
            return True

        try:
            order = self.exchange.create_market_buy_order(
                config.SYMBOL,
                amount
            )
            self.logger.info(f"   Order filled: {order}")
            self.position = 'long'
            self.last_trade_price = price
            return True
        except Exception as e:
            self.logger.error(f"   Buy order failed: {e}")
            return False
    
    def execute_sell(self, price: float) -> bool:
        """Execute a sell order."""
        balance = self.get_balance()
        amount = min(balance['ETH'], config.TRADE_AMOUNT_USDT / price)
        
        if amount <= 0:
            self.logger.warning("   No ETH to sell")
            return False
        
        self.logger.info(f"🔴 SELL SIGNAL - Executing sell order")
        self.logger.info(f"   Amount: {amount:.6f} ETH @ ${price:.2f}")
        
        if self.last_trade_price:
            pnl = (price - self.last_trade_price) * amount
            pnl_pct = ((price / self.last_trade_price) - 1) * 100
            self.logger.info(f"   P&L: ${pnl:.2f} ({pnl_pct:+.2f}%)")
        
        if not legacy_live_actions_allowed():
            self.logger.info("   [DRY RUN] Order simulated - live opt-ins not satisfied")
            self.position = None
            return True

        try:
            order = self.exchange.create_market_sell_order(
                config.SYMBOL,
                amount
            )
            self.logger.info(f"   Order filled: {order}")
            self.position = None
            return True
        except Exception as e:
            self.logger.error(f"   Sell order failed: {e}")
            return False
    
    def analyze_and_trade(self) -> Dict[str, Any]:
        """Main trading logic - analyze RSI and execute trades."""
        result = {
            'timestamp': datetime.now().isoformat(),
            'price': None,
            'rsi': None,
            'signal': None,
            'action_taken': None
        }
        
        # Fetch price data
        ohlcv = self.fetch_ohlcv(limit=config.RSI_PERIOD + 50)
        if not ohlcv:
            self.logger.error("Could not fetch price data")
            return result
        
        # Calculate RSI
        prices = self.get_closing_prices(ohlcv)
        current_price = prices[-1]
        
        try:
            rsi = calculate_rsi(prices, config.RSI_PERIOD)
        except ValueError as e:
            self.logger.error(f"RSI calculation error: {e}")
            return result
        
        # Get signal
        signal, description = get_rsi_signal(
            rsi,
            config.RSI_OVERSOLD,
            config.RSI_OVERBOUGHT
        )
        
        result['price'] = current_price
        result['rsi'] = rsi
        result['signal'] = signal
        
        # Log status
        self.logger.info(f"─" * 60)
        self.logger.info(f"ETH Price: ${current_price:.2f} | RSI: {rsi} | Signal: {signal}")
        self.logger.info(f"{description}")
        
        # Execute trade based on signal
        if signal == "BUY" and self.position != 'long':
            if self.execute_buy(current_price):
                result['action_taken'] = 'BUY'
                
        elif signal == "SELL" and self.position == 'long':
            if self.execute_sell(current_price):
                result['action_taken'] = 'SELL'
        else:
            result['action_taken'] = 'HOLD'
            
        return result
    
    def run(self):
        """Run the bot continuously."""
        self.logger.info("=" * 60)
        self.logger.info("ETH RSI TRADING BOT STARTED")
        self.logger.info("=" * 60)
        self.logger.info(f"Symbol: {config.SYMBOL}")
        self.logger.info(f"Timeframe: {config.TIMEFRAME}")
        self.logger.info(f"RSI Period: {config.RSI_PERIOD}")
        self.logger.info(f"Oversold: {config.RSI_OVERSOLD} | Overbought: {config.RSI_OVERBOUGHT}")
        self.logger.info(f"Trade Amount: ${config.TRADE_AMOUNT_USDT}")
        self.logger.info(f"Check Interval: {config.CHECK_INTERVAL_SECONDS}s")
        self.logger.info("=" * 60)
        
        while True:
            try:
                self.analyze_and_trade()
                time.sleep(config.CHECK_INTERVAL_SECONDS)
                
            except KeyboardInterrupt:
                self.logger.info("\nBot stopped by user")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}")
                time.sleep(10)  # Wait before retrying


def run_once():
    """Run a single analysis (useful for testing)."""
    bot = RSITradingBot()
    return bot.analyze_and_trade()


if __name__ == "__main__":
    bot = RSITradingBot()
    bot.run()
