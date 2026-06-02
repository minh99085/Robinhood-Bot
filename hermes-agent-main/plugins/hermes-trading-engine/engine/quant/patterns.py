"""Lightweight price-action pattern scanner (BOS / CHoCH / Liquidity Sweep).

These are simplified, deterministic heuristics over recent swing highs/lows —
enough to drive the dashboard's "Pattern Scanner" strip with live signals.
Not financial advice; for paper simulation only.
"""

from __future__ import annotations

import numpy as np


def _swings(highs: np.ndarray, lows: np.ndarray, left: int = 2, right: int = 2):
    swing_hi, swing_lo = [], []
    n = len(highs)
    for i in range(left, n - right):
        if highs[i] == max(highs[i - left:i + right + 1]):
            swing_hi.append(i)
        if lows[i] == min(lows[i - left:i + right + 1]):
            swing_lo.append(i)
    return swing_hi, swing_lo


def scan(candles: list[dict]) -> dict:
    """Return signal dict for BOS, CHoCH, liquidity sweep + bias."""
    if len(candles) < 12:
        return {
            "bos": {"signal": False, "dir": "flat"},
            "choch": {"signal": False, "dir": "flat"},
            "liquidity_sweep": {"signal": False, "dir": "flat"},
            "bias": "neutral",
        }

    highs = np.array([c["h"] for c in candles[-120:]], dtype=float)
    lows = np.array([c["l"] for c in candles[-120:]], dtype=float)
    closes = np.array([c["c"] for c in candles[-120:]], dtype=float)

    swing_hi, swing_lo = _swings(highs, lows)
    last = closes[-1]

    # Break of Structure: close beyond the most recent confirmed swing.
    bos = {"signal": False, "dir": "flat"}
    if swing_hi and last > highs[swing_hi[-1]]:
        bos = {"signal": True, "dir": "up"}
    elif swing_lo and last < lows[swing_lo[-1]]:
        bos = {"signal": True, "dir": "down"}

    # Change of Character: trend of last two swing highs vs lows flips.
    choch = {"signal": False, "dir": "flat"}
    if len(swing_hi) >= 2 and len(swing_lo) >= 2:
        hh = highs[swing_hi[-1]] > highs[swing_hi[-2]]
        hl = lows[swing_lo[-1]] > lows[swing_lo[-2]]
        if hh and hl:
            choch = {"signal": True, "dir": "up"}
        elif (not hh) and (not hl):
            choch = {"signal": True, "dir": "down"}

    # Liquidity sweep: wick pokes beyond prior swing then closes back inside.
    sweep = {"signal": False, "dir": "flat"}
    if swing_hi and highs[-1] > highs[swing_hi[-1]] and last < highs[swing_hi[-1]]:
        sweep = {"signal": True, "dir": "down"}  # swept highs, likely reverse down
    elif swing_lo and lows[-1] < lows[swing_lo[-1]] and last > lows[swing_lo[-1]]:
        sweep = {"signal": True, "dir": "up"}

    votes = 0
    for s in (bos, choch, sweep):
        if s["dir"] == "up":
            votes += 1
        elif s["dir"] == "down":
            votes -= 1
    bias = "bullish" if votes > 0 else "bearish" if votes < 0 else "neutral"

    return {"bos": bos, "choch": choch, "liquidity_sweep": sweep, "bias": bias}
