"""Tests for the institutional validation contract + production-readiness verdict."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_bot_inspection_report as gen  # noqa: E402
from engine.validation_contract import (
    CONTRACT_CONDITIONS,
    build_validation_contract,
    calibration_adjusted_ev,
    credible_positive_expectancy,
    production_readiness_verdict,
)


def _passing_feats():
    return {
        "tests_passing": True, "bregman_enabled": True, "bregman_paper_enabled": True,
        "bregman_constraint_groups_scanned": 30, "fill_realism_enabled": True,
        "fantasy_fill_rejections": 2, "after_cost_pnl": 1.5,
        "btc_pulse_after_cost_pnl": -0.5, "btc_pulse_gate_enabled": True,
    }


def _ok_reconciliation():
    return {"ok": True, "max_rel_diff_pct": 0.2}


# --- contract conditions ----------------------------------------------------
def test_contract_passes_when_all_conditions_hold():
    c = build_validation_contract(_passing_feats(), ledger_reconciliation=_ok_reconciliation())
    assert c["passed"] is True
    assert c["failed"] == []
    assert {ch["name"] for ch in c["checks"]} == set(CONTRACT_CONDITIONS)


def test_contract_fails_on_red_pytest():
    f = _passing_feats(); f["tests_passing"] = False
    c = build_validation_contract(f, ledger_reconciliation=_ok_reconciliation())
    assert c["passed"] is False and "pytest_green" in c["failed"]


def test_contract_fails_on_zero_groups():
    f = _passing_feats(); f["bregman_constraint_groups_scanned"] = 0
    c = build_validation_contract(f, ledger_reconciliation=_ok_reconciliation())
    assert "groups_scanned_positive" in c["failed"]


def test_contract_fails_on_unreconciled_ledger():
    c = build_validation_contract(_passing_feats(), ledger_reconciliation={"ok": False})
    assert "ledger_reconciled" in c["failed"]


def test_contract_fails_when_pulse_negative_and_not_gated():
    f = _passing_feats(); f["btc_pulse_gate_enabled"] = False  # negative + ungated
    c = build_validation_contract(f, ledger_reconciliation=_ok_reconciliation())
    assert "btc_pulse_gated_when_negative" in c["failed"]


def test_contract_fails_on_missing_after_cost():
    f = _passing_feats(); f["after_cost_pnl"] = None
    c = build_validation_contract(f, ledger_reconciliation=_ok_reconciliation())
    assert "after_cost_pnl_populated" in c["failed"]


# --- expectancy + readiness verdict -----------------------------------------
def test_credible_positive_expectancy_true_for_positive_returns():
    exp = credible_positive_expectancy([0.05, 0.04, 0.06, 0.05, 0.05, 0.04, 0.05])
    assert exp["credible_positive"] is True and exp["lo"] > 0


def test_credible_positive_expectancy_false_when_ci_includes_zero():
    exp = credible_positive_expectancy([0.1, -0.1, 0.1, -0.1, 0.05, -0.05])
    assert exp["credible_positive"] is False


def test_expectancy_insufficient_samples():
    exp = credible_positive_expectancy([0.1, 0.2])
    assert exp["credible_positive"] is False and exp["reason"] == "insufficient_samples"


def test_production_ready_requires_contract_and_expectancy():
    passing = {"passed": True, "failed": []}
    credible = {"credible_positive": True}
    assert production_readiness_verdict(passing, credible)["production_ready"] is True
    # contract fails -> not ready
    v = production_readiness_verdict({"passed": False, "failed": ["pytest_green"]}, credible)
    assert v["production_ready"] is False
    # no credible expectancy -> not ready
    v2 = production_readiness_verdict(passing, {"credible_positive": False})
    assert v2["production_ready"] is False
    assert "no_credible_positive_after_cost_expectancy" in v2["blocking_reasons"]


def test_calibration_adjusted_ev_shrinks_with_ece():
    assert calibration_adjusted_ev(1.0, 0.0) == 1.0
    assert calibration_adjusted_ev(1.0, 0.5) == 0.5
    assert calibration_adjusted_ev(None, 0.1) is None


# --- report wiring ----------------------------------------------------------
def _runner(cmd, cwd, timeout):
    if cmd[:1] == ["git"]:
        return (0, "git", "")
    if "pytest" in cmd:
        return (0, "1 passed", "")
    return (0, "", "")


def test_report_includes_contract_and_withholds_readiness(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    # status with no Bregman activity / no after-cost -> contract fails
    (data_dir / "polymarket_training.json").write_text(json.dumps({
        "mode": "paper", "pnl": {"equity": 500.0}, "safety": {"ok": True}}),
        encoding="utf-8")
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), data_dir=str(data_dir),
        skip_tests=True, include_docker=False, include_api=False, include_artifacts=False,
        runner=_runner, opener=lambda u, t: (0, "x"))
    assert res["validation_contract_passed"] is False
    assert res["production_ready"] is False
    report = json.loads((Path(res["bundle_dir"]) / "report.json").read_text())
    assert "validation_contract" in report
    assert "production_readiness_verdict" in report
    md = (Path(res["bundle_dir"]) / "report.md").read_text()
    assert "Validation Contract" in md
