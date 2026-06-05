"""Tests for news/Grok evidence-weighting helpers. Tests-first.

News and Grok are EVIDENCE-ONLY: bounded, never authority, never select trades.
"""

from __future__ import annotations

from engine.research.grok_client import grok_evidence_weight
from engine.research.news_scanner import combine_news_evidence, news_evidence_weight


def test_news_weight_bounded_by_cap():
    assert 0.0 <= news_evidence_weight(1.0, 1.0, 1.0, cap=0.1) <= 0.1
    assert news_evidence_weight(1.0, 1.0, 1.0, cap=0.1) == 0.1


def test_news_weight_zero_when_irrelevant():
    assert news_evidence_weight(0.0, 1.0, 1.0) == 0.0


def test_news_weight_monotonic_in_relevance():
    lo = news_evidence_weight(0.2, 0.8, 1.0)
    hi = news_evidence_weight(0.8, 0.8, 1.0)
    assert hi > lo


def test_news_weight_clamps_out_of_range_inputs():
    # values >1 are clamped, so cannot exceed cap
    assert news_evidence_weight(5.0, 5.0, 5.0, cap=0.1) == 0.1
    assert news_evidence_weight(-1.0, 1.0) == 0.0


def test_combine_never_exceeds_cap():
    weights = [0.1] * 20
    assert combine_news_evidence(weights, cap=0.1) <= 0.1


def test_combine_monotonic_nondecreasing():
    a = combine_news_evidence([0.02, 0.02], cap=0.1)
    b = combine_news_evidence([0.02, 0.02, 0.02], cap=0.1)
    assert b >= a


def test_grok_weight_bounded():
    w = grok_evidence_weight(1.0, source_count=10, cap=0.1)
    assert 0.0 <= w <= 0.1


def test_grok_weight_zero_below_min_sources():
    assert grok_evidence_weight(1.0, source_count=1, min_sources=2) == 0.0


def test_grok_weight_increases_with_sources():
    lo = grok_evidence_weight(0.8, source_count=2)
    hi = grok_evidence_weight(0.8, source_count=20)
    assert hi > lo


def test_grok_weight_handles_bad_input():
    assert grok_evidence_weight("nope", source_count="x") == 0.0


def test_evidence_only_cannot_dominate():
    # even maximal news + grok weight is small (advisory), never authority (>=0.5)
    assert news_evidence_weight(1, 1, 1) < 0.5
    assert grok_evidence_weight(1.0, 100) < 0.5
