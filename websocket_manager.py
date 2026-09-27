import asyncio
import json
import logging
import time
from typing import Dict, List, Any, Callable, Optional
import websockets
from dataclasses import dataclass

@dataclass
class LiquidationEvent:
    exchange: str
    symbol: str
    price: float
    size_usd: float
    side: str  # 'buy' or 'sell' (liquidated side)
    timestamp: float

class WebSocketManager:
    """
    Manages WebSocket connections to multiple exchanges for real-time data.
    Aggregates liquidation events and updates internal state.
    """
    def __init__(self, logger: logging.Logger, status_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.logger = logger
        self.status_callback = status_callback
        self.running = False
        self.callbacks: List[Callable[[LiquidationEvent], None]] = []
        self.latest_prices: Dict[str, float] = {}
        self.connections = {}

    def _emit_status(self, stream: str, status: str, **extra):
        """Emit non-sensitive connection telemetry to the owning bot."""
        if not self.status_callback:
            return
        payload = {"status": status, "updated_at": time.time(), **extra}
        try:
            self.status_callback(stream, payload)
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error in WebSocket status callback: {e}")
        
    async def start(self):
        """Start all WebSocket connections."""
        self.running = True
        self.logger.info("🚀 Starting WebSocket Manager...")
        
        # Start connection tasks
        await asyncio.gather(*(stream() for stream in self._market_streams()))

    def _market_streams(self):
        """Return enabled market data stream coroutines."""
        return [
            self._connect_binance,
            self._connect_hyperliquid,
            self._connect_okx,
        ]

    def register_callback(self, callback: Callable[[LiquidationEvent], None]):
        """Register a function to be called on new liquidation events."""
        self.callbacks.append(callback)

    async def _handle_liquidation(self, event: LiquidationEvent):
        """Distribute liquidation event to callbacks."""
        for callback in self.callbacks:
            try:
                callback(event)
            except Exception as e:
                self.logger.error(f"Error in liquidation callback: {e}")

    async def _connect_binance(self):
        """Connect to Binance Futures WebSocket for liquidations."""
        url = "wss://fstream.binance.com/ws/ethusdt@forceOrder"
        self.logger.info(f"Connecting to Binance: {url}")
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    self.logger.info("✅ Connected to Binance WebSocket")
                    self._emit_status("binance", "connected", url=url)
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._emit_status("binance", "message", url=url)
                            data = json.loads(msg)
                            
                            # Defensive check for message type
                            if not isinstance(data, dict):
                                continue
                                
                            order = data.get('o', {})
                            if order.get('X') == 'FILLED' and 'T' in data:
                                side = 'long' if order.get('S') == 'SELL' else 'short'
                                try:
                                    price = float(order.get('p', 0))
                                    quantity = float(order.get('q', 0))
                                    amount_usd = price * quantity
                                    
                                    if amount_usd > 0:
                                        event = LiquidationEvent(
                                            exchange='binance',
                                            symbol='ETH',
                                            price=price,
                                            size_usd=amount_usd,
                                            side=side,
                                            timestamp=data['T'] / 1000
                                        )
                                        await self._handle_liquidation(event)
                                except (ValueError, TypeError) as e:
                                    self.logger.error(f"Error parsing Binance liquidation: {e}")
                        except asyncio.TimeoutError:
                            await ws.ping()
            except Exception as e:
                self._emit_status("binance", "error", error=str(e))
                self.logger.error(f"Binance WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _connect_hyperliquid(self):
        """Connect to Hyperliquid WebSocket for L2 Book data."""
        url = "wss://api.hyperliquid.xyz/ws"
        subscribe_msg = {
            "method": "subscribe",
            "subscription": {
                "type": "l2Book",
                "coin": "ETH"
            }
        }
        self.logger.info(f"Connecting to Hyperliquid: {url}")
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    self.logger.info("✅ Connected to Hyperliquid WebSocket")
                    await ws.send(json.dumps(subscribe_msg))
                    self._emit_status("hyperliquid", "subscribed", url=url, channel="l2Book")
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._emit_status("hyperliquid", "message", url=url, channel="l2Book")
                            data = json.loads(msg)
                            if data.get('channel') == 'l2Book':
                                levels = data.get('data', {}).get('levels', [])
                                if levels:
                                    bids = levels[0]
                                    asks = levels[1]
                                    if bids and asks:
                                        best_bid = float(bids[0]['px'])
                                        best_ask = float(asks[0]['px'])
                                        self.latest_prices['ETH'] = (best_bid + best_ask) / 2
                        except asyncio.TimeoutError:
                             await ws.ping() # Keepalive
            except Exception as e:
                self._emit_status("hyperliquid", "error", error=str(e))
                self.logger.error(f"Hyperliquid WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def _parse_okx_liquidation_message(self, data: Dict[str, Any]) -> List[LiquidationEvent]:
        """Parse OKX liquidation-orders messages into normalized ETH events."""
        if data.get("event"):
            return []
        arg = data.get("arg", {})
        if arg.get("channel") != "liquidation-orders":
            return []

        events: List[LiquidationEvent] = []
        for item in data.get("data", []):
            inst_id = item.get("instId", "")
            if not inst_id.startswith("ETH-") or not inst_id.endswith("-SWAP"):
                continue

            for detail in item.get("details", []):
                try:
                    price = float(detail.get("bkPx", 0))
                    contracts = float(detail.get("sz", 0))
                    timestamp_ms = int(detail.get("ts", 0))
                    if price <= 0 or contracts <= 0 or timestamp_ms <= 0:
                        continue

                    # OKX ETH swaps are 0.1 ETH per contract; keep the bot's
                    # existing liquidation shape compatible with analyze_liquidations().
                    size_usd = contracts * price * 0.1
                    side = "long" if detail.get("side") == "sell" else "short"
                    events.append(LiquidationEvent(
                        exchange="okx",
                        symbol="ETH",
                        price=price,
                        size_usd=size_usd,
                        side=side,
                        timestamp=timestamp_ms / 1000,
                    ))
                except (TypeError, ValueError) as e:
                    self.logger.error(f"Error parsing OKX liquidation detail: {e}")
        return events

    async def _connect_okx(self):
        """Connect to OKX public WebSocket for ETH swap liquidations."""
        url = "wss://ws.okx.com:8443/ws/v5/public"
        subscribe_msg = {
            "op": "subscribe",
            "args": [{"channel": "liquidation-orders", "instType": "SWAP"}],
        }
        self.logger.info(f"Connecting to OKX: {url}")
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    self.logger.info("✅ Connected to OKX WebSocket")
                    await ws.send(json.dumps(subscribe_msg))
                    self._emit_status("okx", "subscribed", url=url, channel="liquidation-orders")
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._emit_status("okx", "message", url=url, channel="liquidation-orders")
                            data = json.loads(msg)
                            for event in self._parse_okx_liquidation_message(data):
                                await self._handle_liquidation(event)
                        except asyncio.TimeoutError:
                            await ws.ping()
            except Exception as e:
                self._emit_status("okx", "error", error=str(e))
                self.logger.error(f"OKX WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def get_price(self, symbol: str) -> Optional[float]:
        """Get the latest price from WebSocket stream."""
        return self.latest_prices.get(symbol)
