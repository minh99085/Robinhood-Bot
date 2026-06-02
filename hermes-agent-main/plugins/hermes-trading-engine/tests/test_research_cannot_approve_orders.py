"""Research advisory firewall — Grok/research can never act.

Quant scope — *Compliance/Security/Operational Excellence*: proves research may
inform a probability ONLY; it cannot size, approve, arm, submit, or override
risk. The firewall strips any execution-intent fields, and the research
contribution metric only ever measures how much of the research view survived
calibration (bounded), never an action. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.research.schemas import ProbabilityEstimateBundle
from engine.research.validators import (
    ResearchFirewall, research_contribution, research_is_advisory_only,
    validate_probability_output)


def test_firewall_detects_and_strips_execution_intent():
    fw = ResearchFirewall()
    raw = {"market_id": "m1", "fair_probability": 0.7, "confidence": 0.8,
           "order_size": 100, "approve": True, "submit": True, "arm": True,
           "place_order": True, "should_trade": True, "size": 50}
    found = fw.scan(raw)
    for k in ("order_size", "approve", "submit", "arm", "place_order",
              "should_trade", "size"):
        assert k in found
    clean = fw.sanitize(raw)
    assert not (set(clean) & set(found))
    verdict = fw.assert_advisory(raw)
    assert verdict["advisory"] is True and verdict["stripped"]


def test_validated_bundle_has_no_execution_fields():
    out = validate_probability_output({"market_id": "m1", "fair_probability": 0.7,
                                       "confidence": 0.8, "approve": True, "size": 9})
    assert out is not None
    for k in ("approve", "size", "order_size", "submit", "arm", "should_trade"):
        assert not hasattr(out, k)
    b = ProbabilityEstimateBundle(market_id="m1")
    for k in ("approve", "size", "should_trade", "approved", "order_size"):
        assert not hasattr(b, k)
    assert research_is_advisory_only() is True


def test_research_contribution_is_bounded_and_advisory():
    # research wanted 0.5 -> 0.9; the calibrated final only moved to 0.62
    c = research_contribution(p_market=0.5, p_research=0.9, p_final=0.62)
    assert 0.0 <= c <= 1.0
    assert c == pytest.approx((0.62 - 0.5) / (0.9 - 0.5), abs=1e-6)
    # research cannot amplify beyond its own view (clamped at 1.0)
    assert research_contribution(p_market=0.5, p_research=0.6, p_final=0.9) == pytest.approx(1.0)
    # no research deviation -> zero contribution
    assert research_contribution(p_market=0.5, p_research=0.5, p_final=0.5) == 0.0


def test_firewall_sanitize_keeps_probability_fields():
    fw = ResearchFirewall()
    clean = fw.sanitize({"market_id": "m1", "fair_probability": 0.7, "confidence": 0.8,
                         "evidence": [], "approve": True})
    assert clean["fair_probability"] == 0.7 and clean["confidence"] == 0.8
    assert "approve" not in clean
