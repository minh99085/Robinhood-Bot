"""Bregman divergence + probability-simplex utilities (pure Python, deterministic).

Quant scope — *Statistical & Probabilistic Modeling* and *Bregman arbitrage
priority*: the building blocks for Polymarket Bregman arbitrage. A set of
mutually-exclusive, exhaustive Polymarket outcomes must price onto the
probability simplex ``{p : p_i >= 0, Σ p_i = 1}``. Observed executable prices
that lie OFF the simplex encode a (possibly tradable) mispricing; the Bregman
divergence between the observed price vector and its simplex projection is a
numerically-safe, deterministic measure of that gap.

Divergences provided (all are Bregman divergences D_φ(p, q) for a convex
generator φ):

* ``squared_euclidean``           — φ(x) = Σ x_i²            (== generalized
                                     entropy divergence at α = 2).
* ``kl_divergence``               — normalized KL, Σ p log(p/q).
* ``generalized_kl_divergence``   — I-divergence Σ p log(p/q) − p + q (valid for
                                     unnormalized non-negative vectors; the
                                     α → 1 limit of the generalized entropy).
* ``generalized_entropy_divergence`` — the Tsallis/α generalized-entropy family.

Everything is stdlib + ``math`` only: no numpy, no randomness, no network, so
detection is fully reproducible for replay + certification.
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

logger = logging.getLogger("hte.training.bregman")

_EPS = 1e-12
Vector = Sequence[float]

DIVERGENCE_METHODS = ("squared_euclidean", "kl", "generalized_kl", "generalized_entropy")


# --------------------------------------------------------------------------- #
# numeric helpers
# --------------------------------------------------------------------------- #
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def safe_prob(p: float, eps: float = _EPS) -> float:
    """Clamp a probability into ``[eps, 1 - eps]`` (never 0 or 1)."""
    return min(1.0 - eps, max(eps, float(p)))


def _safe_pos(x: float, eps: float = _EPS) -> float:
    """Clamp a value to a strictly-positive floor (for logs / powers)."""
    return max(eps, float(x))


def normalize(v: Vector) -> list[float]:
    """KL projection onto the simplex: divide by the sum (uniform if all zero)."""
    vals = [max(0.0, float(x)) for x in v]
    total = sum(vals)
    n = len(vals)
    if n == 0:
        return []
    if total <= 0:
        return [1.0 / n] * n
    return [x / total for x in vals]


def _check_same_length(p: Vector, q: Vector) -> None:
    if len(p) != len(q):
        raise ValueError(f"vectors must be the same length ({len(p)} != {len(q)})")


# --------------------------------------------------------------------------- #
# Bregman divergences
# --------------------------------------------------------------------------- #
def squared_euclidean(p: Vector, q: Vector) -> float:
    """Squared Euclidean distance Σ (p_i − q_i)² (Bregman; φ(x) = Σ x_i²)."""
    _check_same_length(p, q)
    return float(sum((float(a) - float(b)) ** 2 for a, b in zip(p, q)))


def kl_divergence(p: Vector, q: Vector, *, eps: float = _EPS) -> float:
    """Normalized Kullback–Leibler divergence Σ p_i log(p_i / q_i).

    Inputs are normalized to distributions first; both are clamped so the result
    is always finite (no inf / nan).
    """
    _check_same_length(p, q)
    pn, qn = normalize(p), normalize(q)
    total = 0.0
    for a, b in zip(pn, qn):
        a = _safe_pos(a, eps)
        b = _safe_pos(b, eps)
        total += a * math.log(a / b)
    return float(max(0.0, total))


# safe alias (the implementation above is already numerically safe)
safe_kl_divergence = kl_divergence


def generalized_kl_divergence(p: Vector, q: Vector, *, eps: float = _EPS) -> float:
    """Generalized KL / I-divergence Σ [p_i log(p_i/q_i) − p_i + q_i].

    This is the Bregman divergence of the negative Shannon entropy on the
    positive orthant; unlike normalized KL it is sensitive to a difference in
    total mass (so it detects a price vector whose sum ≠ 1).
    """
    _check_same_length(p, q)
    total = 0.0
    for a, b in zip(p, q):
        a = max(0.0, float(a))
        b = _safe_pos(b, eps)
        if a <= 0.0:
            total += b           # limit of a*log(a/b) is 0 as a->0
        else:
            total += a * math.log(a / b) - a + b
    return float(max(0.0, total))


def generalized_entropy_divergence(p: Vector, q: Vector, *, alpha: float = 1.0,
                                   eps: float = _EPS) -> float:
    """Generalized-entropy (Tsallis α) Bregman divergence.

    Generator φ_α(x) = Σ (x_i^α − x_i) / (α − 1). Special cases:

    * ``alpha == 1`` -> generalized KL (I-divergence).
    * ``alpha == 2`` -> squared Euclidean distance.

    Always ``>= 0`` and finite (positive clamping on powers/logs).
    """
    _check_same_length(p, q)
    if abs(alpha - 1.0) < 1e-9:
        return generalized_kl_divergence(p, q, eps=eps)
    denom = alpha - 1.0
    total = 0.0
    for a, b in zip(p, q):
        a = _safe_pos(a, eps)
        b = _safe_pos(b, eps)
        phi_p = (a ** alpha - a) / denom
        phi_q = (b ** alpha - b) / denom
        grad_q = (alpha * b ** (alpha - 1.0) - 1.0) / denom
        total += phi_p - phi_q - grad_q * (a - b)
    return float(max(0.0, total))


# --------------------------------------------------------------------------- #
# simplex projection + validation
# --------------------------------------------------------------------------- #
def project_to_simplex(v: Vector, *, z: float = 1.0) -> list[float]:
    """Euclidean projection of ``v`` onto the probability simplex {x>=0, Σx=z}.

    Implements the exact O(n log n) algorithm (Wang & Carreira-Perpiñán, 2013).
    """
    vals = [float(x) for x in v]
    n = len(vals)
    if n == 0:
        return []
    u = sorted(vals, reverse=True)
    css = 0.0
    rho = 0
    theta = 0.0
    cumulative = 0.0
    for i, ui in enumerate(u):
        cumulative += ui
        candidate = (cumulative - z) / (i + 1)
        if ui - candidate > 0:
            rho = i + 1
            theta = candidate
    _ = css, rho
    return [max(0.0, x - theta) for x in vals]


def is_on_simplex(v: Vector, *, tol: float = 1e-9) -> bool:
    """True when ``v`` is a valid probability vector (non-negative, sums to 1)."""
    vals = [float(x) for x in v]
    if not vals:
        return False
    if any(x < -tol or x > 1.0 + tol for x in vals):
        return False
    return abs(sum(vals) - 1.0) <= tol


def divergence_gap(observed: Vector, *, method: str = "squared_euclidean",
                   alpha: float = 1.0) -> float:
    """Bregman gap between observed executable prices and their simplex projection.

    Projects ``observed`` onto the probability simplex and returns the chosen
    Bregman divergence between the two. The gap is ``0`` (within numerical
    tolerance) when the observed vector already lies on the simplex, and strictly
    positive otherwise — the core "is this mispriced?" signal.
    """
    observed = [float(x) for x in observed]
    proj = project_to_simplex(observed)
    if method == "squared_euclidean":
        gap = squared_euclidean(observed, proj)
    elif method == "kl":
        gap = kl_divergence(observed, proj)
    elif method == "generalized_kl":
        gap = generalized_kl_divergence(observed, proj)
    elif method == "generalized_entropy":
        gap = generalized_entropy_divergence(observed, proj, alpha=alpha)
    else:
        raise ValueError(f"unknown divergence method: {method!r}")
    logger.debug("divergence_gap method=%s sum=%.6f gap=%.8f", method,
                 sum(observed), gap)
    return float(gap)
