"""
macro_regime.py — FRED-based macro regime classifier for HL Flush Bot.

Fetches VIX, 10Y-2Y yield spread, and HY credit spread from the St Louis Fed
FRED API (free, no rate limit for personal use).

Regime:
  RISK_ON   — normal operation
  CAUTION   — reduce position sizes
  RISK_OFF  — block new LONG entries

FRED API key (free): https://fred.stlouisfed.org/docs/api/api_key.html
Set via env var FRED_API_KEY or Fly secret: fly secrets set FRED_API_KEY=<key>
"""

import requests
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

SERIES = {
    "vix":          "VIXCLS",          # CBOE VIX (daily)
    "yield_spread": "T10Y2Y",          # 10Y Treasury minus 2Y Treasury (daily, <0 = inverted)
    "hy_spread":    "BAMLH0A0HYM2EY",  # ICE BofA HY Option-Adjusted Spread in % (daily)
}

# Thresholds
VIX_CAUTION  = 20.0   # Market unease begins
VIX_RISK_OFF = 30.0   # Fear regime
YIELD_FLAT   = 0.20   # Spread below here = flattening (caution)
YIELD_INVERT = 0.0    # Spread below 0 = inverted (recession signal)
HY_CAUTION   = 4.0    # Credit stress starting
HY_RISK_OFF  = 5.5    # Credit crunch territory


def _fetch_series(series_id: str, api_key: str, limit: int = 5) -> Optional[float]:
    """Fetch latest non-null observation from a FRED series. Returns None on failure."""
    try:
        resp = requests.get(
            FRED_BASE,
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "limit": limit,
                "sort_order": "desc",
            },
            timeout=10,
        )
        resp.raise_for_status()
        obs = [o for o in resp.json().get("observations", []) if o.get("value") not in (".", None, "")]
        return float(obs[0]["value"]) if obs else None
    except Exception as e:
        logger.warning(f"FRED fetch failed ({series_id}): {e}")
        return None


def get_macro_regime(api_key: str) -> dict:
    """
    Fetch macro indicators from FRED and classify the regime.

    Returns dict with:
        regime       — 'RISK_ON' | 'CAUTION' | 'RISK_OFF'
        vix          — float or None
        yield_spread — float or None (10Y-2Y %)
        hy_spread    — float or None (HY OAS %)
        risk_score   — int 0-6 (higher = more risk-off pressure)
        signals      — list[str] human-readable signal explanations
        timestamp    — ISO UTC string
    """
    vix          = _fetch_series(SERIES["vix"],          api_key)
    yield_spread = _fetch_series(SERIES["yield_spread"], api_key)
    hy_spread    = _fetch_series(SERIES["hy_spread"],    api_key)

    signals = []
    risk_score = 0

    # ── VIX ──────────────────────────────────────────────────────────────────
    if vix is not None:
        if vix >= VIX_RISK_OFF:
            signals.append(f"VIX {vix:.1f} ≥ {VIX_RISK_OFF} — FEAR (risk-off)")
            risk_score += 2
        elif vix >= VIX_CAUTION:
            signals.append(f"VIX {vix:.1f} ≥ {VIX_CAUTION} — elevated (caution)")
            risk_score += 1
        else:
            signals.append(f"VIX {vix:.1f} — benign (risk-on)")
    else:
        signals.append("VIX — unavailable (FRED lag or API error)")

    # ── YIELD SPREAD ──────────────────────────────────────────────────────────
    if yield_spread is not None:
        if yield_spread < YIELD_INVERT:
            signals.append(f"10Y-2Y {yield_spread:+.2f}% — INVERTED (recession signal, risk-off)")
            risk_score += 2
        elif yield_spread < YIELD_FLAT:
            signals.append(f"10Y-2Y {yield_spread:+.2f}% — flat, near-inversion (caution)")
            risk_score += 1
        else:
            signals.append(f"10Y-2Y {yield_spread:+.2f}% — normal (risk-on)")
    else:
        signals.append("Yield spread — unavailable")

    # ── HY CREDIT SPREAD ──────────────────────────────────────────────────────
    if hy_spread is not None:
        if hy_spread >= HY_RISK_OFF:
            signals.append(f"HY spread {hy_spread:.1f}% ≥ {HY_RISK_OFF}% — credit crunch (risk-off)")
            risk_score += 2
        elif hy_spread >= HY_CAUTION:
            signals.append(f"HY spread {hy_spread:.1f}% ≥ {HY_CAUTION}% — credit caution")
            risk_score += 1
        else:
            signals.append(f"HY spread {hy_spread:.1f}% — benign (risk-on)")
    else:
        signals.append("HY spread — unavailable")

    # ── CLASSIFY ──────────────────────────────────────────────────────────────
    if risk_score >= 4:
        regime = "RISK_OFF"
    elif risk_score >= 2:
        regime = "CAUTION"
    else:
        regime = "RISK_ON"

    return {
        "regime":       regime,
        "vix":          vix,
        "yield_spread": yield_spread,
        "hy_spread":    hy_spread,
        "risk_score":   risk_score,
        "signals":      signals,
        "timestamp":    datetime.utcnow().isoformat() + "Z",
    }


def format_regime_summary(snapshot: dict) -> str:
    """One-line summary for logging/display."""
    vix_s   = f"VIX={snapshot['vix']:.1f}"               if snapshot["vix"]          is not None else "VIX=N/A"
    yld_s   = f"10Y2Y={snapshot['yield_spread']:+.2f}%"  if snapshot["yield_spread"] is not None else "10Y2Y=N/A"
    hy_s    = f"HY={snapshot['hy_spread']:.1f}%"         if snapshot["hy_spread"]    is not None else "HY=N/A"
    return f"{snapshot['regime']} | {vix_s} | {yld_s} | {hy_s} | score={snapshot['risk_score']}/6"
