import ccxt
import pandas as pd
import logging
import requests
import time
from datetime import datetime, timedelta

def fetch_historical_data(symbol: str, timeframe: str = '1h', days: int = 30) -> pd.DataFrame:
    """
    Fetch historical OHLCV data from Binance (has deep history).
    Returns a pandas DataFrame.
    """
    print(f"📥 Fetching {days} days of {timeframe} data for {symbol} from Binance...")
    exchange = ccxt.binance()
    
    # Calculate start time
    start_time = datetime.now() - timedelta(days=days)
    since = int(start_time.timestamp() * 1000)
    
    all_candles = []
    
    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol + '/USDT', timeframe, since=since, limit=1000)
            if not candles:
                break
            
            all_candles.extend(candles)
            
            # Update since timestamp to the last candle + 1ms
            last_timestamp = candles[-1][0]
            since = last_timestamp + 1
            
            # Check if we reached current time
            if last_timestamp >= int(datetime.now().timestamp() * 1000):
                break
                
            print(f"   Fetched {len(candles)} candles... (Latest: {datetime.fromtimestamp(last_timestamp/1000)})")
            
        except Exception as e:
            print(f"❌ Error fetching data: {e}")
            break
            
    if not all_candles:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    
    print(f"✅ Successfully loaded {len(df)} candles.")
    return df

def fetch_hyperliquid_historical_data(coin: str, timeframe: str = '1h', days: int = 7) -> pd.DataFrame:
    """
    Fetch historical OHLCV data from Hyperliquid Info API.
    Note: interval can be 1m, 5m, 15m, 1h, 4h, 1d.
    """
    print(f"📥 Fetching {days} days of {timeframe} data for {coin} from Hyperliquid...")
    url = 'https://api.hyperliquid.xyz/info'
    
    end_time = int(time.time() * 1000)
    start_time = end_time - (days * 86400 * 1000)
    
    # Map common aliases
    if coin == "PEPE": coin = "kPEPE"
    
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": timeframe,
            "startTime": start_time,
            "endTime": end_time
        }
    }
    
    try:
        r = requests.post(url, json=payload)
        data = r.json()
        
        if not data or not isinstance(data, list):
            print(f"❌ No data returned for {coin}")
            return pd.DataFrame()
            
        # HL columns: t (ms), T (ms), s (sym), i (interval), o, h, l, c, v, n
        df = pd.DataFrame(data)
        df = df[['t', 'o', 'h', 'l', 'c', 'v']]
        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        
        # Convert numeric columns
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
            
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        
        print(f"✅ Successfully loaded {len(df)} candles for {coin}.")
        return df
        
    except Exception as e:
        print(f"❌ Error fetching Hyperliquid data for {coin}: {e}")
        return pd.DataFrame()

if __name__ == "__main__":
    # Test fetch
    df = fetch_hyperliquid_historical_data("ETH", "1h", days=3)
    if not df.empty:
        print(df.head())
        print(df.tail())
