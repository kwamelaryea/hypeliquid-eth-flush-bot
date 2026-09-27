import pandas as pd
import numpy as np
import time
import requests
import logging
from datetime import datetime, timedelta
from backtest_loader import fetch_hyperliquid_historical_data
from cascade_detector import CascadeDetector, RiskLevel

class MockInfo:
    """Mock for Hyperliquid Info client to simulate historical data."""
    def __init__(self, token_data, funding_data):
        self.token_data = token_data  # Dict: symbol -> DataFrame
        self.funding_data = funding_data  # Dict: symbol -> List of funding items
        self.current_time = None

    def set_time(self, timestamp):
        # timestamp is a pandas Timestamp or datetime
        self.current_time = timestamp

    def candle_snapshot(self, coin, interval, startTime, endTime):
        if coin == "BTC": coin = "BTC" # Just making sure
        df = self.token_data.get(coin, pd.DataFrame())
        if df.empty: return []
        
        # Filter for the range
        mask = (df.index >= pd.to_datetime(startTime, unit='ms')) & (df.index <= pd.to_datetime(endTime, unit='ms'))
        res = df.loc[mask].copy()
        
        # Critical: Simulation must NOT see data after current_time
        if self.current_time:
            res = res[res.index <= self.current_time]
            
        hl_data = []
        for ts, row in res.iterrows():
            hl_data.append({
                't': int(ts.timestamp() * 1000),
                'o': str(row['open']),
                'h': str(row['high']),
                'l': str(row['low']),
                'c': str(row['close']),
                'v': str(row['volume'])
            })
        return hl_data

    def funding_history(self, coin, startTime=None):
        all_funding = self.funding_data.get(coin, [])
        if not self.current_time: return all_funding
        limit_ts = int(self.current_time.timestamp() * 1000)
        # Filter items up to current simulation time
        return [f for f in all_funding if f['time'] <= limit_ts]

class HistoricalBacktester:
    def __init__(self, main_symbol="ETH", days=7):
        self.main_symbol = main_symbol
        self.days = days
        self.canary_tokens = ["WLFI", "HYPE", "WIF", "PEPE", "TRUMP", "MELANIA"]
        self.all_symbols = [main_symbol, "BTC"] + self.canary_tokens
        self.logger = logging.getLogger("Backtest")
        logging.basicConfig(level=logging.INFO)
        
    def fetch_all_data(self):
        token_data = {}
        funding_data = {}
        
        url = 'https://api.hyperliquid.xyz/info'
        
        for sym in self.all_symbols:
            # Fetch OHLCV
            df = fetch_hyperliquid_historical_data(sym, "1h", days=self.days + 1)
            if not df.empty:
                token_data[sym] = df
            
            # Fetch Funding
            coin = sym
            if sym == "PEPE": coin = "kPEPE"
            
            payload = {
                "type": "fundingHistory", 
                "coin": coin, 
                "startTime": int((time.time() - (self.days + 2) * 86400) * 1000)
            }
            try:
                r = requests.post(url, json=payload)
                data = r.json()
                if isinstance(data, list):
                    # Sort by time asc
                    funding_data[sym] = sorted(data, key=lambda x: x['time'])
                else:
                    funding_data[sym] = []
            except Exception as e:
                print(f"Error fetching funding for {sym}: {e}")
                funding_data[sym] = []
                
        return token_data, funding_data

    def run(self):
        print(f"🚀 Starting Real-World Backtest ({self.days} days)...")
        token_data, funding_data = self.fetch_all_data()
        
        if self.main_symbol not in token_data:
            print(f"❌ Could not find data for {self.main_symbol}")
            return

        mock_info = MockInfo(token_data, funding_data)
        detector = CascadeDetector(mock_info, self.logger)
        
        # Primary series for markers
        main_df = token_data[self.main_symbol]
        
        # Common timestamps
        common_ts = main_df.index.tolist()
        
        results_baseline = {"capital": 1000.0, "position": 0, "entry": 0.0, "trades": 0, "max_dd": 0.0}
        results_augmented = {"capital": 1000.0, "position": 0, "entry": 0.0, "trades": 0, "max_dd": 0.0}
        
        peak_b = 1000.0
        peak_a = 1000.0
        
        print(f"{'Time':<20} | {'Price':<8} | {'Risk':<10} | {'Action':<15}")
        print("-" * 60)
        
        for i, ts in enumerate(common_ts):
            if i < 20: continue # Wait for EMA/RSI warm up
            
            mock_info.set_time(ts)
            current_price = main_df.loc[ts, 'close']
            
            # 1. Evaluate Cascade Detector
            cascade_signal = detector.evaluate(force=True)
            risk = cascade_signal.risk_level if cascade_signal else RiskLevel.NORMAL
            score = cascade_signal.risk_score if cascade_signal else 0
            
            # 2. Heuristic Signal Logic (Simplified Flush strategy for backtest)
            # In real bot, this looks at clusters. Here we use RSI/EMA as proxy
            # Baseline: RSI < 30 and Price < EMA (Simplified dip-buy)
            # (In reality we want to see if the detector BLOCKS a bad trade)
            
            # Calculate simple RSI on main_df up to ts
            past_prices = main_df.iloc[:i+1]['close'].values
            if len(past_prices) < 15: continue
            
            # Very basic RSI/EMA for backtest purposes
            ema = main_df.iloc[:i+1]['close'].ewm(span=20).mean().iloc[-1]
            
            # Baseline Logic
            signal = "HOLD"
            if current_price < ema * 0.98: # 2% dip
                signal = "LONG"
                
            # Augmented Logic check
            trade_params = detector.get_trading_params(10.0, 1.5)
            allow_long = trade_params["allow_long"]
            
            # Simulation Loop for Baseline
            if results_baseline["position"] == 0 and signal == "LONG":
                results_baseline["position"] = 1
                results_baseline["entry"] = current_price
                results_baseline["trades"] += 1
            elif results_baseline["position"] == 1:
                pnl = (current_price / results_baseline["entry"] - 1)
                if pnl > 0.015 or pnl < -0.01: # TP 1.5% or SL 1%
                    results_baseline["capital"] *= (1 + pnl)
                    results_baseline["position"] = 0
                    peak_b = max(peak_b, results_baseline["capital"])
                    dd = (peak_b - results_baseline["capital"]) / peak_b
                    results_baseline["max_dd"] = max(results_baseline["max_dd"], dd)
            
            # Simulation Loop for Augmented
            if results_augmented["position"] == 0:
                if signal == "LONG" and allow_long:
                    results_augmented["position"] = 1
                    results_augmented["entry"] = current_price
                    results_augmented["trades"] += 1
                elif risk == RiskLevel.CRITICAL and current_price < ema:
                    # Cascade short opportunity
                    results_augmented["position"] = -1
                    results_augmented["entry"] = current_price
                    results_augmented["trades"] += 1
            elif results_augmented["position"] == 1:
                pnl = (current_price / results_augmented["entry"] - 1)
                if pnl > 0.015 or pnl < -0.01:
                    results_augmented["capital"] *= (1 + pnl)
                    results_augmented["position"] = 0
            elif results_augmented["position"] == -1:
                pnl = (results_augmented["entry"] / current_price - 1)
                if pnl > 0.03 or pnl < -0.015: # TP 3% (rides cascade) or SL 1.5%
                    results_augmented["capital"] *= (1 + pnl)
                    results_augmented["position"] = 0
            
            results_augmented["max_dd"] = max(results_augmented["max_dd"], (peak_a - results_augmented["capital"]) / peak_a if peak_a > 0 else 0)
            peak_a = max(peak_a, results_augmented["capital"])

            if risk != RiskLevel.NORMAL:
                print(f"{str(ts):<20} | {current_price:<8.2f} | {risk.value:<10} | {'Blocked Long' if not allow_long else 'Elevated'}")

        print("-" * 60)
        print("FINAL RESULTS")
        print(f"Strategy         | Return | Max DD | Trades")
        print(f"Baseline         | {(results_baseline['capital']/1000-1)*100:>5.1f}% | {results_baseline['max_dd']*100:>5.1f}% | {results_baseline['trades']}")
        print(f"Augmented        | {(results_augmented['capital']/1000-1)*100:>5.1f}% | {results_augmented['max_dd']*100:>5.1f}% | {results_augmented['trades']}")
        
if __name__ == "__main__":
    backtester = HistoricalBacktester(days=7)
    backtester.run()
