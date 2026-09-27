#!/usr/bin/env python3
"""
ETH RSI Trading Bot - CLI Runner

Usage:
    python run.py              # Run bot continuously
    python run.py --once       # Run single analysis
    python run.py --backtest   # Run backtest on historical data
    python run.py --status     # Check current RSI and price
"""

import argparse
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(
        description="ETH RSI Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run.py                  # Run bot continuously
    python run.py --once           # Single RSI check
    python run.py --status         # Current market status
    python run.py --backtest 30    # Backtest last 30 days
        """
    )
    
    parser.add_argument(
        '--once', '-1',
        action='store_true',
        help='Run a single analysis and exit'
    )
    
    parser.add_argument(
        '--status', '-s',
        action='store_true',
        help='Show current price and RSI status'
    )
    
    parser.add_argument(
        '--backtest', '-b',
        type=int,
        metavar='DAYS',
        help='Run backtest for N days of historical data'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=True,
        help='Run in paper trading mode (default: True)'
    )
    
    parser.add_argument(
        '--live',
        action='store_true',
        help='⚠️  Enable LIVE trading (real money!)'
    )
    
    args = parser.parse_args()
    
    # Import here to allow --help without dependencies
    import config
    
    # Import/direct execution stays paper-only. Live requires both this CLI flag
    # and the independent environment opt-in plus credentials.
    config.DRY_RUN = True
    config.EXCHANGE_LIVE_RUNTIME_OPT_IN = False
    if args.live:
        if not getattr(config, "EXCHANGE_LIVE_TRADING_OPT_IN", False):
            print("Live mode requires EXCHANGE_ENABLE_LIVE_TRADING=true.", file=sys.stderr)
            sys.exit(2)
        if not config.API_KEY or not config.API_SECRET:
            print("Live mode requires EXCHANGE_API_KEY and EXCHANGE_API_SECRET.", file=sys.stderr)
            sys.exit(2)
        print("⚠️  WARNING: LIVE TRADING MODE ENABLED!")
        print("    Real orders will be placed on the exchange.")
        confirm = input("    Type 'YES' to confirm: ")
        if confirm != 'YES':
            print("Aborted.")
            sys.exit(0)
        config.EXCHANGE_LIVE_RUNTIME_OPT_IN = True
        config.DRY_RUN = False

    if args.status:
        show_status()
    elif args.once:
        run_single()
    elif args.backtest:
        run_backtest(args.backtest)
    else:
        run_continuous()


def show_status():
    """Show current market status."""
    from bot import RSITradingBot
    import config
    from rsi import get_rsi_signal, calculate_rsi
    
    print("\n" + "=" * 50)
    print("📊 ETH RSI STATUS")
    print("=" * 50)
    
    bot = RSITradingBot()
    
    # Get current price
    price = bot.get_current_price()
    if price:
        print(f"💰 Current Price: ${price:,.2f}")
    
    # Get RSI
    ohlcv = bot.fetch_ohlcv(limit=100)
    if ohlcv:
        prices = bot.get_closing_prices(ohlcv)
        rsi = calculate_rsi(prices, config.RSI_PERIOD)
        signal, desc = get_rsi_signal(rsi, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)
        
        # RSI visualization
        rsi_bar = create_rsi_bar(rsi)
        
        print(f"📈 RSI ({config.RSI_PERIOD}): {rsi}")
        print(f"   {rsi_bar}")
        print(f"🎯 Signal: {signal}")
        print(f"   {desc}")
    
    print("=" * 50 + "\n")


def create_rsi_bar(rsi: float) -> str:
    """Create a visual RSI bar."""
    bar_width = 40
    position = int((rsi / 100) * bar_width)
    
    bar = ""
    for i in range(bar_width):
        if i < bar_width * 0.3:  # Oversold zone
            char = "🟢" if i == position else "░"
        elif i > bar_width * 0.7:  # Overbought zone
            char = "🔴" if i == position else "░"
        else:  # Neutral zone
            char = "🟡" if i == position else "░"
        bar += char
    
    return f"[{bar}] 0━━━30━━━━━━━━70━━━100"


def run_single():
    """Run a single analysis."""
    from bot import run_once
    
    print("\n🔍 Running single RSI analysis...\n")
    result = run_once()
    
    print("\n📋 Result:")
    print(f"   Price: ${result['price']:,.2f}" if result['price'] else "   Price: N/A")
    print(f"   RSI: {result['rsi']}" if result['rsi'] else "   RSI: N/A")
    print(f"   Signal: {result['signal']}" if result['signal'] else "   Signal: N/A")
    print(f"   Action: {result['action_taken']}" if result['action_taken'] else "   Action: None")
    print()


def run_continuous():
    """Run the bot continuously."""
    from bot import RSITradingBot
    
    print("\n🤖 Starting ETH RSI Trading Bot...")
    print("   Press Ctrl+C to stop\n")
    
    bot = RSITradingBot()
    bot.run()


def run_backtest(days: int):
    """Run backtest on historical data."""
    import config
    from rsi import calculate_rsi, get_rsi_signal
    from bot import RSITradingBot
    
    print(f"\n📊 Running backtest for {days} days...")
    
    bot = RSITradingBot()
    
    # Fetch historical data
    # Approximate candles needed based on timeframe
    timeframe_hours = {
        '1m': 1/60, '5m': 5/60, '15m': 15/60, '30m': 0.5,
        '1h': 1, '4h': 4, '1d': 24
    }
    hours = timeframe_hours.get(config.TIMEFRAME, 1)
    candles_needed = int((days * 24) / hours)
    
    ohlcv = bot.fetch_ohlcv(limit=min(candles_needed, 1000))
    if not ohlcv:
        print("❌ Could not fetch historical data")
        return
    
    prices = bot.get_closing_prices(ohlcv)
    
    # Run backtest
    trades = []
    position = None
    entry_price = None
    
    print(f"   Analyzing {len(prices)} candles...\n")
    
    for i in range(config.RSI_PERIOD + 1, len(prices)):
        price_slice = prices[:i+1]
        rsi = calculate_rsi(price_slice, config.RSI_PERIOD)
        signal, _ = get_rsi_signal(rsi, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)
        current_price = prices[i]
        
        if signal == "BUY" and position != 'long':
            position = 'long'
            entry_price = current_price
            trades.append({
                'type': 'BUY',
                'price': current_price,
                'rsi': rsi,
                'index': i
            })
            
        elif signal == "SELL" and position == 'long':
            pnl_pct = ((current_price / entry_price) - 1) * 100
            trades.append({
                'type': 'SELL',
                'price': current_price,
                'rsi': rsi,
                'index': i,
                'pnl_pct': pnl_pct
            })
            position = None
    
    # Print results
    print("=" * 60)
    print("📈 BACKTEST RESULTS")
    print("=" * 60)
    print(f"Period: {len(prices)} candles ({config.TIMEFRAME})")
    print(f"Total Trades: {len(trades)}")
    
    if trades:
        sells = [t for t in trades if t['type'] == 'SELL']
        if sells:
            total_pnl = sum(t['pnl_pct'] for t in sells)
            avg_pnl = total_pnl / len(sells)
            winners = len([t for t in sells if t['pnl_pct'] > 0])
            
            print(f"Completed Trades: {len(sells)}")
            print(f"Win Rate: {(winners/len(sells))*100:.1f}%")
            print(f"Total Return: {total_pnl:.2f}%")
            print(f"Avg Return/Trade: {avg_pnl:.2f}%")
        
        print("\n📝 Trade Log:")
        for t in trades[-10:]:  # Last 10 trades
            emoji = "🟢" if t['type'] == 'BUY' else "🔴"
            pnl = f" ({t['pnl_pct']:+.2f}%)" if 'pnl_pct' in t else ""
            print(f"   {emoji} {t['type']} @ ${t['price']:.2f} (RSI: {t['rsi']}){pnl}")
    
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
