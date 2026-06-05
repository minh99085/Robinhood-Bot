"""Tests for BTC Pulse Tier-2 block predicate (pulse_block_reason). Tests-first.

Blocks on unknown/chop regime, negative after-cost EV, or weak fill realism.
"""

from __future__ import annotations

from engine.training.btc_pulse import PULSE_BLOCKED_REGIMES, pulse_block_reason


def test_unknown_regime_blocked():
    assert pulse_block_reason("unknown", after_cost_ev=0.1, fill_realism_ok=True) == "regime_unknown"


def test_chop_regime_blocked():
    assert pulse_block_reason("chop", after_cost_ev=0.1) == "regime_chop"


def test_stale_oracle_regimes_blocked():
    for r in ("stale_oracle", "stale_fast_price", "oracle_disagreement"):
        assert pulse_block_reason(r, after_cost_ev=1.0) == f"regime_{r}"


def test_negative_after_cost_ev_blocked():
    assert pulse_block_reason("trending_up", after_cost_ev=-0.01) == "negative_after_cost_ev"


def test_zero_after_cost_ev_blocked():
    assert pulse_block_reason("trending_up", after_cost_ev=0.0) == "negative_after_cost_ev"


def test_weak_fill_realism_blocked():
    assert pulse_block_reason("trending_up", after_cost_ev=0.1, fill_realism_ok=False) == "weak_fill_realism"


def test_healthy_regime_not_blocked():
    assert pulse_block_reason("trending_up", after_cost_ev=0.05, fill_realism_ok=True) is None


def test_none_regime_treated_as_unknown():
    assert pulse_block_reason(None) == "regime_unknown"


def test_ev_none_skips_ev_check():
    assert pulse_block_reason("trending_down", after_cost_ev=None, fill_realism_ok=True) is None


def test_non_numeric_ev_blocked():
    assert pulse_block_reason("trending_up", after_cost_ev="oops") == "unknown_after_cost_ev"


def test_blocked_regimes_frozenset_contains_core():
    assert {"unknown", "chop", "stale_oracle"}.issubset(PULSE_BLOCKED_REGIMES)


def test_case_insensitive_regime():
    assert pulse_block_reason("CHOP") == "regime_chop"
