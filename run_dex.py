#!/usr/bin/env python3
"""
ETH RSI Trading Bot - DEX Runner

Usage:
    python run_dex.py              # Run bot continuously
    python run_dex.py --once       # Run single analysis
    python run_dex.py --status     # Check current status
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="ETH RSI Trading Bot - DEX Version (Uniswap)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Networks supported: ethereum, arbitrum, polygon, base, optimism

Examples:
    python run_dex.py --status     # Current RSI and DEX price
    python run_dex.py --once       # Single analysis
    python run_dex.py              # Run continuously
    python run_dex.py --live       # ⚠️ Enable real trades
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
        help='Show current price, RSI, and wallet status'
    )
    
    parser.add_argument(
        '--live',
        action='store_true',
        help='⚠️ Enable LIVE trading (real swaps!)'
    )
    
    parser.add_argument(
        '--network', '-n',
        choices=['arbitrum'],
        help='Override network (default: arbitrum)'
    )
    
    args = parser.parse_args()
    
    import config_dex as config
    
    # Override network if specified
    if args.network:
        config.NETWORK = args.network
        config.DEX_NETWORK_VALID = args.network in config.TOKENS and args.network in config.UNISWAP_ROUTER
        print(f"Network: {args.network.upper()}")

    # The DEX runner is paper-only unless both the CLI and environment opt in.
    config.DRY_RUN = True
    config.DEX_LIVE_RUNTIME_OPT_IN = False
    if args.live:
        if not getattr(config, "DEX_LIVE_TRADING_OPT_IN", False):
            print("DEX live mode requires DEX_ENABLE_LIVE_TRADING=true.", file=sys.stderr)
            sys.exit(2)
        if not getattr(config, "DEX_NETWORK_VALID", False) or not config.WALLET_PRIVATE_KEY:
            print("DEX live mode requires a valid network and WALLET_PRIVATE_KEY.", file=sys.stderr)
            sys.exit(2)
        print("\n⚠️  WARNING: LIVE DEX TRADING MODE!")
        print("    Real swaps will be executed on-chain.")
        print(f"    Network: {config.NETWORK.upper()}")
        print(f"    This will cost real gas + trade real tokens.\n")
        confirm = input("    Type 'YES' to confirm: ")
        if confirm != 'YES':
            print("Aborted.")
            sys.exit(0)
        config.DEX_LIVE_RUNTIME_OPT_IN = True
        config.DRY_RUN = False

    if args.status:
        show_status()
    elif args.once:
        run_single()
    else:
        run_continuous()


def show_status():
    """Show current DEX status."""
    import config_dex as config
    from dex_trader import DEXTrader
    from rsi import calculate_rsi, get_rsi_signal
    import ccxt
    
    print("\n" + "=" * 55)
    print("📊 ETH RSI STATUS - DEX MODE")
    print("=" * 55)
    
    # Initialize DEX
    dex = DEXTrader()
    print(f"🌐 Network: {config.NETWORK.upper()}")
    print(f"🔗 Connected: {'✅' if dex.is_connected() else '❌'}")
    
    # Get DEX price
    dex_price = dex.get_eth_price()
    if dex_price:
        print(f"💰 DEX Price: ${dex_price:,.2f}")
    
    # Get RSI from Kraken candles (works globally)
    exchange = ccxt.kraken({'enableRateLimit': True})
    ohlcv = exchange.fetch_ohlcv("ETH/USD", timeframe=config.TIMEFRAME, limit=100)
    prices = [c[4] for c in ohlcv]
    
    rsi = calculate_rsi(prices, config.RSI_PERIOD)
    signal, desc = get_rsi_signal(rsi, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)
    
    # RSI visualization
    print(f"\n📈 RSI ({config.RSI_PERIOD}): {rsi}")
    print(f"   {create_rsi_bar(rsi)}")
    print(f"🎯 Signal: {signal}")
    print(f"   {desc}")
    
    # Wallet balances
    print(f"\n💳 Wallet Balances:")
    balances = dex.get_balances()
    for token, amount in balances.items():
        if amount > 0:
            print(f"   {token}: {amount:.6f}")
    
    # Gas reserve check
    eth_balance = balances.get('ETH', 0)
    min_gas = config.get_min_gas_reserve()
    if eth_balance < min_gas:
        print(f"\n⛽ ⚠️  LOW GAS WARNING!")
        print(f"   Current: {eth_balance:.6f} ETH")
        print(f"   Minimum: {min_gas:.6f} ETH")
        print(f"   Trades will be SKIPPED until you add more ETH for gas.")
    else:
        print(f"\n⛽ Gas Reserve: {eth_balance:.6f} ETH ✅")
    
    print("=" * 55 + "\n")


def create_rsi_bar(rsi: float) -> str:
    """Create a visual RSI bar."""
    bar_width = 30
    position = int((rsi / 100) * bar_width)
    
    bar = ""
    for i in range(bar_width):
        if i < bar_width * 0.3:
            char = "🟢" if i == position else "░"
        elif i > bar_width * 0.7:
            char = "🔴" if i == position else "░"
        else:
            char = "🟡" if i == position else "░"
        bar += char
    
    return f"[{bar}]"


def run_single():
    """Run a single analysis."""
    from dex_bot import run_once
    
    print("\n🔍 Running single DEX RSI analysis...\n")
    result = run_once()
    
    print("\n📋 Result:")
    print(f"   Network: {result.get('network', 'N/A').upper()}")
    print(f"   Price: ${result['price']:,.2f}" if result['price'] else "   Price: N/A")
    print(f"   RSI: {result.get('rsi', 'N/A')}")
    print(f"   Signal: {result['signal']}" if result['signal'] else "   Signal: N/A")
    print(f"   Action: {result['action_taken']}" if result['action_taken'] else "   Action: None")
    print()


def run_continuous():
    """Run the bot continuously."""
    from dex_bot import DEXFlushBot
    
    print("\n🤖 Starting DEX RSI Trading Bot...")
    print("   Press Ctrl+C to stop\n")
    
    bot = DEXFlushBot()
    bot.run()


if __name__ == "__main__":
    main()
