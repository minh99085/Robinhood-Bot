"""Barrier Frank-Wolfe scaffold for Bregman projection on small dependent groups (Roan Layer 2)."""

from __future__ import annotations

import time
from typing import Optional

from engine.pulse.bregman_projection import projection_distance_nested
from engine.pulse.ip_oracle import find_violating_vertex

_DEPENDENCY_CONSTRAINT_TYPES = frozenset({
    "nested_implication", "implication", "mutex", "at_least_one",
})


def _has_dependency_constraints(constraints: list[dict]) -> bool:
    for c in constraints:
        t = str(c.get("type") or c.get("constraint_type") or "")
        if t in _DEPENDENCY_CONSTRAINT_TYPES:
            return True
    return False


def run_barrier_frank_wolfe(
    prices: dict[str, float],
    constraints: list[dict],
    *,
    alpha: float = 0.9,
    epsilon_init: float = 0.1,
    max_iterations: int = 50,
    converge_threshold: float = 1e-6,
    time_budget_ms: float = 500.0,
    ip_backend: str = "ortools",
    arb_epsilon: float = 0.02,
) -> dict:
    """Iterative FW with barrier shrink; returns projection diagnostics + optional trade vector."""
    t0 = time.perf_counter()
    deadline = t0 + max(0.01, float(time_budget_ms) / 1000.0)
    if not _has_dependency_constraints(constraints):
        return {
            "solver_status": "skipped_trivial",
            "convergence_reason": "single_window_no_bregman",
            "iterations": 0,
            "projection_distance": 0.0,
            "optimal_trade_vector": None,
        }

    barrier = float(epsilon_init)
    best_dist = 0.0
    best_trade: Optional[dict] = None
    iterations = 0
    active_vertices: list = []
    last_oracle = {}

    parent_up = prices.get("parent_up")
    child_up = prices.get("child_up")
    if parent_up is not None and child_up is not None:
        base = projection_distance_nested(parent_up, child_up, epsilon=arb_epsilon)
        best_dist = float(base.get("projection_distance") or 0.0)
        if base.get("actionable_projection"):
            best_trade = {
                "parent_up_target": base.get("feasible_parent_p"),
                "child_up_target": base.get("feasible_child_p"),
                "max_theoretical_profit_per_share": base.get("max_theoretical_profit_per_share"),
            }

    for it in range(max(1, int(max_iterations))):
        if time.perf_counter() >= deadline:
            break
        iterations = it + 1
        oracle = find_violating_vertex(
            prices, constraints, backend=ip_backend,
            time_budget_ms=max(10.0, time_budget_ms / max_iterations))
        last_oracle = oracle
        if oracle.get("vertex"):
            active_vertices.append(oracle["vertex"])
        viol = float(oracle.get("violation") or 0.0)
        dist = float(alpha) * viol
        if dist > best_dist:
            best_dist = dist
        barrier *= 0.95
        if viol < float(converge_threshold):
            break

    elapsed = round((time.perf_counter() - t0) * 1000, 2)
    status = "converged" if iterations and best_dist > arb_epsilon else "scaffold_only"
    if last_oracle.get("status") == "timeout_or_infeasible":
        status = "timeout"

    return {
        "projection_distance": round(best_dist, 6),
        "solver_status": status,
        "convergence_reason": last_oracle.get("status", "done"),
        "iterations": iterations,
        "active_vertices": active_vertices[-5:],
        "alpha": alpha,
        "barrier_epsilon_final": round(barrier, 6),
        "optimal_trade_vector": best_trade,
        "elapsed_ms": elapsed,
        "ip_backend": last_oracle.get("backend", ip_backend),
    }