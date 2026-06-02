"""Source-quality-weighted research evidence control.

Quant scope — *Data Acquisition & Ingestion* + *Evidence Preprocessing* +
*Probabilistic Modeling*: proves evidence scoring covers source quality, recency,
diversity, contradiction, settlement-rule relevance, and MARKET-SPECIFIC
relevance, and that confidence decays for weak/contradictory/stale evidence.
PAPER ONLY — advisory scoring, never sizes/approves.
"""

from __future__ import annotations

import pytest

from engine.research.evidence_scoring import (
    EvidenceScores, confidence_decay, score_evidence)
from engine.research.market_rules import market_specific_relevance_score

_NOW = 1_700_000_000_000


def _ev(**kw):
    base = {"source_type": "news", "credibility": 0.6, "relevance": 0.6, "freshness": 0.6,
            "weight": 0.6, "direction": "supports_yes", "published_ts_ms": _NOW,
            "claim": "", "source_url": "https://x.com/a"}
    base.update(kw)
    return base


def test_score_evidence_has_all_components():
    s = score_evidence([_ev(source_type="official", credibility=0.9, relevance=0.9)],
                       now_ms=_NOW)
    assert isinstance(s, EvidenceScores)
    for v in (s.quality, s.recency, s.diversity, s.contradiction,
              s.settlement_relevance, s.composite):
        assert 0.0 <= v <= 1.0


def test_market_specific_relevance_rewards_on_topic_evidence():
    on = [_ev(relevance=0.9, claim="Bitcoin BTC closes above 100k by the deadline")]
    off = [_ev(relevance=0.2, claim="unrelated celebrity gossip about a movie")]
    q = "Will Bitcoin (BTC) close above $100,000?"
    on_score = market_specific_relevance_score(on, question=q, asset="btc")
    off_score = market_specific_relevance_score(off, question=q, asset="btc")
    assert on_score > off_score
    assert 0.0 <= off_score <= on_score <= 1.0


def test_market_relevance_empty_is_zero():
    assert market_specific_relevance_score([], question="x") == 0.0


def test_weak_contradictory_stale_evidence_decays_confidence():
    clean = score_evidence([_ev(source_type="official", credibility=0.9, relevance=0.9,
                                freshness=0.9)], now_ms=_NOW)
    bad = score_evidence([_ev(direction="supports_yes", weight=0.9),
                          _ev(direction="supports_no", weight=0.9,
                              published_ts_ms=_NOW - 60 * 86_400_000)],
                         now_ms=_NOW, half_life_s=86_400)
    assert confidence_decay(0.9, clean) > confidence_decay(0.9, bad)
    assert confidence_decay(0.9, clean) <= 0.9 + 1e-9


def test_diversity_and_contradiction_directions():
    same = score_evidence([_ev(source_url="https://x.com/a"),
                           _ev(source_url="https://x.com/a")], now_ms=_NOW)
    diverse = score_evidence([_ev(source_type="official", source_url="https://bls.gov/a"),
                              _ev(source_type="news", source_url="https://reuters.com/b"),
                              _ev(source_type="exchange", source_url="https://cme.com/c")],
                             now_ms=_NOW)
    assert diverse.diversity > same.diversity
    conflict = score_evidence([_ev(direction="supports_yes", weight=0.9),
                               _ev(direction="supports_no", weight=0.9)], now_ms=_NOW)
    assert conflict.contradiction > same.contradiction
