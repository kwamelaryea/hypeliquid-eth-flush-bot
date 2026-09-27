import pandas as pd
import numpy as np
from indicators import calculate_ema, calculate_rsi, calculate_atr

class BacktestEngine:
    def __init__(self, initial_balance=1000, leverage=1):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.leverage = leverage
        self.position = None # None, 'long', 'short'
        self.entry_price = 0
        self.entry_size = 0
        self.trades = []
        self.equity_curve = []
        
        # Strategy Parameters
        self.take_profit_pct = 0.15 # Fallback
        self.stop_loss_pct = 0.05   # Fallback
        self.use_atr_stops = True
        self.atr_period = 14
        self.atr_multiplier = 2.0
        self.ema_period = 200
        self.rsi_period = 14
        self.rsi_oversold = 45 # Relaxed for testing ATR stops
        self.rsi_overbought = 70
        
    def calculate_indicators(self, df: pd.DataFrame):
        """Calculate technical indicators using pandas/indicators lib."""
        # Convert to lists for indicators.py (or use pandas native for speed)
        # Using pandas for Backtest is faster/easier than loop-based indicators.py
        
        # EMA
        df['ema'] = df['close'].ewm(span=self.ema_period, adjust=False).mean()
        df['ema_slope'] = df['ema'].diff()
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # ATR
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['atr'] = true_range.rolling(self.atr_period).mean() # Simple ATR for speed
        
        return df

    def detect_flush(self, row, prev_row):
        """
        Heuristic to detect a 'flush' (liquidation cascade) in absence of real liq data.
        Criteria:
        1. Large price drop (Close < Open * 0.98) - 2% drop in one candle
        2. High volume (Volume > 2x Moving Average Volume)
        """
        is_flush_down = (row['close'] < row['open'] * 0.99) and (row['volume'] > row['vol_ma'] * 1.5)
        return is_flush_down

    def run(self, df: pd.DataFrame):
        """Run the backtest loop."""
        print("⚙️ Running backtest simulation...")
        
        # Pre-calc indicators
        df = self.calculate_indicators(df)
        df['vol_ma'] = df['volume'].rolling(window=20).mean()
        
        for index, row in df.iterrows():
            current_price = row['close']
            timestamp = index
            atr = row['atr']
            
            # Record Equity
            unrealized_pnl = 0
            if self.position == 'long':
                 unrealized_pnl = (current_price - self.entry_price) / self.entry_price * self.entry_size * self.entry_price
            
            self.equity_curve.append({
                'timestamp': timestamp,
                'equity': self.balance + unrealized_pnl
            })

            # Check Exit Conditions
            if self.position:
                pnl_pct = 0
                if self.position == 'long':
                    pnl_pct = (current_price - self.entry_price) / self.entry_price
                
                # Check TP/SL
                # Check TP/SL
                if current_price >= self.current_tp_price:
                    self._close_position(current_price, timestamp, 'TP')
                    continue
                elif current_price <= self.current_sl_price:
                    self._close_position(current_price, timestamp, 'SL')
                    continue
            
            # Check Entry Conditions (Flush Logic)
            # We look for a flush to enter *after* it stabilizes or during?
            # The bot buys "flushes".
            
            # Simple Logic: If candle was a huge drop (flush), try to buy the reversal
            if self.detect_flush(row, None):
                # Filter Logic
                can_long = True
                if self.position == 'long': can_long = False
                
                reason = []
                # if row['ema_slope'] <= 0:
                #    can_long = False
                #    reason.append(f"EMA Slope {row['ema_slope']:.4f} <= 0 (Downtrend)")
                
                if row['rsi'] > self.rsi_oversold:
                    can_long = False
                    reason.append(f"RSI {row['rsi']:.1f} > {self.rsi_oversold}")
                    
                if can_long:
                    self._open_position('long', current_price, timestamp, atr)
                else:
                    print(f"⚠️ Flush detected at {timestamp} but filtered: {', '.join(reason)}")

        return self.get_results()

    def _open_position(self, side, price, timestamp, atr):
        self.position = side
        self.entry_price = price
        cost = self.balance * 0.95 # allocate 95%
        self.entry_size = (cost / price) * self.leverage
        # Calculate Dynamic Stops
        if self.use_atr_stops and atr > 0:
            sl_dist = atr * self.atr_multiplier
            if side == 'long':
                self.current_sl_price = price - sl_dist
                self.current_tp_price = price + (sl_dist * 1.5)
        else:
            # Fallback
            if side == 'long':
                self.current_sl_price = price * (1 - self.stop_loss_pct)
                self.current_tp_price = price * (1 + self.take_profit_pct)
                
        self.trades.append({
            'type': 'OPEN',
            'side': side,
            'price': price,
            'time': timestamp,
            'balance': self.balance,
            'atr': atr,
            'sl': self.current_sl_price,
            'tp': self.current_tp_price
        })
        print(f"🟢 OPEN {side} at {price:.2f} (ATR: {atr:.2f}) | SL: {self.current_sl_price:.2f} | TP: {self.current_tp_price:.2f}")

    def _close_position(self, price, timestamp, reason):
        pnl = 0
        if self.position == 'long':
            pnl = (price - self.entry_price) * self.entry_size
            
        self.balance += pnl
        self.trades.append({
            'type': 'CLOSE',
            'side': self.position,
            'price': price,
            'time': timestamp,
            'balance': self.balance,
            'pnl': pnl,
            'reason': reason
        })
        print(f"🔴 CLOSE {self.position} ({reason}) at {price:.2f} on {timestamp} | PnL: ${pnl:.2f}")
        self.position = None
        self.entry_size = 0

    def get_results(self):
        df_trades = pd.DataFrame(self.trades)
        final_balance = self.balance
        return {
            'final_balance': final_balance,
            'return_pct': (final_balance - self.initial_balance) / self.initial_balance * 100,
            'trades': len(df_trades[df_trades['type'] == 'CLOSE']) if not df_trades.empty else 0
        }
