import numpy as np
from typing import List, Tuple

def calculate_ema(prices: List[float], period: int = 200) -> float:
    """
    Calculate the Exponential Moving Average (EMA).
    """
    if len(prices) < period:
        raise ValueError(f"Need at least {period} prices, got {len(prices)}")
    
    prices = np.array(prices)
    alpha = 2 / (period + 1)
    
    # Simple Moving Average for the first value
    ema = np.mean(prices[:period])
    
    # Calculate subsequent EMA values
    for price in prices[period:]:
        ema = (price - ema) * alpha + ema
        
    return round(float(ema), 2)

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """
    Calculate the RSI for a list of closing prices.
    """
    if len(prices) < period + 1:
        raise ValueError(f"Need at least {period + 1} prices, got {len(prices)}")
    
    prices = np.array(prices)
    deltas = np.diff(prices)
    
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return round(rsi, 2)

def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """
    Calculate the Average True Range (ATR).
    """
    if len(highs) < period + 1:
        return 0.0

    tr_values = []
    # Calculate TR for each period
    # TR = Max(High - Low, abs(High - PrevClose), abs(Low - PrevClose))
    
    # We need prev close, so start from index 1
    for i in range(1, len(highs)):
        h = highs[i]
        l = lows[i]
        pc = closes[i-1]
        
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_values.append(tr)
        
    if len(tr_values) < period:
        return 0.0
        
    # Initial ATR is SMA of TR
    atr = np.mean(tr_values[:period])
    
    # Subsequent ATR values (Wilder's Smoothing)
    # ATR = ((Prev ATR * (period - 1)) + Current TR) / period
    for i in range(period, len(tr_values)):
        atr = (atr * (period - 1) + tr_values[i]) / period
        
    return round(float(atr), 4)
