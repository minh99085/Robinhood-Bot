"""Institutional probability-calibration models (pure Python, deterministic).

Quant scope — *Statistical & Probabilistic Modeling* and *Strategy Optimization
& Robustness Testing*: turn raw (over-confident) probabilities into calibrated
probabilities using fitted **Platt scaling**, **isotonic regression**, or
**temperature scaling**, with a **conservative shrink-toward-0.5 fallback** when
there are not enough resolved samples to fit a model.

Design invariants (Risk Management / Compliance):

* Pure stdlib + ``math`` — no numpy/sklearn, no randomness, no network. Every fit
  is deterministic, so replay and training reports are reproducible.
* The conservative fallback can only make a probability LESS aggressive (pulled
  toward 0.5); it never amplifies an under-supported edge.
* Calibration artifacts (method, slope/intercept, reliability buckets, fitted
  parameters, before/after metrics) round-trip to/from plain dicts for export
  into replay + training reports.

This module is intentionally a leaf (it imports only stdlib) so both the
research and training packages can depend on it without an import cycle.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Protocol

logger = logging.getLogger("hte.calibration")

CALIBRATION_METHODS = ("platt", "isotonic", "temperature", "conservative_shrink",
                       "identity")

_EPS = 1e-6
Pair = tuple[float, int]


# --------------------------------------------------------------------------- #
# numeric helpers
# --------------------------------------------------------------------------- #
def clamp01(p: float, eps: float = _EPS) -> float:
    """Clamp ``p`` into the open unit interval ``[eps, 1 - eps]``."""
    return min(1.0 - eps, max(eps, float(p)))


def logit(p: float) -> float:
    """Log-odds of ``p`` (clamped so it is always finite)."""
    pc = clamp01(p)
    return math.log(pc / (1.0 - pc))


def sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# --------------------------------------------------------------------------- #
# scoring metrics (operate on resolved (p, y) pairs)
# --------------------------------------------------------------------------- #
def brier(pairs: list[Pair]) -> float:
    """Mean squared error between predicted probability and binary outcome."""
    if not pairs:
        return 0.0
    return round(sum((float(p) - float(y)) ** 2 for p, y in pairs) / len(pairs), 8)


def log_loss(pairs: list[Pair]) -> float:
    """Mean negative log-likelihood (probabilities clamped, never infinite)."""
    if not pairs:
        return 0.0
    total = 0.0
    for p, y in pairs:
        pc = clamp01(p)
        total += -(int(y) * math.log(pc) + (1 - int(y)) * math.log(1.0 - pc))
    return round(total / len(pairs), 8)


def reliability_buckets(pairs: list[Pair], bins: int = 10) -> list[dict]:
    """Equal-width reliability table: predicted vs realized frequency per bin."""
    rows: list[dict] = []
    n = len(pairs)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sub = [(p, y) for p, y in pairs
               if (lo <= p < hi) or (b == bins - 1 and p == hi)]
        if not sub:
            rows.append({"bucket": f"{lo:.2f}-{hi:.2f}", "count": 0,
                         "avg_predicted": None, "realized_frequency": None})
            continue
        avg_p = sum(p for p, _ in sub) / len(sub)
        freq = sum(int(y) for _, y in sub) / len(sub)
        rows.append({"bucket": f"{lo:.2f}-{hi:.2f}", "count": len(sub),
                     "avg_predicted": round(avg_p, 6),
                     "realized_frequency": round(freq, 6),
                     "weight": round(len(sub) / n, 6) if n else 0.0})
    return rows


def ece(pairs: list[Pair], bins: int = 10) -> float:
    """Expected Calibration Error: count-weighted |predicted - realized|."""
    if not pairs:
        return 0.0
    n = len(pairs)
    total = 0.0
    for row in reliability_buckets(pairs, bins):
        if not row["count"]:
            continue
        total += (row["count"] / n) * abs(row["avg_predicted"] - row["realized_frequency"])
    return round(total, 8)


def _fit_logistic_1d(xs: list[float], ys: list[int], *, iters: int = 300,
                     lr: float = 0.3) -> tuple[float, float]:
    """Fit ``P(y=1) = sigmoid(w*x + b)`` by gradient descent on standardized x.

    Returns ``(w, b)`` in the ORIGINAL x scale. Degenerate inputs (no variance or
    a single class) return the identity-ish ``(1.0, 0.0)`` so callers stay safe.
    """
    n = len(xs)
    if n == 0:
        return 1.0, 0.0
    mean_y = sum(ys) / n
    if mean_y <= 0.0 or mean_y >= 1.0:
        return 1.0, 0.0
    mean_x = sum(xs) / n
    var_x = sum((x - mean_x) ** 2 for x in xs) / n
    std_x = math.sqrt(var_x)
    if std_x < 1e-12:
        return 1.0, 0.0
    zs = [(x - mean_x) / std_x for x in xs]
    w, b = 0.0, math.log(mean_y / (1.0 - mean_y))
    for _ in range(iters):
        gw = gb = 0.0
        for z, y in zip(zs, ys):
            err = sigmoid(w * z + b) - y
            gw += err * z
            gb += err
        w -= lr * gw / n
        b -= lr * gb / n
    # un-standardize: w*x + b == (w/std_x)*x + (b - w*mean_x/std_x)
    w_orig = w / std_x
    b_orig = b - w * mean_x / std_x
    return w_orig, b_orig


# --------------------------------------------------------------------------- #
# calibration slope / intercept (logistic regression of y on logit(p))
# --------------------------------------------------------------------------- #
def calibration_slope_intercept(pairs: list[Pair]) -> tuple[float, float]:
    """Cox calibration slope + intercept.

    Fits ``logit(P(y=1)) = intercept + slope * logit(p)``. Perfect calibration
    gives ``slope == 1`` and ``intercept == 0``; an over-confident model gives
    ``slope < 1``.
    """
    if not pairs:
        return 1.0, 0.0
    xs = [logit(p) for p, _ in pairs]
    ys = [int(y) for _, y in pairs]
    slope, intercept = _fit_logistic_1d(xs, ys)
    return round(slope, 6), round(intercept, 6)


# --------------------------------------------------------------------------- #
# fitted models
# --------------------------------------------------------------------------- #
class _Model(Protocol):
    method: str

    def transform(self, p: float) -> float: ...

    def params(self) -> dict: ...


@dataclass
class IdentityModel:
    """Pass-through (no calibration data available / not requested)."""

    method: str = "identity"

    def transform(self, p: float) -> float:
        return clamp01(p)

    def params(self) -> dict:
        return {}

    @classmethod
    def from_params(cls, _p: dict) -> "IdentityModel":
        return cls()


@dataclass
class PlattModel:
    """Platt scaling on the logit feature: ``sigmoid(a*logit(p) + b)``."""

    a: float
    b: float
    method: str = "platt"

    def transform(self, p: float) -> float:
        return clamp01(sigmoid(self.a * logit(p) + self.b))

    def params(self) -> dict:
        return {"a": round(self.a, 12), "b": round(self.b, 12)}

    @classmethod
    def from_params(cls, p: dict) -> "PlattModel":
        return cls(a=float(p["a"]), b=float(p["b"]))


@dataclass
class TemperatureModel:
    """Temperature scaling: ``sigmoid(logit(p) / T)``. ``T > 1`` softens."""

    temperature: float
    method: str = "temperature"

    def transform(self, p: float) -> float:
        t = max(1e-3, float(self.temperature))
        return clamp01(sigmoid(logit(p) / t))

    def params(self) -> dict:
        return {"temperature": round(self.temperature, 12)}

    @classmethod
    def from_params(cls, p: dict) -> "TemperatureModel":
        return cls(temperature=float(p["temperature"]))


@dataclass
class IsotonicModel:
    """Monotone (non-decreasing) step calibration fitted via PAVA."""

    xs: list[float]
    ys: list[float]
    method: str = "isotonic"

    def transform(self, p: float) -> float:
        p = clamp01(p)
        if not self.xs:
            return p
        if p <= self.xs[0]:
            return clamp01(self.ys[0])
        if p >= self.xs[-1]:
            return clamp01(self.ys[-1])
        # linear interpolation between adjacent calibrated knots (monotone)
        lo = 0
        for i in range(1, len(self.xs)):
            if p <= self.xs[i]:
                lo = i - 1
                break
        x0, x1 = self.xs[lo], self.xs[lo + 1]
        y0, y1 = self.ys[lo], self.ys[lo + 1]
        if x1 - x0 < 1e-12:
            return clamp01(y1)
        frac = (p - x0) / (x1 - x0)
        return clamp01(y0 + frac * (y1 - y0))

    def params(self) -> dict:
        return {"xs": [round(x, 12) for x in self.xs],
                "ys": [round(y, 12) for y in self.ys]}

    @classmethod
    def from_params(cls, p: dict) -> "IsotonicModel":
        return cls(xs=[float(x) for x in p["xs"]], ys=[float(y) for y in p["ys"]])


@dataclass
class ConservativeShrinkModel:
    """Pull a raw probability toward ``anchor`` (0.5) by ``shrink`` in [0, 1].

    Used when there are too few resolved samples to fit a real calibrator: it can
    only reduce the distance from 0.5 (never increase it), guaranteeing a fresh
    trainer is never made more aggressive by an under-supported calibration.
    """

    shrink: float
    anchor: float = 0.5
    method: str = "conservative_shrink"

    def transform(self, p: float) -> float:
        p = clamp01(p)
        s = min(1.0, max(0.0, self.shrink))
        return clamp01(self.anchor + (p - self.anchor) * (1.0 - s))

    def params(self) -> dict:
        return {"shrink": round(self.shrink, 12), "anchor": round(self.anchor, 12)}

    @classmethod
    def from_params(cls, p: dict) -> "ConservativeShrinkModel":
        return cls(shrink=float(p["shrink"]), anchor=float(p.get("anchor", 0.5)))


_MODEL_TYPES = {
    "identity": IdentityModel,
    "platt": PlattModel,
    "temperature": TemperatureModel,
    "isotonic": IsotonicModel,
    "conservative_shrink": ConservativeShrinkModel,
}


# --------------------------------------------------------------------------- #
# fit functions
# --------------------------------------------------------------------------- #
def fit_platt(pairs: list[Pair]) -> PlattModel:
    """Fit Platt scaling on the logit feature."""
    xs = [logit(p) for p, _ in pairs]
    ys = [int(y) for _, y in pairs]
    a, b = _fit_logistic_1d(xs, ys)
    logger.debug("fit_platt n=%d a=%.4f b=%.4f", len(pairs), a, b)
    return PlattModel(a=a, b=b)


def fit_temperature(pairs: list[Pair], *, lo: float = 0.05, hi: float = 10.0,
                    iters: int = 80) -> TemperatureModel:
    """Fit temperature scaling by golden-section search minimizing log-loss."""
    if not pairs:
        return TemperatureModel(temperature=1.0)
    logits = [(logit(p), int(y)) for p, y in pairs]

    def nll(t: float) -> float:
        t = max(1e-3, t)
        total = 0.0
        for lg, y in logits:
            pc = clamp01(sigmoid(lg / t))
            total += -(y * math.log(pc) + (1 - y) * math.log(1.0 - pc))
        return total

    gr = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = nll(c), nll(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = nll(c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = nll(d)
    t = (a + b) / 2
    logger.debug("fit_temperature n=%d T=%.4f", len(pairs), t)
    return TemperatureModel(temperature=t)


def fit_isotonic(pairs: list[Pair]) -> IsotonicModel:
    """Fit isotonic regression via the Pool-Adjacent-Violators Algorithm."""
    if not pairs:
        return IsotonicModel(xs=[], ys=[])
    ordered = sorted(pairs, key=lambda pr: pr[0])
    xs = [float(p) for p, _ in ordered]
    ys = [float(y) for _, y in ordered]
    # PAVA: blocks of (sum, count, value)
    blocks: list[list[float]] = []
    for y in ys:
        blocks.append([y, 1.0, y])
        while len(blocks) >= 2 and blocks[-2][2] > blocks[-1][2]:
            s2, c2, _ = blocks.pop()
            s1, c1, _ = blocks.pop()
            s, c = s1 + s2, c1 + c2
            blocks.append([s, c, s / c])
    fitted: list[float] = []
    for s, c, v in blocks:
        fitted.extend([v] * int(c))
    # collapse to unique x knots (keep last fitted value per x for monotone steps)
    knot_x: list[float] = []
    knot_y: list[float] = []
    for x, fy in zip(xs, fitted):
        if knot_x and abs(knot_x[-1] - x) < 1e-12:
            knot_y[-1] = fy
        else:
            knot_x.append(x)
            knot_y.append(fy)
    logger.debug("fit_isotonic n=%d knots=%d", len(pairs), len(knot_x))
    return IsotonicModel(xs=knot_x, ys=knot_y)


def fit_conservative_shrink(pairs: list[Pair], *, base_shrink: float = 0.15,
                            min_samples: int = 20,
                            anchor: float = 0.5) -> ConservativeShrinkModel:
    """Shrink-toward-anchor fallback whose strength decays with evidence.

    With zero samples the shrink approaches 1 (probability pinned to the anchor);
    with ``min_samples`` resolved observations it relaxes to ``base_shrink``.
    """
    n = len(pairs)
    deficit = max(0.0, 1.0 - (n / float(min_samples))) if min_samples > 0 else 0.0
    shrink = base_shrink + (1.0 - base_shrink) * deficit
    logger.debug("fit_conservative_shrink n=%d shrink=%.4f", n, shrink)
    return ConservativeShrinkModel(shrink=min(1.0, max(0.0, shrink)), anchor=anchor)


# --------------------------------------------------------------------------- #
# orchestrating calibrator
# --------------------------------------------------------------------------- #
@dataclass
class InstitutionalCalibrator:
    """Fit + apply a calibration model with a conservative fallback.

    ``method="auto"`` chooses the conservative shrink fallback when there are
    fewer than ``min_samples`` resolved observations, otherwise it selects the
    fitted method (Platt / temperature / isotonic) with the lowest in-sample
    log-loss (isotonic is only eligible with ``>= 2*min_samples`` to avoid
    over-fitting). An explicit ``method`` forces that calibrator.
    """

    method: str = "auto"
    min_samples: int = 20
    base_shrink: float = 0.15
    bins: int = 10
    _model: _Model = field(default_factory=IdentityModel)
    fitted_method: str = "identity"
    effective_sample_size: float = 0.0
    slope: float = 1.0
    intercept: float = 0.0
    _buckets: list[dict] = field(default_factory=list)
    _metrics: dict = field(default_factory=dict)

    # -- fitting -------------------------------------------------------------
    def fit(self, pairs: list[Pair]) -> "InstitutionalCalibrator":
        pairs = [(float(p), int(y)) for p, y in (pairs or [])]
        self.effective_sample_size = float(len(pairs))
        self.slope, self.intercept = calibration_slope_intercept(pairs)
        self._buckets = reliability_buckets(pairs, self.bins)
        self._model = self._select_model(pairs)
        self.fitted_method = self._model.method
        cal = [(self._model.transform(p), y) for p, y in pairs]
        self._metrics = {
            "raw": {"brier": brier(pairs), "log_loss": log_loss(pairs),
                    "ece": ece(pairs, self.bins)},
            "calibrated": {"brier": brier(cal), "log_loss": log_loss(cal),
                           "ece": ece(cal, self.bins)},
        }
        logger.info("calibrator fitted method=%s n=%d slope=%.3f ece_raw=%.4f "
                    "ece_cal=%.4f", self.fitted_method, len(pairs), self.slope,
                    self._metrics["raw"]["ece"], self._metrics["calibrated"]["ece"])
        return self

    def _select_model(self, pairs: list[Pair]) -> _Model:
        if self.method == "identity":
            return IdentityModel()
        if self.method == "conservative_shrink":
            return fit_conservative_shrink(pairs, base_shrink=self.base_shrink,
                                           min_samples=self.min_samples)
        if len(pairs) < self.min_samples and self.method == "auto":
            return fit_conservative_shrink(pairs, base_shrink=self.base_shrink,
                                           min_samples=self.min_samples)
        if self.method == "platt":
            return fit_platt(pairs)
        if self.method == "temperature":
            return fit_temperature(pairs)
        if self.method == "isotonic":
            return fit_isotonic(pairs)
        # auto with enough samples: pick the lowest-log-loss fitted model
        candidates: list[_Model] = [fit_platt(pairs), fit_temperature(pairs)]
        if len(pairs) >= 2 * self.min_samples:
            candidates.append(fit_isotonic(pairs))
        best = min(candidates,
                   key=lambda m: log_loss([(m.transform(p), y) for p, y in pairs]))
        return best

    # -- applying ------------------------------------------------------------
    def transform(self, p: float) -> float:
        return self._model.transform(p)

    def transform_with_interval(self, p: float, *, z: float = 1.0
                                ) -> tuple[float, float, float]:
        """Return ``(p_cal, lo, hi)`` with a binomial-style interval whose width
        grows as the effective sample size shrinks (more data -> tighter band)."""
        p_cal = self.transform(p)
        n_eff = max(1.0, self.effective_sample_size)
        half = z * math.sqrt(max(p_cal * (1.0 - p_cal), _EPS) / n_eff)
        # add a structural floor so a tiny sample never reports false precision
        half += 0.5 * max(0.0, 1.0 - self.effective_sample_size / max(1, self.min_samples)) * 0.25
        lo = max(0.0, p_cal - half)
        hi = min(1.0, p_cal + half)
        return p_cal, round(lo, 6), round(hi, 6)

    # -- introspection / export ---------------------------------------------
    @property
    def calibration_method(self) -> str:
        return self.fitted_method

    def reliability_buckets(self) -> list[dict]:
        return list(self._buckets)

    def metrics(self) -> dict:
        return dict(self._metrics)

    def to_artifact(self) -> dict:
        """Serialize the fitted calibrator for replay + training reports."""
        return {
            "method": self.fitted_method,
            "requested_method": self.method,
            "min_samples": self.min_samples,
            "base_shrink": self.base_shrink,
            "bins": self.bins,
            "effective_sample_size": self.effective_sample_size,
            "slope": self.slope,
            "intercept": self.intercept,
            "reliability_buckets": self.reliability_buckets(),
            "metrics": self.metrics(),
            "params": self._model.params(),
        }

    @classmethod
    def from_artifact(cls, art: dict) -> "InstitutionalCalibrator":
        method = art.get("method", "identity")
        model_cls = _MODEL_TYPES.get(method, IdentityModel)
        model = model_cls.from_params(art.get("params", {}))
        inst = cls(method=art.get("requested_method", method),
                   min_samples=int(art.get("min_samples", 20)),
                   base_shrink=float(art.get("base_shrink", 0.15)),
                   bins=int(art.get("bins", 10)))
        inst._model = model
        inst.fitted_method = method
        inst.effective_sample_size = float(art.get("effective_sample_size", 0.0))
        inst.slope = float(art.get("slope", 1.0))
        inst.intercept = float(art.get("intercept", 0.0))
        inst._buckets = list(art.get("reliability_buckets", []))
        inst._metrics = dict(art.get("metrics", {}))
        return inst
