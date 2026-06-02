"""CalibrationAdapter — map a raw LLM/research probability to a calibrated one.

Quant scope — *Statistical & Probabilistic Modeling* + *Strategy Optimization &
Robustness Testing*:

* Default (un-fitted) behaviour is the original conservative identity-with-
  shrinkage: shrink the raw probability toward 0.5 by a small factor, because raw
  LLM probabilities are typically over-confident. This is the safe fallback when
  there are not enough resolved outcomes to fit anything.
* When resolved ``(p_raw, outcome)`` pairs are available, the adapter can FIT a
  real calibrator — Platt scaling, isotonic regression, or temperature scaling —
  via :class:`engine.calibration_models.InstitutionalCalibrator`, automatically
  falling back to the conservative shrink when samples are insufficient.

Grok stays research-only: this adapter only transforms a probability and exports
calibration artifacts; it never sizes, approves, places, or arms an order.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from engine.calibration_models import InstitutionalCalibrator

CALIBRATION_VERSION = "v1"

logger = logging.getLogger("hte.research.calibration_adapter")


class CalibrationAdapter:
    """Conservative-shrink by default; optionally fitted from resolved outcomes.

    ``apply(p_raw)`` keeps its original semantics until :meth:`fit` is called with
    resolved ``(probability, outcome)`` pairs, after which it applies the fitted
    calibration model.
    """

    def __init__(self, shrink: Optional[float] = None, *, min_samples: int = 20):
        if shrink is None:
            try:
                shrink = float(os.getenv("RESEARCH_CALIBRATION_SHRINK", "0.15"))
            except (TypeError, ValueError):
                shrink = 0.15
        self.shrink = min(1.0, max(0.0, shrink))
        self.min_samples = int(min_samples)
        self.version = CALIBRATION_VERSION
        self.method = "shrink"
        self._calibrator: Optional[InstitutionalCalibrator] = None

    def fit(self, pairs: list[tuple[float, int]], *, method: str = "auto"
            ) -> "CalibrationAdapter":
        """Fit a calibration model from resolved ``(probability, outcome)`` pairs.

        Falls back to the conservative shrink fallback (inside the calibrator)
        when fewer than ``min_samples`` resolved observations are available.
        """
        self._calibrator = InstitutionalCalibrator(
            method=method, min_samples=self.min_samples,
            base_shrink=self.shrink).fit(pairs)
        self.method = self._calibrator.calibration_method
        self.version = f"{self.method}-v1"
        logger.info("calibration adapter fitted method=%s n=%d", self.method,
                    int(self._calibrator.effective_sample_size))
        return self

    def apply(self, p_raw: Optional[float]) -> Optional[float]:
        if p_raw is None:
            return None
        try:
            p = float(p_raw)
        except (TypeError, ValueError):
            return None
        p = min(1.0, max(0.0, p))
        if self._calibrator is not None:
            return round(self._calibrator.transform(p), 6)
        return round(0.5 + (p - 0.5) * (1.0 - self.shrink), 6)

    def transform_with_distribution(self, p_raw: Optional[float], *,
                                    market_p: Optional[float] = None, z: float = 1.0,
                                    evidence_quality: float = 1.0, ambiguity: float = 0.0,
                                    chainlink_stale: bool = False,
                                    calibration_instability: float = 0.0) -> dict:
        """Calibrated probability DISTRIBUTION (mean + credible interval +
        effective sample size + method), optionally Bayesian-shrunk toward the
        market price when evidence is weak / sample low / ambiguous / stale /
        unstable. Research-only: transforms a probability, never sizes/approves."""
        from engine.training.probability_stack import bayesian_shrink
        if p_raw is None:
            return {"mean": None, "ci_low": None, "ci_high": None,
                    "method": self.method, "effective_sample_size": 0.0}
        if self._calibrator is not None:
            mean, lo, hi = self._calibrator.transform_with_interval(float(p_raw), z=z)
            ess = float(self._calibrator.effective_sample_size)
        else:
            mean = self.apply(p_raw) or 0.5
            half = 0.5 * self.shrink + 0.1
            lo, hi = max(0.0, mean - half), min(1.0, mean + half)
            ess = 0.0
        if market_p is not None:
            shrunk = bayesian_shrink(mean, float(market_p), evidence_quality=evidence_quality,
                                     effective_sample_size=ess, ambiguity=ambiguity,
                                     chainlink_stale=chainlink_stale,
                                     calibration_instability=calibration_instability)
            # keep the interval centered on the shrunk mean (width preserved)
            half = abs(hi - lo) / 2.0
            mean, lo, hi = shrunk, max(0.0, shrunk - half), min(1.0, shrunk + half)
        return {"mean": round(mean, 6), "ci_low": round(lo, 6), "ci_high": round(hi, 6),
                "method": self.method, "effective_sample_size": round(ess, 4)}

    def to_artifact(self) -> dict:
        """Export the fitted calibration artifact (for replay + training reports).

        Returns the default-shrink descriptor when the adapter has not been fitted.
        """
        if self._calibrator is not None:
            return self._calibrator.to_artifact()
        return {"method": "shrink", "shrink": self.shrink,
                "effective_sample_size": 0.0, "slope": 1.0, "intercept": 0.0,
                "reliability_buckets": [], "metrics": {}, "params": {}}
