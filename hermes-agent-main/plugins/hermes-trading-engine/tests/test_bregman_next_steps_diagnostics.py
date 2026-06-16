"""Fix 4: Bregman next-step diagnostics (read-only; certification stays strict).

The funnel must surface the NEAREST-opportunity views — closest-to-positive
complete depth-sufficient near-misses, top groups rejected by stale book, top
groups rejected by thin depth, and whether any certified executable bundle exists
(with the exact reason if none) — WITHOUT loosening completeness/certification.
"""

from __future__ import annotations

from engine.training.inspection_summary import build_bregman_funnel


def _nm(mid, lb, *, reject_reason="ok", complete=True, thin_legs=0, stale_legs=0,
        min_depth=100.0):
    return {
        "group_id": mid, "after_cost_lower_bound": lb, "reject_reason": reject_reason,
        "completeness": {"completeness_proven": complete},
        "depth_quality": {"thin_legs": thin_legs, "min_leg_depth_usd": min_depth},
        "freshness": {"stale_legs": stale_legs},
        "near_miss_tradeable": False,
    }


def _telemetry(near_misses, *, certified=0, executable=0):
    return {
        "groups_discovered": 5, "constraint_groups_scanned": 5,
        "certified_arbitrages": certified, "executable_depth_certified": executable,
        "bregman_top_near_misses": near_misses,
        "best_complete_group_lower_bound": max(
            (n["after_cost_lower_bound"] for n in near_misses
             if n["completeness"]["completeness_proven"]), default=None),
        "bregman_zero_certified_explanation": (
            None if certified else "no_complete_family_passed_certification"),
    }


def test_next_steps_surfaces_nearest_opportunity_views():
    near = [
        _nm("complete_close", 0.004, complete=True, thin_legs=0),     # closest, executable-ish
        _nm("complete_far", -0.05, complete=True, thin_legs=0),
        _nm("stale", -0.01, reject_reason="stale_book", stale_legs=2),
        _nm("thin", -0.02, reject_reason="thin_depth", thin_legs=1, min_depth=3.0),
        _nm("incomplete", 0.02, complete=False),                       # NOT tradeable (incomplete)
    ]
    f = build_bregman_funnel(_telemetry(near, certified=0))
    ns = f["bregman_next_steps"]
    # closest-to-positive complete depth-sufficient near-miss is the top complete one
    top_complete = ns["top_complete_depth_sufficient_near_misses"]
    assert top_complete and top_complete[0]["group_id"] == "complete_close"
    # stale + thin reject views isolate the right groups
    assert any(r["group_id"] == "stale" for r in ns["top_groups_rejected_by_stale_book"])
    assert any(r["group_id"] == "thin" for r in ns["top_groups_rejected_by_thin_depth"])
    # the incomplete (raw-positive) group is NOT promoted as a complete near-miss
    assert all(r["group_id"] != "incomplete" for r in top_complete)


def test_next_steps_reports_no_certified_bundle_with_exact_reason():
    f = build_bregman_funnel(_telemetry([_nm("c", -0.01)], certified=0, executable=0))
    ns = f["bregman_next_steps"]
    assert ns["certified_executable_bundle_exists"] is False
    assert ns["certified_executable_bundle_reason"]
    assert ns["certification_strictness_preserved"] is True


def test_next_steps_reports_certified_bundle_when_present():
    f = build_bregman_funnel(_telemetry([_nm("c", 0.05)], certified=2, executable=1))
    ns = f["bregman_next_steps"]
    assert ns["certified_executable_bundle_exists"] is True
    assert "available" in ns["certified_executable_bundle_reason"]
