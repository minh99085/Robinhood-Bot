"""Microstructure features + an online logistic learner for the pulse model.

These features are computed causally from OHLCV candles (so they can be
backtested honestly, unlike historical order-book/funding data which isn't
freely available). The OnlineLogistic learns, walk-forward, whether they predict
the next bar's direction — and the backtester compares its out-of-sample Brier
against the markov+MC baseline to decide if the signal actually adds skill.

Features (each roughly standardized inside the learner):
  clv        close location value of the last bar in [-1,1] (intrabar pressure)
  clv_ewma   smoothed CLV over the recent window (persistent pressure)
  mom_z      short momentum z-score (trend strength)
  range_z    current bar range vs recent (volatility regime)
  vol_imb    signed-volume imbalance over the window (order-flow proxy)
"""

from __future__ import annotations

import math

N_FEATURES = 5


def _clv(c: dict) -> float:
    rng = max(c["h"] - c["l"], 1e-9)
    return ((c["c"] - c["l"]) - (c["h"] - c["c"])) / rng


def pulse_features(candles: list[dict], window: int = 20, mom: int = 5) -> list[float]:
    """Return a fixed-length feature vector from candles up to (and including) the last.
    Uses only the given candles — caller must pass a causal slice (no future bars)."""
    if len(candles) < mom + 2:
        return [0.0] * N_FEATURES
    w = candles[-window:]
    closes = [c["c"] for c in candles]

    clv_last = _clv(candles[-1])

    clvs = [_clv(c) for c in w]
    # EWMA (more weight on recent)
    alpha = 2.0 / (len(clvs) + 1)
    ewma = clvs[0]
    for v in clvs[1:]:
        ewma = alpha * v + (1 - alpha) * ewma

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - mom - 1, len(closes))
            if i > 0 and closes[i - 1] > 0]
    if len(rets) >= 2:
        import statistics
        sd = statistics.pstdev(rets) or 1e-9
        mom_z = sum(rets[-mom:]) / (sd * math.sqrt(mom))
    else:
        mom_z = 0.0

    ranges = [c["h"] - c["l"] for c in w]
    rmean = sum(ranges) / len(ranges)
    rsd = (sum((x - rmean) ** 2 for x in ranges) / len(ranges)) ** 0.5 or 1e-9
    range_z = ((candles[-1]["h"] - candles[-1]["l"]) - rmean) / rsd

    vols = [c.get("v", 0.0) or 0.0 for c in w]
    signs = [1 if c["c"] >= c["o"] else -1 for c in w]
    tot = sum(vols)
    vol_imb = (sum(s * v for s, v in zip(signs, vols)) / tot) if tot > 0 else 0.0

    return [clv_last, ewma, mom_z, range_z, vol_imb]


class OnlineLogistic:
    """Tiny online logistic regression with causal feature standardization (Welford).

    predict_proba uses stats learned from PAST observations only; observe() then
    folds in the new sample and takes one SGD step. No label lookahead.
    """

    def __init__(self, n_features: int = N_FEATURES, lr: float = 0.05, l2: float = 1e-4):
        self.n = n_features
        self.lr, self.l2 = lr, l2
        self.w = [0.0] * n_features
        self.b = 0.0
        self.count = 0
        self.mean = [0.0] * n_features
        self.M2 = [0.0] * n_features

    def _std(self, i: int) -> float:
        if self.count > 1 and self.M2[i] > 0:
            return (self.M2[i] / self.count) ** 0.5
        return 1.0

    def _z(self, x: list[float]) -> list[float]:
        return [max(-6.0, min(6.0, (x[i] - self.mean[i]) / self._std(i))) for i in range(self.n)]

    def predict_proba(self, x: list[float]) -> float:
        z = self._z(x)
        s = self.b + sum(self.w[i] * z[i] for i in range(self.n))
        s = max(-30.0, min(30.0, s))
        return 1.0 / (1.0 + math.exp(-s))

    def observe(self, x: list[float], y: int) -> None:
        # update running feature stats (Welford) — features only, no label
        self.count += 1
        for i in range(self.n):
            d = x[i] - self.mean[i]
            self.mean[i] += d / self.count
            self.M2[i] += d * (x[i] - self.mean[i])
        # SGD step on the (now-standardized) sample
        z = self._z(x)
        p = self.predict_proba(x)
        err = p - y
        for i in range(self.n):
            self.w[i] -= self.lr * (err * z[i] + self.l2 * self.w[i])
        self.b -= self.lr * err

    def ready(self) -> bool:
        return self.count >= 40

    # --- persistence (for the live engine) ------------------------------
    def to_dict(self) -> dict:
        return {"w": self.w, "b": self.b, "count": self.count, "mean": self.mean, "M2": self.M2}

    def load(self, d: dict) -> None:
        if not d:
            return
        self.w = d.get("w", self.w)
        self.b = d.get("b", self.b)
        self.count = d.get("count", self.count)
        self.mean = d.get("mean", self.mean)
        self.M2 = d.get("M2", self.M2)
