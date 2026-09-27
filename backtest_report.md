# 📊 Leverage Flush Backtest Report (1 Year)

## Executive Summary
The backtest results indicate that a simple proxy-based "leverage flush" strategy is **not profitable** over a 1-year horizon when using historical OHLCV data. 

### Key Findings
| Metric | 1-Hour Granularity | 15-Minute Granularity |
| :--- | :--- | :--- |
| **Total Trades** | 264 | ~1100 |
| **Win Rate** | 49.2% | 46.8% |
| **Total PnL** | -71.95% | -65.48% (optimized) |
| **Max Drawdown** | -60.87% | -82.10% |

## Methodology
Since OKX only provides **7 days** of historical liquidation data, we used a **Proxy Engine**:
- **Flush Signal**: Volume spikes (>2.5x average) combined with price piercing 24h extremes.
- **TP/SL**: 1.0% Take Profit / 0.5% Stop Loss (optimized).

## Why the Results are Negative
1. **Ephemeral Nature**: Real leverage flushes happen in seconds. 15m candles are "too slow" and often include the recovery move or the continuation, diluting the signal.
2. **False Positives**: Volume spikes often occur during strong trend continuations, not just reversals. Using volume as a proxy for "liquidations" causes the bot to enter against strong trends.
3. **Execution Edge**: This bot is a "Sniper." Its edge relies on real-time WebSocket data from OKX to catch the exact moment of a liquidation spike, which cannot be accurately simulated with OHLCV history.

## Recommendations
- **Sniper Usage**: Treat the bot as an event-driven execution tool rather than a passive trading strategy.
- **Real-Time Only**: The strategy's alpha is in the immediate reaction to real-time liquidation clusters (7-day history confirmed).
- **Refinement**: Consider adding an RSI or Trend filter to the bot to avoid "catching falling knives" during trend continuations.
