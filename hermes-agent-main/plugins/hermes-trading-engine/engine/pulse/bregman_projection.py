"""Bregman/KL projection for dependent market groups (Roan Layer 2).

Single 2-outcome windows: skip (trivial polytope). Dependency groups: KL distance + optional
Frank-Wolfe via ``frank_wolfe.run_barrier_frank_wolfe``.
"""

from __future__ import annotations

import math
from typing import Optional

def kl_divergence(p: float, q: float) -> Optional[float]:
    """KL(p || q) for binary probabilities (Bregman for LMSR)."""
    try:
        eps = 1e-9
        p = max(eps, min(1.0 - eps, float(p)))
        q = max(eps, min(1.0 - eps, float(q)))
        return round(p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q)), 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def bregman_divergence_binary(p: float, q: float) -> Optional[float]:
    """Alias for KL on binary probabilities."""
    return kl_divergence(p, q)


def _project_implication_feasible(parent_p: float, child_p: float) -> tuple[float, float]:
    p = float(parent_p)
    c = float(child_p)
    if p >= c:
        return p, c
    return c, c


def projection_distance_nested(
    parent_mid: float,
    child_mid: float,
    *,
    epsilon: float = 0.02,
) -> dict:
    """KL-style projection distance for nested implication P(parent_up) >= P(child_up)."""
    p_mid = float(parent_mid)
    c_mid = float(child_mid)
    violation = max(0.0, c_mid - p_mid)
    fp, fc = _project_implication_feasible(p_mid, c_mid)
    kl_parent = kl_divergence(p_mid, fp) or 0.0
    kl_child = kl_divergence(c_mid, fc) or 0.0
    dist = round(kl_parent + kl_child, 6)
    max_theoretical = round(violation, 6)
    actionable = violation > float(epsilon)
    return {
        "constraint_type": "nested_implication",
        "projection_distance": dist,
        "max_theoretical_profit_per_share": max_theoretical,
        "feasible_parent_p": round(fp, 6),
        "feasible_child_p": round(fc, 6),
        "solver_status": "brute_force_lcmm",
        "convergence_reason": "closed_form_implication",
        "iterations": 1,
        "actionable_projection": actionable,
        "note": "Layer-2; VWAP path validates execution.",
    }


def project_dependency_group(
    parent_mid: float,
    child_mid: float,
    *,
    epsilon: float = 0.02,
    use_frank_wolfe: bool = True,
    fw_kwargs: Optional[dict] = None,
) -> dict:
    """Full projection pipeline for one nested pair (5m brain vs 15m hands)."""
    base = projection_distance_nested(parent_mid, child_mid, epsilon=epsilon)
    n_conditions = 2
    if n_conditions <= 2 and not use_frank_wolfe:
        return base

    prices = {"parent_up": parent_mid, "child_up": child_mid}
    constraints = [{
        "type": "nested_implication",
        "constraint_type": "nested_implication",
        "parent_key": "parent_up",
        "child_key": "child_up",
    }]
    from engine.pulse.frank_wolfe import run_barrier_frank_wolfe
    fw = run_barrier_frank_wolfe(prices, constraints, arb_epsilon=epsilon, **(fw_kwargs or {}))
    merged = {**base, **fw}
    merged["actionable_projection"] = (
        float(fw.get("projection_distance") or 0) > epsilon
        or base.get("actionable_projection", False)
    )
    return merged


def modified_kelly_arb_size_usd(
    *,
    edge_per_share: float,
    fill_probability: float,
    max_usd: float,
    depth_cap_usd: float,
) -> float:
    """Roan modified Kelly: f = (b*p - q)/b * sqrt(p); capped at depth."""
    b = max(1e-6, float(edge_per_share))
    p = max(0.0, min(1.0, float(fill_probability)))
    if b <= 0 or p <= 0:
        return 0.0
    q = 1.0 - p
    raw_frac = (b * p - q) / b
    if raw_frac > 0:
        f = raw_frac * math.sqrt(p)
    else:
        # Guaranteed arb edge: failed fill ≈ no trade, not a full Kelly loss.
        f = min(1.0, b) * math.sqrt(p)
    f = max(0.0, min(1.0, f))
    raw = f * float(max_usd)
    return round(min(raw, float(depth_cap_usd), float(max_usd)), 4)


def frank_wolfe_scaffold(
    prices: dict,
    constraints: list[dict],
    *,
    max_iterations: int = 10,
    alpha: float = 0.9,
) -> dict:
    """Backward-compatible scaffold wrapper."""
    from engine.pulse.frank_wolfe import run_barrier_frank_wolfe
    return run_barrier_frank_wolfe(
        prices, constraints, alpha=alpha, max_iterations=max_iterations)