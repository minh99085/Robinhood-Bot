"""Campaign-safe institutional profile (PAPER ONLY, fail-closed).

Quant scope — *Compliance/Security/Operational Excellence*: a single profile that
turns on all read-only learning + realism features for the institutional paper
campaign while keeping EVERY live-money path disabled and fail-closed. It is NOT
a global production default.
"""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path

from engine.training.campaign_controller import campaign_safety_check
from engine.training.config import FORBIDDEN_LIVE_FLAGS, TrainingConfig

_ROOT = Path(__file__).resolve().parents[1]


def _clean_live_env(monkeypatch):
    for f in FORBIDDEN_LIVE_FLAGS:
        monkeypatch.delenv(f, raising=False)
    for k in ("HTE_AUTOTRADE", "BTC_AUTOTRADE_ENABLED", "ARB_EXECUTION_ENABLED"):
        monkeypatch.delenv(k, raising=False)


def test_institutional_defaults_shape():
    c = TrainingConfig.institutional_campaign_defaults()
    assert c.campaign_safe_profile is True
    assert c.campaign_enabled is True
    assert c.algorithm_freeze_mode is True
    assert c.aggressive_can_promote_params is False
    # aggressive paper learning features ON
    assert c.exploration_enabled is True
    assert c.experiments_enabled is True
    assert c.chainlink_enabled is True
    # realism + read-only + guards ON
    assert c.clob_enabled is True and c.clob_read_only is True
    assert c.chainlink_read_only is True
    assert c.realistic_fill_enabled is True
    assert c.allow_pm_reference_price_fills is False
    assert c.reject_on_stale_book is True
    assert c.clean_label_guard is True
    assert c.risk_engine_enabled is True
    assert c.disable_btc_pulse_trading is True
    # strictly paper
    assert c.is_paper_only is True
    assert c.mode == "paper_train"


def test_not_a_global_default():
    c = TrainingConfig()
    assert c.campaign_safe_profile is False
    assert c.realistic_fill_enabled is False
    assert c.campaign_enabled is False


def test_post_init_forces_safe_invariants_even_if_constructed_unsafe():
    # someone tries to construct the safe profile but flip guards off
    c = TrainingConfig(campaign_safe_profile=True, clob_enabled=True, clob_read_only=False,
                       chainlink_enabled=True, chainlink_read_only=False,
                       realistic_fill_enabled=False, clean_label_guard=False,
                       algorithm_freeze_mode=False, allow_pm_reference_price_fills=True,
                       risk_engine_enabled=False)
    assert c.clob_read_only is True
    assert c.chainlink_read_only is True
    assert c.realistic_fill_enabled is True
    assert c.clean_label_guard is True
    assert c.algorithm_freeze_mode is True
    assert c.allow_pm_reference_price_fills is False
    assert c.risk_engine_enabled is True
    assert c.aggressive_can_promote_params is False


def test_env_profile_resolves_to_safe_defaults(monkeypatch):
    _clean_live_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_CAMPAIGN_SAFE_PROFILE", "1")
    c = TrainingConfig.from_env()
    assert c.campaign_safe_profile is True
    assert c.clob_read_only is True
    assert c.chainlink_read_only is True
    assert c.realistic_fill_enabled is True
    assert c.clean_label_guard is True
    assert c.algorithm_freeze_mode is True
    assert c.campaign_enabled is True


def test_safety_check_passes_for_safe_profile(monkeypatch):
    _clean_live_env(monkeypatch)
    rep = campaign_safety_check(TrainingConfig.institutional_campaign_defaults())
    assert rep["passed"] is True
    assert rep["fail_closed_reason"] is None
    assert rep["startup_safety_passed"] is True
    for k in ("campaign_safe_profile", "clob_read_only_enabled", "chainlink_read_only_enabled",
              "realistic_fill_enabled", "clean_label_guard_enabled", "live_disabled",
              "micro_live_disabled", "guarded_live_disabled", "btc_autotrade_disabled",
              "risk_gates_required"):
        assert rep[k] is True


def test_start_script_campaign_safe_profile_prints_and_status_shows(tmp_path, monkeypatch):
    _clean_live_env(monkeypatch)
    monkeypatch.setenv("HTE_MODE", "paper")
    spec = importlib.util.spec_from_file_location(
        "start_camp_safe", _ROOT / "scripts" / "start_polymarket_paper_training.py")
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)
    rc = start.run(["--campaign-safe-profile", "--campaign", "--catalog", "synthetic",
                    "--max-ticks", "1", "--data-dir", str(tmp_path)])
    assert rc == 0
    status = json.loads((tmp_path / "polymarket_training.json").read_text())
    cs = status.get("campaign_safety", {})
    assert cs.get("campaign_safe_profile") is True
    assert cs.get("startup_safety_passed") is True
    assert cs.get("live_disabled") is True
