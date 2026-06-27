"""Roan/Bregman phases 2-4 unit tests."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.bregman_projection import (
    modified_kelly_arb_size_usd,
    project_dependency_group,
)
from engine.pulse.constraint_registry import nested_implication_violation
from engine.pulse.frank_wolfe import run_barrier_frank_wolfe
from engine.pulse.ip_oracle import find_violating_vertex
from engine.pulse.walk_forward import passes_walk_forward

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_nested_implication_registry():
    v = nested_implication_violation(0.45, 0.58, epsilon=0.02)
    assert v is not None
    assert v["violation_magnitude"] == 0.13


def test_project_dependency_group_fixture():
    fx = json.loads((FIXTURES / "roan_nested_implication_violation.json").read_text())
    d = project_dependency_group(
        fx["hands_15m"]["up_mid"], fx["brain_5m"]["up_mid"], epsilon=0.02,
        use_frank_wolfe=True,
        fw_kwargs={"max_iterations": 5, "time_budget_ms": 200},
    )
    assert d["actionable_projection"] is True
    assert float(d.get("projection_distance") or 0) >= 0


def test_ip_oracle_nested_closed_form():
    r = find_violating_vertex(
        {"parent_up": 0.45, "child_up": 0.58},
        [{"type": "nested_implication", "parent_key": "parent_up", "child_key": "child_up"}],
        time_budget_ms=100,
    )
    assert r["status"] in ("ok", "feasible")
    assert r["backend"] == "closed_form"


def test_frank_wolfe_nested_pair():
    r = run_barrier_frank_wolfe(
        {"parent_up": 0.45, "child_up": 0.58},
        [{"type": "nested_implication", "parent_key": "parent_up", "child_key": "child_up"}],
        arb_epsilon=0.02,
        max_iterations=3,
        time_budget_ms=200,
    )
    assert r["iterations"] >= 1
    assert float(r["projection_distance"]) > 0


def test_modified_kelly_positive_edge():
    sz = modified_kelly_arb_size_usd(
        edge_per_share=0.1, fill_probability=0.9, max_usd=50, depth_cap_usd=25)
    assert sz > 0


def test_walk_forward_insufficient_data():
    r = passes_walk_forward([], min_holdout_n=10)
    assert r["passed"] is False