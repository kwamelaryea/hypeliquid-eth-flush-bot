"""
RSI (Relative Strength Index) Calculator

RSI = 100 - (100 / (1 + RS))
RS = Average Gain / Average Loss over N periods
"""

import numpy as np
from typing import List, Tuple


def calculate_ema(prices: List[float], period: int = 200) -> float:
    """
    Calculate the Exponential Moving Average (EMA).
    
    Args:
        prices: List of closing prices (oldest first)
        period: EMA period (default 200)
        
    Returns:
        Current EMA value
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
    
    Args:
        prices: List of closing prices (oldest first)
        period: RSI period (default 14)
        
    Returns:
        Current RSI value (0-100)
    """
    if len(prices) < period + 1:
        raise ValueError(f"Need at least {period + 1} prices, got {len(prices)}")
    
    prices = np.array(prices)
    
    # Calculate price changes
    deltas = np.diff(prices)
    
    # Separate gains and losses
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    # Calculate initial average gain/loss (SMA)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    # Use Wilder's smoothing method for subsequent values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    # Calculate RS and RSI
    if avg_loss == 0:
        return 100.0  # No losses = maximum RSI
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return round(rsi, 2)


def calculate_rsi_series(prices: List[float], period: int = 14) -> List[float]:
    """
    Calculate RSI for entire price series.
    
    Args:
        prices: List of closing prices
        period: RSI period
        
    Returns:
        List of RSI values (first `period` values will be None)
    """
    if len(prices) < period + 1:
        return []
    
    prices = np.array(prices)
    deltas = np.diff(prices)
    
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    rsi_values = [None] * period
    
    # Initial averages
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    # First RSI value
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(round(100 - (100 / (1 + rs)), 2))
    
    # Subsequent values using Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(round(100 - (100 / (1 + rs)), 2))
    
    return rsi_values


def get_rsi_signal(rsi: float, oversold: float = 30, overbought: float = 70) -> Tuple[str, str]:
    """
    Determine trading signal based on RSI.
    
    Args:
        rsi: Current RSI value
        oversold: Oversold threshold (default 30)
        overbought: Overbought threshold (default 70)
        
    Returns:
        Tuple of (signal, description)
        signal: "BUY", "SELL", or "HOLD"
    """
    if rsi <= oversold:
        return "BUY", f"RSI {rsi} is OVERSOLD (≤{oversold}) - Buy signal"
    elif rsi >= overbought:
        return "SELL", f"RSI {rsi} is OVERBOUGHT (≥{overbought}) - Sell signal"
    else:
        return "HOLD", f"RSI {rsi} is neutral ({oversold}-{overbought}) - Hold"


if __name__ == "__main__":
    # Test with sample data
    test_prices = [
        44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
        46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
        46.22, 45.64
    ]
    
    rsi = calculate_rsi(test_prices)
    signal, desc = get_rsi_signal(rsi)
    
    print(f"RSI: {rsi}")
    print(f"Signal: {signal}")
    print(f"Description: {desc}")
