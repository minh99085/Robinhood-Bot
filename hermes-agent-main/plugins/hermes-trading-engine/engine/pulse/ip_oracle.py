"""Integer programming oracle for small dependent groups (Roan Frank-Wolfe inner loop).

Uses OR-Tools CP-SAT when available; falls back to closed-form LCMM for nested_implication.
"""

from __future__ import annotations

import time
from typing import Optional


def _ortools_available() -> bool:
    try:
        from ortools.sat.python import cp_model  # noqa: F401
        return True
    except ImportError:
        return False


def find_violating_vertex(
    prices: dict[str, float],
    constraints: list[dict],
    *,
    backend: str = "ortools",
    time_budget_ms: float = 500.0,
) -> dict:
    """Find a feasible vertex direction that maximizes linearized violation (FW inner oracle)."""
    t0 = time.perf_counter()
    deadline = t0 + max(0.01, float(time_budget_ms) / 1000.0)

    if not constraints:
        return {"status": "no_constraints", "vertex": None, "elapsed_ms": 0.0}

    ctype = str(constraints[0].get("type") or constraints[0].get("constraint_type") or "")
    if ctype == "nested_implication":
        parent_p = float(prices.get("parent_up") or 0)
        child_p = float(prices.get("child_up") or 0)
        if child_p > parent_p:
            return {
                "status": "ok",
                "vertex": {"parent_up": child_p, "child_up": child_p},
                "violation": round(child_p - parent_p, 6),
                "backend": "closed_form",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }
        return {
            "status": "feasible",
            "vertex": None,
            "violation": 0.0,
            "backend": "closed_form",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    use_ortools = backend == "ortools" and _ortools_available()
    if not use_ortools:
        return {
            "status": "fallback",
            "vertex": None,
            "violation": 0.0,
            "backend": "none",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "note": "ortools_unavailable_or_unsupported_constraint",
        }

    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    # Scaled integers 0..100 for probabilities
    keys = sorted(prices.keys())
    vars_map = {k: model.NewIntVar(0, 100, k) for k in keys}
    for c in constraints:
        if c.get("type") == "sum_to_one" and c.get("keys"):
            model.Add(sum(vars_map[k] for k in c["keys"] if k in vars_map) == 100)
        if c.get("type") == "nested_implication":
            pk, ck = c.get("parent_key"), c.get("child_key")
            if pk in vars_map and ck in vars_map:
                model.Add(vars_map[pk] >= vars_map[ck])

    # Maximize sum of deviations from current (greedy proxy)
    obj_terms = []
    for k in keys:
        cur = int(round(float(prices.get(k) or 0) * 100))
        obj_terms.append(vars_map[k] - cur)
    if obj_terms:
        model.Maximize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.01, deadline - time.perf_counter())
    result = solver.Solve(model)
    if result not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "status": "timeout_or_infeasible",
            "vertex": None,
            "backend": "ortools",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    vertex = {k: round(solver.Value(vars_map[k]) / 100.0, 6) for k in keys}
    return {
        "status": "ok",
        "vertex": vertex,
        "backend": "ortools",
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
    }