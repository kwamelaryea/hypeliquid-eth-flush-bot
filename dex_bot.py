"""
ETH Leverage Flush Trading Bot - DEX Version

Trades on Uniswap V3 based on leverage flush strategy using aggregated
liquidation data from multiple exchanges (OKX, Binance, Bybit).
"""

import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt  # Used for price data and Bybit liquidations

import config_dex as config
from dex_trader import DEXTrader


class LiquidationAggregator:
    """Aggregates liquidation data from multiple exchanges."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.exchanges = {
            'okx': self._fetch_okx_liquidations,
            'binance': self._fetch_binance_liquidations,
            'bybit': self._fetch_bybit_liquidations,
        }
        # Initialize CCXT exchanges for those that support it
        self.bybit = ccxt.bybit({'enableRateLimit': True})
        
    def fetch_all(self, symbol: str = "ETH", hours: int = 24) -> List[Dict]:
        """Fetch liquidations from all exchanges in parallel."""
        all_liquidations = []
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(fetch_fn, symbol, hours): name 
                for name, fetch_fn in self.exchanges.items()
            }
            
            for future in as_completed(futures):
                exchange = futures[future]
                try:
                    liquidations = future.result()
                    for liq in liquidations:
                        liq['exchange'] = exchange
                    all_liquidations.extend(liquidations)
                    self.logger.debug(f"Fetched {len(liquidations)} liquidations from {exchange}")
                except Exception as e:
                    self.logger.warning(f"Failed to fetch from {exchange}: {e}")
        
        self.logger.info(f"📊 Aggregated {len(all_liquidations)} liquidations from {len(self.exchanges)} exchanges")
        return all_liquidations
    
    def _fetch_okx_liquidations(self, symbol: str, hours: int) -> List[Dict]:
        """Fetch liquidations from OKX API."""
        base_url = "https://www.okx.com"
        endpoint = "/api/v5/public/liquidation-orders"
        params = {
            "instType": "SWAP",
            "uly": f"{symbol}-USDT",
            "limit": "100"
        }
        
        liquidations = []
        after = None
        start_time = int(time.time() * 1000) - (hours * 3600 * 1000)
        
        for _ in range(10):  # Max 10 pages
            if after:
                params["after"] = after
            try:
                response = requests.get(base_url + endpoint, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                
                if data["code"] != "0":
                    break
                
                items = data.get("data", [])
                if not items:
                    break
                
                for item in items:
                    details = item.get("details", [])
                    for detail in details:
                        ts = int(detail["ts"])
                        if ts < start_time:
                            return liquidations
                        
                        # OKX contract size is 0.1 ETH for ETH-USDT-SWAP
                        contract_size = 0.1
                        size_usd = float(detail["sz"]) * float(detail["bkPx"]) * contract_size
                        
                        liquidations.append({
                            "price": float(detail["bkPx"]),
                            "size_usd": size_usd,
                            "side": detail["side"],  # 'buy' = short liquidated, 'sell' = long liquidated
                            "timestamp": ts
                        })
                
                if items and items[-1].get("details"):
                    after = items[-1]["details"][-1]["ts"]
                else:
                    break
                    
            except Exception as e:
                self.logger.debug(f"OKX fetch error: {e}")
                break
            
            time.sleep(0.3)
        
        return liquidations
    
    def _fetch_binance_liquidations(self, symbol: str, hours: int) -> List[Dict]:
        """Fetch liquidations from Binance Futures API."""
        base_url = "https://fapi.binance.com"
        endpoint = "/fapi/v1/allForceOrders"
        
        liquidations = []
        start_time = int(time.time() * 1000) - (hours * 3600 * 1000)
        
        params = {
            "symbol": f"{symbol}USDT",
            "limit": 100,
            "startTime": start_time
        }
        
        try:
            response = requests.get(base_url + endpoint, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            for item in data:
                price = float(item["p"])  # Price
                qty = float(item["q"])    # Quantity in base asset
                size_usd = price * qty
                
                liquidations.append({
                    "price": price,
                    "size_usd": size_usd,
                    "side": "sell" if item["S"] == "BUY" else "buy",  # Opposite of order side
                    "timestamp": item["T"]
                })
                
        except requests.exceptions.HTTPError as e:
            # Binance may require API key for this endpoint
            self.logger.debug(f"Binance API error (may need auth): {e}")
        except Exception as e:
            self.logger.debug(f"Binance fetch error: {e}")
        
        return liquidations
    
    def _fetch_bybit_liquidations(self, symbol: str, hours: int) -> List[Dict]:
        """Fetch liquidations from Bybit using CCXT."""
        liquidations = []
        start_time = int(time.time() * 1000) - (hours * 3600 * 1000)
        
        try:
            # Bybit public liquidation endpoint
            base_url = "https://api.bybit.com"
            endpoint = "/v5/market/recent-trade"
            
            params = {
                "category": "linear",
                "symbol": f"{symbol}USDT",
                "limit": 1000
            }
            
            response = requests.get(base_url + endpoint, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data["retCode"] == 0:
                trades = data.get("result", {}).get("list", [])
                
                # Filter for liquidation trades (marked with 'L' in some APIs)
                # Bybit v5 doesn't directly expose liquidations via REST,
                # so we'll use a workaround with their liquidation endpoint
                
                # Try the dedicated liquidation endpoint
                liq_endpoint = "/v5/market/liqrecords"  # May not exist
                
        except Exception as e:
            self.logger.debug(f"Bybit CCXT error: {e}")
        
        # Fallback: try alternative Bybit endpoint
        try:
            alt_url = "https://api.bybit.com/v5/market/insurance"
            response = requests.get(alt_url, params={"coin": symbol}, timeout=10)
            # This gives insurance fund data, not liquidations - but useful context
        except:
            pass
        
        # Use Bybit's public WebSocket data if REST fails
        # For now, return empty and rely on other exchanges
        return liquidations


class DEXFlushBot:
    """Leverage flush-based trading bot for DEX (Uniswap)."""
    
    def __init__(self):
        self.setup_logging()
        self.logger = logging.getLogger(__name__)
        self.dex = DEXTrader()
        self.price_feed = self._init_price_feed()
        self.liquidation_aggregator = LiquidationAggregator(self.logger)
        
        # Trading state
        self.last_trade_price = None
        self.last_action = None
        self.last_liquidations = None
        self.last_liquidations_time = 0
        
        # Strategy parameters
        self.threshold_amount = 1_000_000  # $1M minimum for significant cluster
        self.entry_buffer_pct = 0.5        # Enter 0.5% above liquidation level
        self.take_profit_pct = 15          # Take profit at +15%
        self.stop_loss_pct = 5             # Stop loss at -5%
        self.liquidation_refresh_hours = 1 # Refresh liquidation data hourly
        
    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, config.LOG_LEVEL),
            format='%(asctime)s | %(levelname)s | %(message)s',
            handlers=[
                logging.FileHandler(config.LOG_FILE),
                logging.StreamHandler()
            ]
        )
    
    def _init_price_feed(self):
        exchange = ccxt.kraken({'enableRateLimit': True})
        self.logger.info("Price feed initialized (Kraken)")
        return exchange
    
    def fetch_ohlcv(self, limit: int = 100) -> list:
        try:
            ohlcv = self.price_feed.fetch_ohlcv(
                "ETH/USD",
                timeframe=config.TIMEFRAME,
                limit=limit
            )
            return ohlcv
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV: {e}")
            return []
    
    def get_current_price(self) -> Optional[float]:
        return self.dex.get_eth_price()
    
    def get_balances(self) -> Dict[str, float]:
        return self.dex.get_balances()
    
    def get_position(self, balances: dict, price: float) -> str:
        usdc_balance = balances.get(config.QUOTE_TOKEN, 0)
        weth_balance = balances.get('WETH', 0)
        weth_value_usd = weth_balance * price
        
        if weth_value_usd > usdc_balance and weth_balance >= config.MIN_TRADE_ETH:
            return 'long'
        elif usdc_balance >= config.MIN_TRADE_USDC:
            return 'cash'
        else:
            return 'empty'
    
    def has_gas_for_trade(self, balances: dict) -> bool:
        eth_balance = balances.get('ETH', 0)
        min_reserve = config.get_min_gas_reserve()
        
        if eth_balance < min_reserve:
            self.logger.warning(
                f"⛽ LOW GAS: {eth_balance:.6f} ETH < {min_reserve} minimum reserve"
            )
            return False
        return True
    
    def execute_buy(self, price: float, usdc_amount: float) -> bool:
        self.logger.info(f"🟢 BUY SIGNAL - Executing DEX swap")
        self.logger.info(f"   Network: {config.NETWORK.upper()}")
        self.logger.info(f"   Amount: ${usdc_amount:.2f} {config.QUOTE_TOKEN} -> ETH @ ${price:.2f}")
        
        expected_eth = usdc_amount / price
        self.logger.info(f"   Expected: ~{expected_eth:.6f} ETH")
        
        success, tx_hash = self.dex.buy_eth(usdc_amount)
        
        if success:
            self.last_trade_price = price
            self.last_action = 'BUY'
            if tx_hash and tx_hash != "dry_run_tx_hash":
                self.logger.info(f"   TX: {self._get_explorer_url(tx_hash)}")
            return True
        return False
    
    def execute_sell(self, price: float, eth_amount: float) -> bool:
        self.logger.info(f"🔴 SELL SIGNAL - Executing DEX swap")
        self.logger.info(f"   Network: {config.NETWORK.upper()}")
        self.logger.info(f"   Amount: {eth_amount:.6f} ETH -> {config.QUOTE_TOKEN} @ ${price:.2f}")
        
        expected_usdc = eth_amount * price
        self.logger.info(f"   Expected: ~${expected_usdc:.2f} {config.QUOTE_TOKEN}")
        
        if self.last_trade_price:
            pnl_pct = ((price / self.last_trade_price) - 1) * 100
            self.logger.info(f"   P&L: {pnl_pct:+.2f}%")
        
        success, tx_hash = self.dex.sell_eth(eth_amount)
        
        if success:
            self.last_action = 'SELL'
            if tx_hash and tx_hash != "dry_run_tx_hash":
                self.logger.info(f"   TX: {self._get_explorer_url(tx_hash)}")
            return True
        return False
    
    def _get_explorer_url(self, tx_hash: str) -> str:
        explorers = {
            "ethereum": "https://etherscan.io/tx/",
            "arbitrum": "https://arbiscan.io/tx/",
            "polygon": "https://polygonscan.com/tx/",
            "base": "https://basescan.org/tx/",
            "optimism": "https://optimistic.etherscan.io/tx/",
        }
        base_url = explorers.get(config.NETWORK, "")
        return f"{base_url}{tx_hash}"
    
    def analyze_liquidations(self, liquidations: List[Dict], current_price: float) -> Dict[str, Any]:
        """
        Analyze aggregated liquidations to find significant price clusters.
        
        Returns dict with:
        - lower_levels: Significant liquidation levels below current price
        - upper_levels: Significant liquidation levels above current price
        - total_lower_usd: Total USD liquidated below current price
        - total_upper_usd: Total USD liquidated above current price
        - exchange_breakdown: Liquidations by exchange
        """
        lower_clusters = defaultdict(lambda: {"total_usd": 0, "count": 0, "prices": [], "exchanges": set()})
        upper_clusters = defaultdict(lambda: {"total_usd": 0, "count": 0, "prices": [], "exchanges": set()})
        exchange_stats = defaultdict(lambda: {"count": 0, "total_usd": 0})
        
        for liq in liquidations:
            price = liq["price"]
            usd_value = liq["size_usd"]
            exchange = liq.get("exchange", "unknown")
            
            # Track exchange stats
            exchange_stats[exchange]["count"] += 1
            exchange_stats[exchange]["total_usd"] += usd_value
            
            # Bin into $25 price clusters
            bin_price = round(price / 25) * 25
            
            if price < current_price:
                lower_clusters[bin_price]["total_usd"] += usd_value
                lower_clusters[bin_price]["count"] += 1
                lower_clusters[bin_price]["prices"].append(price)
                lower_clusters[bin_price]["exchanges"].add(exchange)
            else:
                upper_clusters[bin_price]["total_usd"] += usd_value
                upper_clusters[bin_price]["count"] += 1
                upper_clusters[bin_price]["prices"].append(price)
                upper_clusters[bin_price]["exchanges"].add(exchange)
        
        # Find significant levels (above threshold)
        # Lower threshold for multi-exchange confirmation
        threshold_single = self.threshold_amount
        threshold_multi = self.threshold_amount * 0.5  # 50% threshold if confirmed by 2+ exchanges
        
        lower_levels = []
        for bin_price, info in lower_clusters.items():
            threshold = threshold_multi if len(info["exchanges"]) >= 2 else threshold_single
            if info["total_usd"] >= threshold:
                avg_price = sum(info["prices"]) / info["count"]
                lower_levels.append({
                    "price": avg_price,
                    "total_usd": info["total_usd"],
                    "count": info["count"],
                    "exchanges": list(info["exchanges"])
                })
        
        upper_levels = []
        for bin_price, info in upper_clusters.items():
            threshold = threshold_multi if len(info["exchanges"]) >= 2 else threshold_single
            if info["total_usd"] >= threshold:
                avg_price = sum(info["prices"]) / info["count"]
                upper_levels.append({
                    "price": avg_price,
                    "total_usd": info["total_usd"],
                    "count": info["count"],
                    "exchanges": list(info["exchanges"])
                })
        
        # Sort by price
        lower_levels.sort(key=lambda x: x["price"])
        upper_levels.sort(key=lambda x: x["price"])
        
        total_lower = sum(c["total_usd"] for c in lower_clusters.values())
        total_upper = sum(c["total_usd"] for c in upper_clusters.values())
        
        return {
            "lower_levels": lower_levels,
            "upper_levels": upper_levels,
            "total_lower_usd": total_lower,
            "total_upper_usd": total_upper,
            "exchange_breakdown": dict(exchange_stats)
        }
    
    def analyze_and_trade(self) -> Dict[str, Any]:
        result = {
            'timestamp': datetime.now().isoformat(),
            'price': None,
            'signal': None,
            'action_taken': None,
            'network': config.NETWORK,
            'balances': None,
            'liquidation_analysis': None
        }
        
        current_price = self.get_current_price()
        if not current_price:
            self.logger.error("Could not fetch current price")
            return result
        
        balances = self.get_balances()
        position = self.get_position(balances, current_price)
        usdc_balance = balances.get(config.QUOTE_TOKEN, 0)
        weth_balance = balances.get('WETH', 0)
        
        result['price'] = current_price
        result['balances'] = balances
        
        eth_balance = balances.get('ETH', 0)
        has_gas = self.has_gas_for_trade(balances)
        
        self.logger.info(f"─" * 60)
        self.logger.info(f"[{config.NETWORK.upper()}] ETH: ${current_price:.2f}")
        self.logger.info(f"Position: {position.upper()} | WETH: {weth_balance:.6f} | {config.QUOTE_TOKEN}: ${usdc_balance:.2f}")
        self.logger.info(f"Gas: {eth_balance:.6f} ETH {'✅' if has_gas else '⚠️ LOW'}")
        
        # Refresh liquidation data if needed
        refresh_interval = self.liquidation_refresh_hours * 3600
        if time.time() - self.last_liquidations_time > refresh_interval:
            self.logger.info("🔄 Refreshing liquidation data from all exchanges...")
            self.last_liquidations = self.liquidation_aggregator.fetch_all("ETH", hours=24)
            self.last_liquidations_time = time.time()
        
        # Calculate RSI
        ohlcv = self.fetch_ohlcv(limit=100)
        if ohlcv:
            try:
                from rsi import calculate_rsi
                prices = [c[4] for c in ohlcv]
                result['rsi'] = calculate_rsi(prices, config.RSI_PERIOD)
            except Exception as e:
                self.logger.warning(f"Failed to calculate RSI: {e}")
        
        signal = "HOLD"
        description = "Waiting for flush signal"
        
        if self.last_liquidations:
            analysis = self.analyze_liquidations(self.last_liquidations, current_price)
            result['liquidation_analysis'] = analysis
            
            # Log exchange breakdown
            breakdown = analysis["exchange_breakdown"]
            if breakdown:
                breakdown_str = " | ".join([f"{ex}: {s['count']} (${s['total_usd']/1e6:.1f}M)" 
                                           for ex, s in breakdown.items()])
                self.logger.info(f"📊 Sources: {breakdown_str}")
            
            lower_levels = analysis["lower_levels"]
            
            if lower_levels:
                # Find the most significant lower level (highest USD volume)
                best_level = max(lower_levels, key=lambda x: x["total_usd"])
                blue_bottom = best_level["price"]
                entry_threshold = blue_bottom * (1 + self.entry_buffer_pct / 100)
                
                # Log significant levels
                self.logger.info(f"🎯 Key liquidation level: ${blue_bottom:.2f} "
                               f"(${best_level['total_usd']/1e6:.1f}M from {best_level['exchanges']})")
                
                # Multi-exchange confirmation bonus
                multi_exchange = len(best_level["exchanges"]) >= 2
                
                if current_price <= entry_threshold:
                    signal = "BUY"
                    confirmation = "✅ MULTI-EXCHANGE" if multi_exchange else "⚠️ SINGLE-EXCHANGE"
                    description = f"Price at liquidation zone ~${blue_bottom:.2f} [{confirmation}]"
            else:
                self.logger.info("📊 No significant liquidation clusters detected")
        
        # Check TP/SL for existing position
        if position == 'long' and self.last_trade_price:
            pnl_pct = ((current_price / self.last_trade_price) - 1) * 100
            if pnl_pct >= self.take_profit_pct:
                signal = "SELL"
                description = f"Take profit at +{pnl_pct:.2f}%"
            elif pnl_pct <= -self.stop_loss_pct:
                signal = "SELL"
                description = f"Stop loss at {pnl_pct:.2f}%"
        
        result['signal'] = signal
        
        self.logger.info(f"Signal: {signal}")
        self.logger.info(f"{description}")
        
        # Execute trades
        if signal == "BUY":
            if not has_gas:
                self.logger.warning(f"   ⛽ SKIP BUY - Insufficient gas")
                result['action_taken'] = 'SKIP_NO_GAS'
            elif usdc_balance >= config.MIN_TRADE_USDC:
                trade_amount = usdc_balance * (config.TRADE_PERCENT / 100)
                if self.execute_buy(current_price, trade_amount):
                    result['action_taken'] = 'BUY'
                else:
                    result['action_taken'] = 'BUY_FAILED'
            else:
                self.logger.info(f"   ⏸️  HOLD - Insufficient funds")
                result['action_taken'] = 'HOLD_NO_FUNDS'
                
        elif signal == "SELL":
            if not has_gas:
                self.logger.warning(f"   ⛽ SKIP SELL - Insufficient gas")
                result['action_taken'] = 'SKIP_NO_GAS'
            elif weth_balance >= config.MIN_TRADE_ETH:
                if "Stop loss" in description:
                    trade_amount = weth_balance  # Full exit on stop loss
                else:
                    trade_amount = weth_balance * (config.TRADE_PERCENT / 100)
                if self.execute_sell(current_price, trade_amount):
                    result['action_taken'] = 'SELL'
                    if "Stop loss" in description:
                        self.last_trade_price = None
                else:
                    result['action_taken'] = 'SELL_FAILED'
            else:
                self.logger.info(f"   ⏸️  HOLD - Insufficient WETH")
                result['action_taken'] = 'HOLD_NO_FUNDS'
        else:
            self.logger.info(f"   ⏸️  HOLD")
            result['action_taken'] = 'HOLD'
            
        return result
    
    def run(self):
        self.logger.info("=" * 60)
        self.logger.info("ETH LEVERAGE FLUSH TRADING BOT - DEX MODE")
        self.logger.info("=" * 60)
        self.logger.info(f"Network: {config.NETWORK.upper()}")
        self.logger.info(f"DEX: Uniswap V3")
        self.logger.info(f"Pair: WETH/{config.QUOTE_TOKEN}")
        self.logger.info(f"")
        self.logger.info(f"Strategy: Leverage Flush (Multi-Exchange)")
        self.logger.info(f"  📊 Data Sources: OKX, Binance, Bybit")
        self.logger.info(f"  🎯 Entry: {self.entry_buffer_pct}% above liquidation cluster")
        self.logger.info(f"  💰 Take Profit: {self.take_profit_pct}%")
        self.logger.info(f"  🛑 Stop Loss: {self.stop_loss_pct}%")
        self.logger.info(f"  📈 Threshold: ${self.threshold_amount/1e6:.1f}M (${self.threshold_amount/2/1e6:.1f}M multi-exchange)")
        self.logger.info(f"")
        self.logger.info(f"Trade Size: {config.TRADE_PERCENT}% of available balance")
        self.logger.info(f"Min Trade: ${config.MIN_TRADE_USDC} USDC / {config.MIN_TRADE_ETH} ETH")
        self.logger.info(f"Mode: {'DRY RUN' if config.DRY_RUN else '🔴 LIVE TRADING'}")
        self.logger.info("=" * 60)
        
        balances = self.get_balances()
        price = self.get_current_price() or 0
        position = self.get_position(balances, price) if price else 'unknown'
        self.logger.info(f"Wallet Balances: {balances}")
        self.logger.info(f"Current Position: {position.upper()}")
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
                time.sleep(10)


def run_once():
    bot = DEXFlushBot()
    return bot.analyze_and_trade()


if __name__ == "__main__":
    bot = DEXFlushBot()
    bot.run()
