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


def test_blocks_up_on_bearish_down_stack():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned", tv_direction="DOWN")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_on_bearish_down_stack" in r["reasons"]


def test_blocks_up_tv_down_non_bearish():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_on_bearish_down_stack=False)
    r = g.evaluate(side="up", mtf_alignment="mixed", tv_direction="DOWN")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_tv_down_non_bearish" in r["reasons"]


def test_allows_up_tv_down_bearish_when_stack_rule_off():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_on_bearish_down_stack=False,
                                block_up_without_bearish=False)
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned",
                      tv_direction="DOWN")["decision"] == "pass"


def test_allows_down_and_bearish_up_when_stack_rule_off():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_on_bearish_down_stack=False)
    assert g.evaluate(side="down", mtf_alignment="bearish_aligned")["decision"] == "pass"
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned", tv_direction="DOWN")["decision"] == "pass"


def test_blocks_up_against_confirmed_down():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", tf_confirm="confirmed_down", tv_direction="DOWN",
                   mtf_alignment="bearish_aligned")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_against_confirmed_down" in r["reasons"]


def test_allows_up_when_confirmed_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False)
    assert g.evaluate(side="up", tf_confirm="confirmed_up")["decision"] == "pass"


def test_disabled_passes():
    g = TradingViewDownBiasGate(enabled=False)
    assert g.evaluate(side="up", mtf_alignment="bullish_aligned")["decision"] == "pass"