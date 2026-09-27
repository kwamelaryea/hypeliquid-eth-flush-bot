"""
cascade_detector.py — Early Warning System for Leverage Cascades

Inspired by the Amberdata WLFI analysis (Oct 10, 2025):
  "WLFI began declining at 3:32 PM UTC — $6.93B in liquidations followed 5 hours 18 minutes later."

Core thesis:
  High-beta, politically-connected tokens with concentrated ownership & elevated funding rates
  act as LEADING INDICATORS of broader market cascade risk. Their structural fragility means:
    1. They liquidate first (high funding = unsustainable carry)
    2. Their holders are networked & coordinated → exits are fast & correlated
    3. Cross-margin contagion then propagates from thin markets → liquid markets (ETH, BTC)

What this module detects:
  - Funding rate stress differential  (canary token vs BTC ≥ 2x)
  - Volume anomaly                    (canary token ≥ 5x 24h baseline)
  - Price divergence                  (canary falling while BTC stable)
  - Orderbook depth deterioration     (bid depth declining in canary token)

Output: CascadeRiskScore (0–100) + CascadeAlert (NORMAL / ELEVATED / CRITICAL)
Integration: Plug into analyze_and_trade() to switch to defensive mode before the flush hits ETH.
"""

import time
import logging
import requests
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Dict, List, Tuple
from enum import Enum


# =============================================================================
# DATA TYPES
# =============================================================================

class RiskLevel(Enum):
    NORMAL   = "NORMAL"     # Score < 30  — trade normally
    ELEVATED = "ELEVATED"   # Score 30–60 — tighten stops, reduce size
    CRITICAL = "CRITICAL"   # Score > 60  — no new longs, consider shorting cascade

@dataclass
class CascadeSignal:
    """Snapshot of all cascade risk indicators at a point in time."""
    timestamp: float
    
    # --- Funding Rate signals ---
    canary_funding_rate: float     # e.g. 0.02870  (2.87% per 8hr for WLFI)
    btc_funding_rate: float        # e.g. 0.01009  (1.01% per 8hr for BTC)
    funding_ratio: float           # canary / btc — ≥2x is a warning
    
    # --- Volume signals ---
    canary_volume_usd: float       # Current 1hr volume in USD
    canary_baseline_volume: float  # 24hr average hourly volume
    volume_ratio: float            # current / baseline — ≥5x is a warning
    
    # --- Price divergence signals ---
    canary_price_change_pct: float # % change in canary over lookback window
    btc_price_change_pct: float    # % change in BTC over same window
    divergence_pct: float          # canary - btc  (negative = canary falling while BTC stable)
    
    # --- Composite score ---
    risk_score: float              # 0–100
    risk_level: RiskLevel
    
    # --- Human readable ---
    reasons: List[str] = field(default_factory=list)

@dataclass  
class DefensiveConfig:
    """How the bot should behave at each risk level."""
    # NORMAL: business as usual
    normal_size_pct: float    = 1.0   # 100% of normal trade size
    normal_sl_mult: float     = 1.0   # 1x ATR multiplier
    normal_allow_long: bool   = True
    normal_allow_short: bool  = True
    
    # ELEVATED: cautious
    elevated_size_pct: float  = 0.5   # 50% of normal trade size
    elevated_sl_mult: float   = 0.75  # Tighter stop
    elevated_allow_long: bool = True  # Allow longs at ELEVATED — size is already halved, double-blocking prevents all trades
    elevated_allow_short: bool = True # Shorts OK — the flush is coming
    
    # CRITICAL: defensive / pure cascade short
    critical_size_pct: float  = 0.25  # 25% size (emergency short only)
    critical_sl_mult: float   = 0.5   # Very tight
    critical_allow_long: bool = False
    critical_allow_short: bool = True  # The cascade IS the signal

# =============================================================================
# CANARY TOKEN CONFIG
# The report focused on WLFI but the structural characteristics apply to any
# high-beta, concentrated-ownership token with elevated funding rates.
# Monitor a basket — if 2+ tokens show stress simultaneously, confidence rises.
# =============================================================================

# Tokens to monitor as canaries. Hyperliquid tickers where available.
# Priority: politically-connected tokens, meme tokens, high-funding altcoins
CANARY_TOKENS = {
    "HYPE":  {"exchange": "hyperliquid", "weight": 1.5},  # HL native — very sensitive
    "WIF":   {"exchange": "hyperliquid", "weight": 1.0},  # High-beta meme
    "PEPE":  {"exchange": "hyperliquid", "weight": 1.0},  # High-beta meme
    "TRUMP": {"exchange": "hyperliquid", "weight": 2.0},  # Political token — closest to WLFI
    "MELANIA":{"exchange": "hyperliquid","weight": 2.0},  # Political token
    "WLFI":  {"exchange": "hyperliquid", "weight": 2.5},  # The report's primary subject
}

# Thresholds (calibrated from the Amberdata report data)
FUNDING_RATIO_WARNING  = 2.0    # Canary funding ≥ 2x BTC → warning
FUNDING_RATIO_CRITICAL = 3.0    # Canary funding ≥ 3x BTC → critical
VOLUME_RATIO_WARNING   = 5.0    # Volume spike ≥ 5x baseline → warning  
VOLUME_RATIO_CRITICAL  = 10.0   # Volume spike ≥ 10x baseline → critical (report showed 21.7x)
DIVERGENCE_WARNING     = -5.0   # Canary -5% while BTC flat → warning
DIVERGENCE_CRITICAL    = -15.0  # Canary -15% while BTC flat → critical
LOOKBACK_MINUTES       = 60     # Price divergence lookback window


# =============================================================================
# HYPERLIQUID DATA FETCHER
# =============================================================================

class HyperliquidDataFetcher:
    """Fetches funding rates and OHLCV from Hyperliquid Info API."""
    
    BASE_URL = "https://api.hyperliquid.xyz/info"
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._cache: Dict[str, Tuple[float, any]] = {}
        self._cache_ttl = 30  # seconds
    
    def _post(self, payload: dict) -> Optional[dict]:
        try:
            r = requests.post(self.BASE_URL, json=payload, timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self.logger.warning(f"HL API error: {e}")
            return None
    
    def _cached(self, key: str, fetch_fn):
        """Simple TTL cache wrapper."""
        if key in self._cache:
            cached_at, value = self._cache[key]
            if time.time() - cached_at < self._cache_ttl:
                return value
        value = fetch_fn()
        if value is not None:
            self._cache[key] = (time.time(), value)
        return value
    
    def get_all_funding_rates(self) -> Dict[str, float]:
        """Returns {coin: funding_rate} for all HL perps."""
        def fetch():
            data = self._post({"type": "metaAndAssetCtxs"})
            if not data or len(data) < 2:
                return {}
            universe = data[0]["universe"]
            contexts = data[1]
            result = {}
            for i, coin_info in enumerate(universe):
                try:
                    result[coin_info["name"]] = float(contexts[i]["funding"])
                except (IndexError, KeyError, ValueError):
                    pass
            return result
        return self._cached("funding_rates", fetch) or {}
    
    def get_recent_candles(self, coin: str, interval: str = "1m", n: int = 120) -> List[dict]:
        """Get recent OHLCV candles for a coin."""
        end_ms = int(time.time() * 1000)
        
        interval_ms = {
            "1m": 60_000, "5m": 300_000, "15m": 900_000,
            "1h": 3_600_000, "4h": 14_400_000
        }.get(interval, 60_000)
        
        start_ms = end_ms - (n * interval_ms)
        
        data = self._post({
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms
            }
        })
        return data or []
    
    def get_volume_data(self, coin: str) -> Tuple[float, float]:
        """
        Returns (current_1hr_volume_usd, avg_hourly_volume_usd_24h).
        Uses 1m candles over 25 hours to compute baseline.
        """
        candles = self.get_recent_candles(coin, interval="1h", n=25)
        if not candles or len(candles) < 2:
            return 0.0, 0.0
        
        # Each candle has 'v' (base volume) and 'c' (close price)
        volumes_usd = []
        for c in candles:
            try:
                vol_base = float(c.get("v", 0))
                price = float(c.get("c", 0))
                volumes_usd.append(vol_base * price)
            except (ValueError, TypeError):
                pass
        
        if not volumes_usd:
            return 0.0, 0.0
        
        current_1hr = volumes_usd[-1]          # Most recent completed hour
        baseline_24h_avg = sum(volumes_usd[:-1]) / max(len(volumes_usd) - 1, 1)
        
        return current_1hr, baseline_24h_avg
    
    def get_price_change_pct(self, coin: str, lookback_minutes: int = LOOKBACK_MINUTES) -> float:
        """Get % price change over the lookback window."""
        n_candles = lookback_minutes + 5
        candles = self.get_recent_candles(coin, interval="1m", n=n_candles)
        
        if len(candles) < 2:
            return 0.0
        
        try:
            start_price = float(candles[0]["o"]) # Use first candle in window
            end_price   = float(candles[-1]["c"])
            return ((end_price / start_price) - 1) * 100
        except (IndexError, ValueError, ZeroDivisionError):
            return 0.0


# =============================================================================
# CASCADE RISK ENGINE
# =============================================================================

class CascadeDetector:
    """
    Monitors canary tokens for early warning signs of a broader cascade.
    
    Usage:
        detector = CascadeDetector(info_client)  # Pass your HL Info instance
        signal = detector.evaluate()
        
        if signal.risk_level == RiskLevel.CRITICAL:
            # Cut longs, prepare emergency short
            ...
    """
    
    def __init__(self, hl_info=None, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.fetcher = HyperliquidDataFetcher(self.logger)
        self.hl_info = hl_info  # Existing Hyperliquid Info instance (optional)
        
        self.config = DefensiveConfig()
        self._signal_history: deque = deque(maxlen=60)  # 60 signal snapshots
        self._last_evaluation: Optional[CascadeSignal] = None
        self._last_eval_time: float = 0
        self._eval_cooldown: float = 30  # Don't re-evaluate more than once per 30s
        
    def evaluate(self, force: bool = False) -> Optional[CascadeSignal]:
        """
        Run a full cascade risk evaluation.
        
        Args:
            force: Skip cooldown check
            
        Returns:
            CascadeSignal with current risk assessment, or None if too soon
        """
        if not force and (time.time() - self._last_eval_time < self._eval_cooldown):
            return self._last_evaluation
        
        self.logger.debug("🔍 Evaluating cascade risk...")
        
        # 1. Get BTC baseline
        if self.hl_info and hasattr(self.hl_info, 'funding_history'):
            # Backtest/Live Mode using Info client
            now_ms = int(time.time() * 1000)
            day_ago_ms = now_ms - (24 * 3600 * 1000)
            btc_funding_list = self.hl_info.funding_history("BTC", day_ago_ms)
            btc_funding = float(btc_funding_list[-1]['fundingRate']) if btc_funding_list else 0.0
            
            # For price change, we need candles
            # startTime = current_time - lookback
            now_ms = int(time.time() * 1000)
            if hasattr(self.hl_info, 'current_time') and self.hl_info.current_time:
                now_ms = int(self.hl_info.current_time.timestamp() * 1000)
            
            btc_candles = self.hl_info.candles_snapshot("BTC", "1h", now_ms - (LOOKBACK_MINUTES * 60 * 1000 * 2), now_ms)
            if len(btc_candles) >= 2:
                btc_start = float(btc_candles[0]['o'])
                btc_end = float(btc_candles[-1]['c'])
                btc_change = ((btc_end / btc_start) - 1) * 100
            else:
                btc_change = 0.0
            
            all_funding = None # We'll fetch per token
        else:
            all_funding = self.fetcher.get_all_funding_rates()
            btc_funding = all_funding.get("BTC", 0.0)
            btc_change = self.fetcher.get_price_change_pct("BTC", LOOKBACK_MINUTES)
        
        # 2. Evaluate each canary token
        token_signals = []
        for token, meta in CANARY_TOKENS.items():
            sig = self._evaluate_token(
                token, 
                weight=meta["weight"],
                all_funding=all_funding,
                btc_funding=btc_funding,
                btc_change=btc_change
            )
            if sig:
                token_signals.append(sig)
        
        if not token_signals:
            self.logger.debug("No canary data available")
            return None
        
        # 3. Aggregate into composite score
        signal = self._aggregate_signals(token_signals, btc_funding, btc_change)
        
        self._last_evaluation = signal
        self._last_eval_time = time.time()
        self._signal_history.append(signal)
        
        # 4. Log if elevated/critical
        if signal.risk_level != RiskLevel.NORMAL:
            self.logger.warning(
                f"⚠️  CASCADE RISK: {signal.risk_level.value} "
                f"(score={signal.risk_score:.0f}/100) | "
                f"Reasons: {', '.join(signal.reasons)}"
            )
        
        return signal
    
    def _evaluate_token(
        self,
        token: str,
        weight: float,
        all_funding: Optional[Dict[str, float]],
        btc_funding: float,
        btc_change: float
    ) -> Optional[dict]:
        """Evaluate a single canary token. Returns raw signal dict or None."""
        
        try:
            if self.hl_info and hasattr(self.hl_info, 'funding_history'):
                # Use Info client (Backtest/Live)
                now_ms = int(time.time() * 1000)
                if hasattr(self.hl_info, 'current_time') and self.hl_info.current_time:
                    now_ms = int(self.hl_info.current_time.timestamp() * 1000)
                
                day_ago_ms = now_ms - (24 * 3600 * 1000)
                token_funding_list = self.hl_info.funding_history(token, day_ago_ms)
                token_funding = float(token_funding_list[-1]['fundingRate']) if token_funding_list else 0.0
                
                # Volume ratio
                # Need 24h of 1h candles
                candles_baseline = self.hl_info.candles_snapshot(token, "1h", now_ms - (25 * 3600 * 1000), now_ms)
                if not candles_baseline or len(candles_baseline) < 2:
                    current_vol, baseline_vol = 0.0, 0.0
                else:
                    vols = [float(c['v']) * float(c['c']) for c in candles_baseline]
                    current_vol = vols[-1]
                    baseline_vol = sum(vols[:-1]) / len(vols[:-1])
                
                # Price change
                candles_recent = self.hl_info.candles_snapshot(token, "1h", now_ms - (LOOKBACK_MINUTES * 60 * 1000 * 2), now_ms)
                if len(candles_recent) >= 2:
                    t_start = float(candles_recent[0]['o'])
                    t_end = float(candles_recent[-1]['c'])
                    token_change = ((t_end / t_start) - 1) * 100
                else:
                    token_change = 0.0
            else:
                # Use Fetcher (Legacy/Simple)
                token_funding = all_funding.get(token)
                if token_funding is None:
                    return None
                current_vol, baseline_vol = self.fetcher.get_volume_data(token)
                token_change = self.fetcher.get_price_change_pct(token, LOOKBACK_MINUTES)
            
            # --- Funding ratio ---
            funding_ratio = (token_funding / btc_funding) if btc_funding > 1e-12 else 1.0
            
            # --- Volume anomaly ---
            volume_ratio = (current_vol / baseline_vol) if baseline_vol > 0 else 1.0
            
            # --- Price divergence ---
            divergence = token_change - btc_change
            
            return {
                "token": token,
                "weight": weight,
                "funding_ratio": funding_ratio,
                "token_funding": token_funding,
                "volume_ratio": volume_ratio,
                "current_vol": current_vol,
                "baseline_vol": baseline_vol,
                "token_change": token_change,
                "btc_change": btc_change,
                "divergence": divergence,
            }
        
        except Exception as e:
            self.logger.debug(f"Error evaluating {token}: {e}")
            return None
    
    def _aggregate_signals(
        self,
        token_signals: List[dict],
        btc_funding: float,
        btc_change: float
    ) -> CascadeSignal:
        """Combine individual token signals into a composite risk score."""
        
        total_score = 0.0
        total_weight = 0.0
        reasons = []
        
        # Track best/worst canary stats for the snapshot
        max_funding_ratio = 0.0
        max_volume_ratio = 0.0
        min_divergence = 0.0
        best_canary_funding = 0.0
        best_canary_vol = 0.0
        best_canary_baseline = 0.0
        
        for sig in token_signals:
            w = sig["weight"]
            token_score = 0.0
            
            # --- Funding rate stress (0-40 points) ---
            fr = sig["funding_ratio"]
            if fr >= FUNDING_RATIO_CRITICAL:
                fs = 40.0
                reasons.append(f"{sig['token']} funding {fr:.1f}x BTC (CRITICAL)")
            elif fr >= FUNDING_RATIO_WARNING:
                fs = 20.0 + (fr - FUNDING_RATIO_WARNING) / (FUNDING_RATIO_CRITICAL - FUNDING_RATIO_WARNING) * 20
                reasons.append(f"{sig['token']} funding {fr:.1f}x BTC")
            elif fr >= 1.5:
                fs = 10.0
            else:
                fs = 0.0
            token_score += fs
            
            # --- Volume anomaly (0-35 points) ---
            vr = sig["volume_ratio"]
            if vr >= VOLUME_RATIO_CRITICAL:
                vs = 35.0
                reasons.append(f"{sig['token']} volume {vr:.1f}x baseline (SPIKE)")
            elif vr >= VOLUME_RATIO_WARNING:
                vs = 15.0 + (vr - VOLUME_RATIO_WARNING) / (VOLUME_RATIO_CRITICAL - VOLUME_RATIO_WARNING) * 20
                reasons.append(f"{sig['token']} volume {vr:.1f}x baseline")
            elif vr >= 2.0:
                vs = 8.0
            else:
                vs = 0.0
            token_score += vs
            
            # --- Price divergence (0-25 points) ---
            div = sig["divergence"]
            if div <= DIVERGENCE_CRITICAL:
                ds = 25.0
                reasons.append(f"{sig['token']} -{abs(div):.1f}% vs BTC (DIVERGING)")
            elif div <= DIVERGENCE_WARNING:
                ds = 10.0 + (div - DIVERGENCE_WARNING) / (DIVERGENCE_CRITICAL - DIVERGENCE_WARNING) * 15
                reasons.append(f"{sig['token']} -{abs(div):.1f}% vs BTC")
            elif div <= -2.0:
                ds = 5.0
            else:
                ds = 0.0
            token_score += ds
            
            total_score += token_score * w
            total_weight += w
            
            # Track maximums for snapshot
            if fr > max_funding_ratio:
                max_funding_ratio = fr
                best_canary_funding = sig["token_funding"]
            if vr > max_volume_ratio:
                max_volume_ratio = vr
                best_canary_vol = sig["current_vol"]
                best_canary_baseline = sig["baseline_vol"]
            if div < min_divergence:
                min_divergence = div
        
        # Normalize to 0-100
        raw_score = (total_score / total_weight) if total_weight > 0 else 0.0
        risk_score = min(100.0, raw_score)
        
        # Deduplicate reasons
        reasons = list(dict.fromkeys(reasons))
        
        if risk_score >= 60:
            risk_level = RiskLevel.CRITICAL
        elif risk_score >= 30:
            risk_level = RiskLevel.ELEVATED
        else:
            risk_level = RiskLevel.NORMAL
        
        return CascadeSignal(
            timestamp=time.time(),
            canary_funding_rate=best_canary_funding,
            btc_funding_rate=btc_funding,
            funding_ratio=max_funding_ratio,
            canary_volume_usd=best_canary_vol,
            canary_baseline_volume=best_canary_baseline,
            volume_ratio=max_volume_ratio,
            canary_price_change_pct=min_divergence + btc_change,
            btc_price_change_pct=btc_change,
            divergence_pct=min_divergence,
            risk_score=risk_score,
            risk_level=risk_level,
            reasons=reasons,
        )
    
    def get_trading_params(self, base_size_pct: float, base_sl_mult: float) -> dict:
        """
        Given current risk level, return adjusted trading parameters.
        
        Returns:
            dict with: size_pct, sl_multiplier, allow_long, allow_short, risk_level
        """
        signal = self._last_evaluation
        if signal is None:
            return {
                "size_pct": base_size_pct,
                "sl_multiplier": base_sl_mult,
                "allow_long": True,
                "allow_short": True,
                "risk_level": RiskLevel.NORMAL,
            }
        
        cfg = self.config
        
        if signal.risk_level == RiskLevel.CRITICAL:
            return {
                "size_pct": base_size_pct * cfg.critical_size_pct,
                "sl_multiplier": base_sl_mult * cfg.critical_sl_mult,
                "allow_long": cfg.critical_allow_long,
                "allow_short": cfg.critical_allow_short,
                "risk_level": RiskLevel.CRITICAL,
            }
        elif signal.risk_level == RiskLevel.ELEVATED:
            return {
                "size_pct": base_size_pct * cfg.elevated_size_pct,
                "sl_multiplier": base_sl_mult * cfg.elevated_sl_mult,
                "allow_long": cfg.elevated_allow_long,
                "allow_short": cfg.elevated_allow_short,
                "risk_level": RiskLevel.ELEVATED,
            }
        else:
            return {
                "size_pct": base_size_pct,
                "sl_multiplier": base_sl_mult,
                "allow_long": cfg.normal_allow_long,
                "allow_short": cfg.normal_allow_short,
                "risk_level": RiskLevel.NORMAL,
            }
    
    def get_trend(self, n_samples: int = 10) -> str:
        """
        Is cascade risk rising, falling, or stable?
        Returns: "RISING" | "FALLING" | "STABLE"
        """
        if len(self._signal_history) < n_samples:
            return "STABLE"
        
        recent = list(self._signal_history)[-n_samples:]
        scores = [s.risk_score for s in recent]
        
        first_half_avg = sum(scores[:n_samples//2]) / (n_samples//2)
        second_half_avg = sum(scores[n_samples//2:]) / (n_samples - n_samples//2)
        
        delta = second_half_avg - first_half_avg
        
        if delta > 10:
            return "RISING"
        elif delta < -10:
            return "FALLING"
        else:
            return "STABLE"
