"""Chainlink feature engineering.

Quant responsibility — *Data Preprocessing & Feature Engineering*: turns a
time-ordered list of :class:`ChainlinkReading` into normalized, deterministic
features: normalized oracle deviation, volatility, trend/momentum, staleness,
heartbeat gap, and an oracle-freshness score in [0, 1].

All functions are pure and safe with 0/1 samples (no exceptions, no NaNs).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from .feeds.chainlink import ChainlinkReading


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class ChainlinkFeatures:
    feed_key: str
    value: float
    n_samples: int
    age_s: float
    heartbeat_s: float
    stale: bool
    freshness: float          # 1.0 = just updated, 0.0 = at/over heartbeat
    deviation: float          # z-score-like: (last - mean) / std (clamped)
    volatility: float         # stdev of pct returns over the window
    momentum: float           # (last - first) / |first|
    trend: str                # "up" | "down" | "flat"
    heartbeat_gap: float      # max consecutive update gap / heartbeat
    updated_at: float
    inconsistent: bool = False  # abnormal jump vs window (data-quality flag)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


def compute_features(readings: list, *, now: Optional[float] = None,
                     heartbeat_s: float = 3600.0,
                     inconsistent_z: float = 5.0,
                     inconsistent_jump: float = 0.5) -> Optional[ChainlinkFeatures]:
    """Compute features from time-ordered readings (oldest -> newest). Returns
    ``None`` if there are no readings (missing oracle)."""
    if not readings:
        return None
    now = now if now is not None else time.time()
    rs = sorted(readings, key=lambda r: r.updated_at)
    values = [r.value for r in rs]
    last = values[-1]
    first = values[0]
    n = len(values)

    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    std = math.sqrt(var)
    deviation = _clamp((last - mean) / std, -5.0, 5.0) if std > 1e-12 else 0.0

    rets = []
    for a, b in zip(values[:-1], values[1:]):
        if abs(a) > 1e-12:
            rets.append((b - a) / a)
    if len(rets) >= 1:
        rmean = sum(rets) / len(rets)
        rvar = sum((x - rmean) ** 2 for x in rets) / len(rets) if len(rets) > 1 else 0.0
        volatility = math.sqrt(rvar)
    else:
        volatility = 0.0

    momentum = (last - first) / abs(first) if abs(first) > 1e-12 else 0.0
    trend = "up" if momentum > 0.001 else "down" if momentum < -0.001 else "flat"

    # heartbeat gap: largest spacing between consecutive on-chain updates
    gaps = [b.updated_at - a.updated_at for a, b in zip(rs[:-1], rs[1:])]
    max_gap = max(gaps) if gaps else 0.0
    heartbeat_gap = (max_gap / heartbeat_s) if heartbeat_s > 0 else 0.0

    age = max(0.0, now - rs[-1].updated_at)
    freshness = _clamp(1.0 - (age / heartbeat_s)) if heartbeat_s > 0 else 0.0
    stale = rs[-1].is_stale(now, heartbeat_s)

    # Data-quality flag: the newest reading is an outlier vs the PRECEDING window
    # (robust to the outlier inflating the full-window std), or a single-step jump
    # beyond `inconsistent_jump` (e.g. >50%). A lone bad print must be detectable.
    inconsistent = False
    prior = values[:-1]
    if len(prior) >= 3:
        pmean = sum(prior) / len(prior)
        pvar = sum((v - pmean) ** 2 for v in prior) / len(prior)
        pstd = math.sqrt(pvar)
        if pstd > 1e-12 and abs((last - pmean) / pstd) >= inconsistent_z:
            inconsistent = True
    if len(values) >= 2 and abs(values[-2]) > 1e-12:
        if abs((last - values[-2]) / values[-2]) >= inconsistent_jump:
            inconsistent = True

    return ChainlinkFeatures(
        feed_key=rs[-1].feed_key, value=last, n_samples=n, age_s=age,
        heartbeat_s=heartbeat_s, stale=stale, freshness=freshness,
        deviation=deviation, volatility=volatility, momentum=momentum, trend=trend,
        heartbeat_gap=heartbeat_gap, updated_at=rs[-1].updated_at,
        inconsistent=inconsistent)
