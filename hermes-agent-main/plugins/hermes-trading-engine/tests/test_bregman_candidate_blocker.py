"""Candidate-generation blocker taxonomy + synthetic fixture + malformed reconcile.

Proves the remaining report contradictions are impossible:
* blocker is NEVER ``no_depth_sufficient_groups`` when depth-sufficient groups exist;
* a depth-sufficient but negative-edge universe reports the correct precise blocker;
* the known-good synthetic fixture generates binary + multi-way candidates and rejects
  invalid cases WITHOUT loosening gates / enabling live / contaminating real metrics;
* malformed-group summary reconciles with the diagnostic tail.
"""

import tempfile

from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
from engine.training.bregman_fixture import run_bregman_synthetic_fixture


def _trainer(tmp_path, monkeypatch):
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)


def _binary(gid, mid, ask, depth):
    return SimplexGroup(gid, "binary_yes_no",
                        [SimplexLeg(mid, "YES", mid + "_Y", ask=ask, bid=ask - 0.01,
                                    depth_usd=depth, fresh_book=True),
                         SimplexLeg(mid, "NO", mid + "_N", ask=ask, bid=ask - 0.01,
                                    depth_usd=depth, fresh_book=True)],
                        mutually_exclusive=True, exhaustive=True)


def test_blocker_never_no_depth_sufficient_when_depth_sufficient_exists(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    req = float(t.bregman.min_depth_usd)
    # depth-sufficient ($4x) but negative edge (0.6+0.6 > $1) + a thin group
    g_ds = _binary("ds", "m_ds", 0.6, req * 4)
    g_thin = _binary("thin", "m_t", 0.45, 5.0)
    certs = t.bregman.certify_all([g_ds, g_thin])
    blk = t._bregman_candidate_blocker([g_ds, g_thin], certs, 0)
    dep = t._bregman_depth_telemetry([g_ds, g_thin])
    assert dep["bregman_depth_sufficient_groups"] >= 1
    assert blk["bregman_candidate_generation_blocker"] != "no_depth_sufficient_groups"
    assert blk["bregman_real_market_zero_candidate_reason"] == \
        "no_positive_after_cost_lower_bound_among_depth_sufficient_groups"


def test_depth_sufficient_but_negative_edge_fields(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    req = float(t.bregman.min_depth_usd)
    g = _binary("ds", "m_ds", 0.6, req * 4)
    certs = t.bregman.certify_all([g])
    blk = t._bregman_candidate_blocker([g], certs, 0)
    assert blk["bregman_depth_sufficient_but_negative_edge_count"] == 1
    assert blk["bregman_best_depth_sufficient_group_lower_bound"] is not None
    assert blk["bregman_best_depth_sufficient_group_lower_bound"] < 0
    assert blk["bregman_best_depth_sufficient_group_reject_reason"] == "no_positive_edge"
    brg = blk["bregman_best_real_group_summary"]
    assert brg["depth_sufficient"] is True
    assert brg["market_ids"] == ["m_ds"]
    assert brg["outcome_labels"] == ["YES", "NO"]


def test_thin_only_universe_reports_no_depth_sufficient(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    g = _binary("thin", "m_t", 0.45, 5.0)         # all thin
    certs = t.bregman.certify_all([g])
    blk = t._bregman_candidate_blocker([g], certs, 0)
    assert blk["bregman_real_market_zero_candidate_reason"] == "no_depth_sufficient_groups"


def test_real_market_zero_candidate_reason_counts_present(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    req = float(t.bregman.min_depth_usd)
    groups = [_binary("ds", "m_ds", 0.6, req * 4), _binary("thin", "m_t", 0.45, 5.0)]
    certs = t.bregman.certify_all(groups)
    blk = t._bregman_candidate_blocker(groups, certs, 0)
    counts = blk["bregman_real_market_zero_candidate_reason_counts"]
    assert counts and sum(counts.values()) == 2


# --- synthetic fixture proof ----------------------------------------------- #
def test_synthetic_fixture_generates_candidates_and_rejects_invalid():
    r = run_bregman_synthetic_fixture()
    assert r["bregman_synthetic_fixture_passed"] is True
    assert r["synthetic_binary_candidate_generated"] is True
    assert r["synthetic_multiway_candidate_generated"] is True
    assert r["synthetic_invalid_cases_rejected"] is True
    assert all(r["synthetic_invalid_case_results"].values())   # each invalid rejected


def test_synthetic_fixture_does_not_loosen_gates_or_enable_live():
    r = run_bregman_synthetic_fixture()
    assert r["synthetic_fixture_gate_loosening"] is False
    assert r["synthetic_fixture_live_trading_enabled"] is False
    assert r["synthetic_fixture_contaminated_real_metrics"] is False
    assert r["synthetic_fixture_required_depth_usd"] == 50.0     # default, not lowered
    assert r["synthetic_fixture_max_spread"] == 0.08


def test_synthetic_fixture_does_not_touch_real_trainer_metrics(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    before = dict(t.bregman_exec_metrics)
    run_bregman_synthetic_fixture()
    assert dict(t.bregman_exec_metrics) == before     # no contamination
    assert t.open_positions() == [] if hasattr(t, "open_positions") else True


# --- malformed reconciliation ---------------------------------------------- #
def test_malformed_group_reconciliation_no_contradiction():
    from scripts.generate_bot_inspection_report import _reconcile_malformed_groups
    # summary says 0 (trainer certifier) but scanner skip_reasons has malformed groups
    status = {"bregman_funnel": {"malformed_group_count": 0},
              "bregman": {"skip_reasons": {"malformed_group": 7}}}
    _reconcile_malformed_groups(status, None)
    f = status["bregman_funnel"]
    assert f["bregman_malformed_group_runtime_count"] == 7
    assert f["bregman_malformed_group_reported_count"] == 0
    # reconciled total must NOT be 0 while the scanner saw malformed groups
    assert f["malformed_group_count"] == 7
    assert f["bregman_malformed_group_source"] == "abcas_scanner_path_real_rejects"


def test_malformed_reconciliation_clean_when_none():
    from scripts.generate_bot_inspection_report import _reconcile_malformed_groups
    status = {"bregman_funnel": {"malformed_group_count": 0}, "bregman": {}}
    _reconcile_malformed_groups(status, None)
    assert status["bregman_funnel"]["malformed_group_count"] == 0
    assert status["bregman_funnel"]["bregman_malformed_group_source"] == "none"
