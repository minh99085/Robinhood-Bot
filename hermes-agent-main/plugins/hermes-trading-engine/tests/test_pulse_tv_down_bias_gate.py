"""TradingView DOWN-bias gate (Townhall P3)."""

from __future__ import annotations

from engine.pulse.tv_down_bias_gate import TradingViewDownBiasGate


def test_blocks_bullish_aligned_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", mtf_alignment="bullish_aligned", tv_direction="UP")
    assert r["decision"] == "block"
    assert "tv_down_bias_bullish_aligned_up" in r["reasons"]


def test_blocks_up_without_bearish():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", mtf_alignment="mixed", tv_direction="UP")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_without_bearish" in r["reasons"]


def test_allows_down_and_bearish_up_explore_path():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    assert g.evaluate(side="down", mtf_alignment="bearish_aligned")["decision"] == "pass"
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned", tv_direction="DOWN")["decision"] == "pass"


def test_disabled_passes():
    g = TradingViewDownBiasGate(enabled=False)
    assert g.evaluate(side="up", mtf_alignment="bullish_aligned")["decision"] == "pass"