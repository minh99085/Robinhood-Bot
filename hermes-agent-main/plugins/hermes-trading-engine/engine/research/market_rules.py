"""MarketRuleParser — extract resolution rules + ambiguity from market metadata.

Deterministic, offline. Produces a MarketRuleSummary that the AmbiguityScorer
and RiskEngine consume.
"""

from __future__ import annotations

import time
from typing import Optional

from .ambiguity import (
    AmbiguityScorer,
    extract_terms,
    is_settlement_ambiguous,
    label_confidence,
)
from .schemas import MarketRuleSummary

_AMBIGUOUS_TERMS = [
    "approximately", "around", "significant", "substantial", "major", "subjective",
    "at the discretion", "deemed", "reasonably", "widely considered", "rumor", "tweet",
]


def market_specific_relevance_score(evidence: list, *, question: str = "",
                                    asset: str = "") -> float:
    """Mean market-SPECIFIC relevance of an evidence set in ``[0, 1]``.

    How tightly each evidence claim ties to THIS market's question + underlying
    asset (keyword overlap with the question, plus an explicit asset mention) —
    distinct from settlement-rule relevance, which is about the resolution source.
    Empty evidence -> 0. Pure + deterministic (Evidence Preprocessing)."""
    from .evidence_scoring import _keywords  # shared keyword extractor

    items = list(evidence or [])
    if not items:
        return 0.0
    q_kw = _keywords(f"{question} {asset}")
    asset_l = str(asset or "").strip().lower()
    scores = []
    for e in items:
        claim = (e.get("claim", "") if isinstance(e, dict) else getattr(e, "claim", "")) or ""
        c_kw = _keywords(str(claim))
        overlap = (len(q_kw & c_kw) / float(len(q_kw))) if q_kw and c_kw else 0.0
        asset_hit = 1.0 if (asset_l and asset_l in str(claim).lower()) else 0.0
        rel = (e.get("relevance", 0.0) if isinstance(e, dict)
               else getattr(e, "relevance", 0.0)) or 0.0
        scores.append(max(0.0, min(1.0, 0.5 * overlap + 0.3 * asset_hit + 0.2 * float(rel))))
    return round(sum(scores) / len(scores), 6)


class MarketRuleParser:
    def __init__(self):
        self.scorer = AmbiguityScorer()

    def parse(self, meta: dict) -> MarketRuleSummary:
        venue = meta.get("venue") or "polymarket"
        market_id = str(meta.get("market_id") or meta.get("id") or "")
        asset_id = meta.get("asset_id")
        outcome = meta.get("outcome")
        question = str(meta.get("question") or meta.get("title") or "")
        description = str(meta.get("description") or meta.get("resolution_criteria") or "")
        resolution_source = meta.get("resolution_source") or meta.get("resolutionSource")
        close_ts = meta.get("close_ts_ms") or meta.get("end_ts_ms")
        deadline = meta.get("resolution_deadline_ts_ms")

        text = f"{question}\n{description}"
        criteria = [s.strip() for s in description.replace("\n", ". ").split(". ") if s.strip()]
        edge_cases = [c for c in criteria if any(w in c.lower() for w in
                      ("if ", "unless", "except", "edge case", "tie", "void", "n/a"))]
        ambiguous_terms = extract_terms(text, _AMBIGUOUS_TERMS)

        score, categories = self.scorer.score(text, {
            "resolution_source": resolution_source, "close_ts_ms": close_ts,
            "resolution_deadline_ts_ms": deadline, "stale_metadata": meta.get("stale_metadata")})

        return MarketRuleSummary(
            market_id=market_id, asset_id=asset_id, venue=venue, question=question,
            outcome=outcome, resolution_source=resolution_source, close_ts_ms=close_ts,
            resolution_deadline_ts_ms=deadline, criteria=criteria[:20],
            edge_cases=edge_cases[:20], ambiguous_terms=ambiguous_terms,
            ambiguity_categories=categories, ambiguity_score=score,
            parsed_ts_ms=int(time.time() * 1000))

    def label_confidence(self, summary: MarketRuleSummary) -> float:
        """Settlement-label confidence implied by a parsed rule summary (the
        cleaner the rules, the higher the confidence the eventual label earns)."""
        return label_confidence(getattr(summary, "ambiguity_score", 0.0))

    def is_settlement_ambiguous(self, summary: MarketRuleSummary,
                                threshold: float = 0.5) -> bool:
        return is_settlement_ambiguous(getattr(summary, "ambiguity_score", 0.0), threshold)

    def market_relevance(self, summary: MarketRuleSummary, evidence: list) -> float:
        """Market-SPECIFIC relevance of an evidence set to THIS market's question
        + outcome (distinct from settlement-rule relevance). Advisory only."""
        return market_specific_relevance_score(
            evidence or [], question=getattr(summary, "question", "") or "",
            asset=getattr(summary, "outcome", "") or "")

    def evidence_relevance(self, summary: MarketRuleSummary, evidence: list) -> float:
        """Settlement-rule relevance of an evidence set to this market's rules.

        Higher when the evidence comes from the resolution source / official
        channels and its claims overlap the parsed resolution criteria. Advisory
        only — feeds the research trust discount, never sizing/approval."""
        from .evidence_scoring import settlement_rule_relevance_score
        return settlement_rule_relevance_score(evidence or [], rule_summary=summary)
