"""Tests for the inspection live-execution safety audit."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspection_safety_audit as sa  # noqa: E402


def test_clean_paper_config_is_ok():
    env = {
        "HTE_MODE": "paper",
        "MICRO_LIVE_ENABLED": "0",
        "GUARDED_LIVE_ENABLED": "0",
        "BTC_PULSE_LIVE_ENABLED": "0",
        "BTC_AUTOTRADE_ENABLED": "0",
    }
    res = sa.audit(env=env)
    assert res["status"] == "OK"
    assert res["critical"] is False
    assert res["live_detected"] is False


def test_forbidden_live_flag_enabled_is_critical():
    res = sa.audit(env={"HTE_MODE": "paper", "MICRO_LIVE_ENABLED": "1"})
    assert res["status"] == "CRITICAL"
    assert res["critical"] is True
    assert "MICRO_LIVE_ENABLED" in res["summary"]["forbidden_enabled"]


def test_production_execution_flag_is_critical():
    res = sa.audit(env={"PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION": "true"})
    assert res["critical"] is True
    assert "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION" in res["summary"]["forbidden_enabled"]


def test_live_credential_presence_is_critical():
    res = sa.audit(env={"HTE_MODE": "paper",
                        "POLYMARKET_PRIVATE_KEY": "0xabc123deadbeef"})
    assert res["critical"] is True
    assert "POLYMARKET_PRIVATE_KEY" in res["summary"]["credentials_present"]


def test_empty_credential_placeholder_is_not_present():
    # docker-compose interpolation default that resolves empty must NOT trip.
    res = sa.audit(compose_env={"POLYMARKET_API_SECRET": "${POLYMARKET_API_SECRET:-}"})
    assert res["critical"] is False
    assert res["summary"]["credentials_present"] == []


def test_hte_autotrade_paper_is_warning_not_critical():
    # Paper-simulation naming must NOT be a live-execution failure.
    res = sa.audit(env={"HTE_MODE": "paper", "HTE_AUTOTRADE": "1"})
    assert res["critical"] is False
    assert res["status"] in ("OK", "WARN")
    # It is recorded as INFO, never a forbidden-enabled critical.
    assert "HTE_AUTOTRADE" not in res["summary"]["forbidden_enabled"]


def test_non_paper_mode_is_critical():
    res = sa.audit(env={"HTE_MODE": "live"})
    assert res["critical"] is True
    assert res["live_detected"] is True


def test_paper_train_mode_is_not_live():
    # paper_train is the PAPER training mode — must NOT be flagged as live.
    res = sa.audit(status={"mode": "paper_train", "safety": {"live_detected": False}})
    assert res["critical"] is False
    assert res["live_detected"] is False
    assert res["status"] == "OK"


def test_observe_and_replay_and_shadow_modes_are_not_live():
    for m in ("observe_only", "replay", "shadow", "shadow_live", "disabled"):
        res = sa.audit(status={"mode": m})
        assert res["critical"] is False, m


def test_is_live_mode_helper():
    assert sa.is_live_mode("live") is True
    assert sa.is_live_mode("guarded_live") is True
    assert sa.is_live_mode("production") is True
    assert sa.is_live_mode("paper") is False
    assert sa.is_live_mode("paper_train") is False
    assert sa.is_live_mode("observe_only") is False
    assert sa.is_live_mode("shadow_live") is False
    assert sa.is_live_mode("") is False


def test_protective_flag_disabled_with_guarded_live_on_is_critical():
    res = sa.audit(env={"GUARDED_LIVE_ENABLED": "1",
                        "GUARDED_LIVE_BLOCK_SIGNING": "0"})
    assert res["critical"] is True


def test_protective_flag_disabled_with_guarded_live_off_is_warn():
    res = sa.audit(env={"GUARDED_LIVE_ENABLED": "0",
                        "GUARDED_LIVE_BLOCK_SIGNING": "0"})
    assert res["critical"] is False
    assert res["warn"] is True


def test_real_env_overrides_compose_default():
    # compose says enabled via default, but a real .env value of 0 must win.
    res = sa.audit(env={"MICRO_LIVE_ENABLED": "0"},
                   compose_env={"MICRO_LIVE_ENABLED": "1"})
    assert res["critical"] is False


def test_parse_env_assignments_strips_quotes_and_comments():
    text = '# comment\nFOO="bar"\nBAZ=qux  # inline\nexport KEY=val\n'
    parsed = sa.parse_env_assignments(text)
    assert parsed["FOO"] == "bar"
    assert parsed["BAZ"] == "qux"
    assert parsed["KEY"] == "val"
