"""Probability aggressiveness cap — uncertainty reduces size or blocks the trade.

Quant scope — *Risk Management & Portfolio Optimization*: proves high uncertainty
strictly REDUCES the size multiplier (never increases it) and blocks the trade
past a threshold, so weak evidence / high uncertainty can never inflate a paper
position. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.probability_stack import (
    uncertainty_blocks_trade, uncertainty_size_multiplier)


def test_size_multiplier_monotone_decreasing_and_bounded():
    m0 = uncertainty_size_multiplier(0.0)
    m1 = uncertainty_size_multiplier(0.3)
    m2 = uncertainty_size_multiplier(0.6)
    assert m0 == pytest.approx(1.0)
    assert m0 > m1 > m2 >= 0.0
    for u in (0.0, 0.1, 0.5, 0.9, 1.0):
        assert 0.0 <= uncertainty_size_multiplier(u) <= 1.0


def test_high_uncertainty_blocks_trade():
    assert uncertainty_blocks_trade(0.05) is False
    assert uncertainty_blocks_trade(0.9) is True


def test_uncertainty_never_increases_size():
    prev = 1.0
    for u in [i / 20.0 for i in range(0, 21)]:
        m = uncertainty_size_multiplier(u)
        assert m <= prev + 1e-9     # never increases as uncertainty rises
        prev = m


def test_weak_evidence_yields_smaller_size_than_strong():
    # higher total uncertainty (weak evidence) -> smaller size multiplier
    strong = uncertainty_size_multiplier(0.10)   # low uncertainty (strong evidence)
    weak = uncertainty_size_multiplier(0.45)     # high uncertainty (weak evidence)
    assert weak < strong


def test_blocked_trade_has_zero_or_tiny_size():
    u = 0.7
    assert uncertainty_blocks_trade(u) is True
    assert uncertainty_size_multiplier(u) <= 0.2
