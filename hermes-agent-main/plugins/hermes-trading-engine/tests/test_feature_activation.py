"""Tests for the Pass-1 feature-activation audit instrumentation (read-only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from engine.feature_activation import (
    FEATURES,
    build_feature_activation,
    to_markdown,
)
import feature_activation_audit as audit_cli  # noqa: E402

_REQUIRED_ROWS = {
    "Raw ABCAS/Bregman scanner", "Trainer Bregman certifier",
    "Bregman paper execution", "Graph grouping (groups_from_graph)",
    "Profitability-first ranking", "Active learning selector",
    "Random/hash exploration", "Cluster/correlation gate",
    "Paper fill realism (slippage/depth)", "Stale-book rejection",
    "Reference-price fill fallback", "Spread/depth gates", "Ambiguity gate",
    "Chainlink conditioning", "News/research/model overlay",
    "Grok/LLM reasoning overlay", "Profitability governor",
    "Position/open-slot governor", "Stop-loss/take-profit/settlement handling",
}


def test_all_required_rows_present():
    names = {f["feature"] for f in FEATURES}
    assert _REQUIRED_ROWS <= names


def test_every_feature_has_schema_fields():
    for f in FEATURES:
        for k in ("feature", "files", "runtime_status", "controls_trades",
                  "telemetry_only", "flag", "evidence", "risk"):
            assert k in f, (f["feature"], k)
        assert f["runtime_status"] in ("active", "telemetry", "annotated",
                                       "imported", "dead")


def test_audit_classifies_abcas_scanner_as_telemetry_only():
    audit = build_feature_activation()
    assert "Raw ABCAS/Bregman scanner" in audit["summary"]["telemetry_only"]
    # the trainer Bregman execution is the one that can open trades
    assert "Bregman paper execution" in audit["summary"]["truly_active"]


def test_audit_exploitation_and_learning_active():
    # Pass-5 wired profitability ranking + governor; Pass-6 wired active learning
    # as the exploration authority and disabled random/hash exploration.
    audit = build_feature_activation()
    active = audit["summary"]["truly_active"]
    assert "Profitability-first ranking" in active
    assert "Profitability governor" in active
    assert "Active learning selector" in active
    dead = audit["summary"]["dead_or_unused"]
    assert "Random/hash exploration" in dead


def test_audit_flags_pnl_inflation_risks():
    risks = build_feature_activation()["summary"]["pnl_inflation_risks"]
    assert "Paper fill realism (slippage/depth)" in risks
    assert "Reference-price fill fallback" in risks


def test_top_edge_leaks_ranked_and_nonempty():
    leaks = build_feature_activation()["top_edge_leaks"]
    assert len(leaks) >= 10
    assert [x["rank"] for x in leaks] == sorted(x["rank"] for x in leaks)
    # the #1 leak is the shortlist-vs-catalog input universe
    assert "shortlist" in leaks[0]["leak"].lower()


def test_pass2_recommendation_has_preconditions():
    p2 = build_feature_activation()["pass2_recommendation"]
    assert p2["recommended"] is True
    assert any("full" in c.lower() and "catalog" in c.lower() for c in p2["preconditions"])


def test_markdown_renders_table_and_leaks():
    md = to_markdown(build_feature_activation())
    assert "Runtime feature truth table" in md
    assert "Top 10 edge leaks" in md
    assert "Pass 2 recommendation" in md


def test_pass2_status_proves_wiring():
    p2 = build_feature_activation()["pass2_status"]
    assert p2["wired"] is True
    assert p2["bregman_sees_full_raw_catalog"] is True
    assert p2["bregman_execution_priority_before_directional"] is True
    assert "POLYMARKET_BREGMAN_DISCOVERY_LIMIT" in p2["new_env_flags"]


def test_pass2_input_universe_now_active():
    feats = {f["feature"]: f for f in FEATURES}
    assert feats["Bregman INPUT UNIVERSE (catalog vs shortlist)"]["runtime_status"] == "active"
    assert "pass2" in feats["Bregman paper execution"]


def test_pass2_markdown_section_present():
    md = to_markdown(build_feature_activation())
    assert "Pass 2 — wired" in md
    assert "Bregman sees full raw catalog: **True**" in md


def test_pass3_status_proves_realism():
    p3 = build_feature_activation()["pass3_status"]
    assert p3["hardened"] is True
    assert p3["reference_price_fills_allowed_for_exploit_validation"] is False
    assert p3["missing_ask_fallback_allowed"] is False
    assert p3["stale_book_fills_allowed"] is False
    assert p3["offline_stub_fills_count_as_real_pnl"] is False
    assert p3["bregman_requires_all_executable_legs"] is True
    assert p3["realistic_executable_trades_separated_from_shadow"] is True
    assert p3["readiness_excludes_unrealistic_fills"] is True


def test_pass3_markdown_section_present():
    md = to_markdown(build_feature_activation())
    assert "Pass 3 — paper execution realism" in md
    assert "Readiness excludes unrealistic fills: **True**" in md


def test_pass4_status_proves_priority():
    p4 = build_feature_activation()["pass4_status"]
    assert p4["bregman_priority_enabled"] is True
    assert p4["bregman_execution_before_directional"] is True
    assert p4["directional_secondary_after_bregman"] is True
    assert p4["exploration_tertiary_after_exploit"] is True
    assert p4["paper_realism_still_enforced"] is True
    assert p4["reservation"]["POLYMARKET_BREGMAN_RESERVE_OPEN_SLOTS"] == 3


def test_pass4_markdown_section_present():
    md = to_markdown(build_feature_activation())
    assert "Pass 4 — Bregman-first strategy priority" in md
    assert "Bregman execution before directional: **True**" in md


def test_pass5_status_proves_profitability_first():
    p5 = build_feature_activation()["pass5_status"]
    assert p5["profitability_first_enabled"] is True
    assert p5["profitability_annotation_before_truncation"] is True
    assert p5["directional_ranked_by_after_cost_ev"] is True
    assert p5["bregman_ranked_by_after_cost_profit_roi"] is True
    assert p5["negative_after_cost_cannot_count_as_edge"] is True
    assert p5["profitability_governor_active_hard_gate"] is True
    assert p5["bregman_first_priority_preserved"] is True


def test_pass5_markdown_section_present():
    md = to_markdown(build_feature_activation())
    assert "Pass 5 — profitability-first ranking" in md
    assert "Directional ranked by after-cost EV: **True**" in md


def test_pass6_status_proves_active_learning():
    p6 = build_feature_activation()["pass6_status"]
    assert p6["active_learning_is_exploration_authority"] is True
    assert p6["random_hash_exploration_opens_trades"] is False
    assert p6["exploration_requires_paper_realism"] is True
    assert p6["exploration_excluded_from_readiness"] is True
    assert p6["exploration_cannot_consume_bregman_reserved_capacity"] is True
    assert p6["bregman_first_priority_preserved"] is True


def test_pass6_markdown_section_present():
    md = to_markdown(build_feature_activation())
    assert "Pass 6 — profitability-aware active learning" in md
    assert "Random/hash exploration opens trades: **False**" in md


def test_cli_writes_json_and_markdown(tmp_path):
    audit = audit_cli.generate(out_dir=str(tmp_path))
    j = tmp_path / "metrics" / "feature_activation.json"
    m = tmp_path / "reports" / "feature_activation_audit.md"
    assert j.is_file() and m.is_file()
    loaded = json.loads(j.read_text())
    assert loaded["paper_only"] is True
    assert loaded["features"] and "top_edge_leaks" in loaded


def test_live_config_refinement_is_readonly():
    # passing a config must not change the traced verdicts, only annotate live flags
    class _Cfg:
        bregman_execution_enabled = True
        realistic_fill_enabled = False
        reject_on_stale_book = True
        exploration_enabled = True
    audit = build_feature_activation(cfg=_Cfg())
    assert audit["live_config"]["realistic_fill_enabled"] is False
    assert audit["live_config"]["reject_on_stale_book"] is True
