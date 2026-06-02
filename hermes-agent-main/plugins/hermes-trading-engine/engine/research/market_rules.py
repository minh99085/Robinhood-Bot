"""MarketRuleParser — extract resolution rules + ambiguity from market metadata.

Deterministic, offline. Produces a MarketRuleSummary that the AmbiguityScorer
and RiskEngine consume.
"""

from __future__ import annotations

import time
from typing import Optional

from .ambiguity import AmbiguityScorer, extract_terms
from .schemas import MarketRuleSummary

_AMBIGUOUS_TERMS = [
    "approximately", "around", "significant", "substantial", "major", "subjective",
    "at the discretion", "deemed", "reasonably", "widely considered", "rumor", "tweet",
]


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
