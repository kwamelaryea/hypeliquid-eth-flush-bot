from backtest_loader import fetch_historical_data
from backtest_engine import BacktestEngine
import pandas as pd

def main():
    # 1. Fetch Data
    symbol = "ETH"
    print(f"🔍 Starting Backtest for {symbol}...")
    
    try:
        df = fetch_historical_data(symbol, timeframe="15m", days=14)
        if df.empty:
            print("❌ No data fetched. Exiting.")
            return

        # 2. Run Engine
        engine = BacktestEngine(initial_balance=1000)
        results = engine.run(df)
        
        # 3. Report
        print("\n" + "="*40)
        print("📊 BACKTEST RESULTS")
        print("="*40)
        print(f"Final Balance: ${results['final_balance']:.2f}")
        print(f"Return:        {results['return_pct']:.2f}%")
        print(f"Total Trades:  {results['trades']}")
        print("="*40)
        
    except Exception as e:
        print(f"❌ Backtest failed: {e}")

if __name__ == "__main__":
    main()
