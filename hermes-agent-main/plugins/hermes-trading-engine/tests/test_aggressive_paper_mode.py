"""Tests for AGGRESSIVE_PAPER_TRAINING mode + the global paper-only safety lock.

Proves real execution is impossible in aggressive paper mode and the activation
fails closed when any real-money flag is on.
"""

from __future__ import annotations

import pytest

from engine.aggressive_paper import (
    AGGRESSIVE_PAPER_DEFAULTS,
    FORBIDDEN_LIVE_FLAGS,
    PAPER_ONLY_LOCKS,
    AggressivePaperUnsafe,
    aggressive_paper_proof,
    apply_aggressive_paper_env,
    assert_paper_only,
    enabled_live_flags,
    is_aggressive_paper,
    real_execution_possible,
)


def test_aggressive_paper_proof_block_after_apply():
    env = {}
    apply_aggressive_paper_env(env)
    proof = aggressive_paper_proof(env)
    assert proof["aggressive_paper_training_enabled"] is True
    assert proof["feedback_accelerator_enabled"] is True
    assert proof["feedback_accelerator_target_multiplier"] == 100   # 100X profile
    assert proof["paper_profit_discovery_profile_enabled"] is True
    assert proof["real_execution_possible"] is False               # hard invariant
    assert proof["live_flags_forced_off"] is True


def test_proof_real_execution_impossible_even_if_live_flag_flips():
    env = {}
    apply_aggressive_paper_env(env)
    env["BTC_AUTOTRADE_ENABLED"] = "1"          # something flips a live flag later
    proof = aggressive_paper_proof(env)
    assert proof["real_execution_possible"] is False   # aggressive mode pins it off


def test_vps_paper_profile_multiplier_is_100():
    assert AGGRESSIVE_PAPER_DEFAULTS["FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER"] == "100"
    assert AGGRESSIVE_PAPER_DEFAULTS["FEEDBACK_ACCELERATOR_ENABLED"] == "1"
    assert AGGRESSIVE_PAPER_DEFAULTS["PAPER_PROFIT_DISCOVERY_PROFILE"] == "1"


def test_apply_forces_paper_only_locks():
    env = {}
    out = apply_aggressive_paper_env(env)
    # every live lock forced OFF / paper
    assert env["BTC_PULSE_PAPER_ONLY"] == "1"
    assert env["BTC_PULSE_LIVE_ENABLED"] == "0"
    assert env["BTC_AUTOTRADE_ENABLED"] == "0"
    assert env["GUARDED_LIVE_ENABLED"] == "0"
    assert env["MICRO_LIVE_ENABLED"] == "0"
    assert env["HTE_MODE"] == "paper"
    assert env["AGGRESSIVE_PAPER_TRAINING"] == "1"
    assert out["real_execution_possible"] is False


def test_apply_sets_aggressive_defaults_without_override():
    env = {"POLYMARKET_SCAN_LIMIT": "9999"}   # explicit value preserved
    apply_aggressive_paper_env(env)
    assert env["POLYMARKET_SCAN_LIMIT"] == "9999"
    assert env["FEEDBACK_ACCELERATOR_ENABLED"] == "1"
    assert env["FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER"] == "100"
    assert env["ABCAS_ENABLED"] == "1"
    assert env["FILL_REALISM_ENABLED"] == "1"


def test_fails_closed_on_any_real_money_flag():
    for flag in ("BTC_PULSE_LIVE_ENABLED", "BTC_AUTOTRADE_ENABLED",
                 "GUARDED_LIVE_ENABLED", "MICRO_LIVE_ENABLED",
                 "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION", "HTE_AUTOTRADE"):
        with pytest.raises(AggressivePaperUnsafe):
            apply_aggressive_paper_env({flag: "1"})


def test_assert_paper_only_lists_enabled_flags():
    assert enabled_live_flags({"BTC_AUTOTRADE_ENABLED": "1"}) == ["BTC_AUTOTRADE_ENABLED"]
    with pytest.raises(AggressivePaperUnsafe):
        assert_paper_only({"GUARDED_LIVE_ENABLED": "true"})


def test_real_execution_impossible_in_aggressive_mode():
    env = {}
    apply_aggressive_paper_env(env)
    assert is_aggressive_paper(env) is True
    # even if something later flips a live flag, aggressive mode reports impossible
    env["BTC_AUTOTRADE_ENABLED"] = "1"
    assert real_execution_possible(env) is False


def test_real_execution_possible_outside_aggressive_when_live_flag_on():
    # outside aggressive mode the guard truthfully reflects a live flag
    assert real_execution_possible({"BTC_AUTOTRADE_ENABLED": "1"}) is True
    assert real_execution_possible({}) is False


def test_no_live_flag_is_ever_set_to_on_by_defaults():
    env = {}
    apply_aggressive_paper_env(env)
    for f in FORBIDDEN_LIVE_FLAGS:
        assert env.get(f, "0") in ("0", "", "false", "paper", None)


def test_locks_and_defaults_are_disjoint_paper_only():
    # locks never enable a live path; defaults never include a forbidden live flag on
    assert "BTC_PULSE_LIVE_ENABLED" in PAPER_ONLY_LOCKS and PAPER_ONLY_LOCKS["BTC_PULSE_LIVE_ENABLED"] == "0"
    for f in FORBIDDEN_LIVE_FLAGS:
        assert AGGRESSIVE_PAPER_DEFAULTS.get(f) in (None, "0")
