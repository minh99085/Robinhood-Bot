"""Online probability calibration for the pulse model.

A raw model probability of "UP" is not necessarily *true* — a model can be
systematically over- or under-confident. This calibrator learns the mapping
from predicted probability to the empirically realized up-frequency, using
binned reliability with shrinkage toward the identity (so it degrades to the
raw probability when data is thin). It also reports the Brier score (mean
squared error of probabilistic forecasts) for raw vs calibrated, which is the
metric that actually matters for an EV-driven bettor.

Persisted via the Store's `predictions` table so calibration survives restarts.
"""

from __future__ import annotations


class Calibrator:
    def __init__(self, store, bins: int = 20, shrink: float = 25.0, min_samples: int = 40):
        self.store = store
        self.bins = bins
        self.shrink = shrink          # pseudo-count pulling empirical rate toward p
        self.min_samples = min_samples

    def record(self, p_raw: float, outcome: int) -> None:
        """outcome = 1 if the round closed UP, else 0."""
        self.store.add_prediction(float(p_raw), int(outcome))

    def _data(self):
        return self.store.get_predictions(3000)

    def calibrate(self, p: float) -> float:
        p = min(0.98, max(0.02, float(p)))
        data = self._data()
        if len(data) < self.min_samples:
            return p  # not enough evidence — trust the raw model
        b = min(self.bins - 1, max(0, int(p * self.bins)))
        lo, hi = b / self.bins, (b + 1) / self.bins
        pts = [o for (pr, o) in data if lo <= pr < hi]
        n = len(pts)
        if n == 0:
            return p
        ups = sum(pts)
        cal = (ups + self.shrink * p) / (n + self.shrink)
        return min(0.98, max(0.02, cal))

    def stats(self) -> dict:
        data = self._data()
        n = len(data)
        if n == 0:
            return {"samples": 0, "brier_raw": None, "brier_cal": None, "calibrated": False}
        brier_raw = sum((pr - o) ** 2 for pr, o in data) / n
        # calibrated Brier (uses the current mapping on each stored raw prob)
        brier_cal = sum((self.calibrate(pr) - o) ** 2 for pr, o in data) / n
        return {
            "samples": n,
            "brier_raw": round(brier_raw, 4),
            "brier_cal": round(brier_cal, 4),
            "calibrated": n >= self.min_samples,
        }
