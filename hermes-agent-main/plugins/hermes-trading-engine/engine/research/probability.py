"""ProbabilityEstimator — turn validated Grok output + market data into an
audited ProbabilityEstimateBundle. It computes p_llm_raw, p_calibrated,
p_ensemble, an evidence score, and a no_trade_reason. It NEVER computes order
size and NEVER submits orders.

Quant scope — *Statistical & Probabilistic Modeling* (calibrated probability),
*Signal Generation & Strategy Development* (conservative ensemble blend), and
*Compliance/Security/Operational Excellence* (Grok is research-only — it can
estimate a probability and supply evidence, but can never size, approve, place,
arm, or bypass risk; the diagnostics record the calibration method used so the
estimate is fully auditable in replay + training reports).
"""

from __future__ import annotations

import os
import time
from typing import Optional

from .ambiguity import confident_but_ambiguous
from .calibration_adapter import CalibrationAdapter
from .ensemble import ForecastEnsemble
from .evidence_scoring import (confidence_decay, evidence_quality_score,
                               research_uncertainty_from, score_evidence)
from .schemas import GrokProbabilityOutput, ProbabilityEstimateBundle


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def evidence_score_of(output: GrokProbabilityOutput) -> float:
    """Source-quality-weighted evidence score blended with model source coverage.

    Delegates the per-item quality to :func:`evidence_quality_score` (which weights
    by source-type reliability) so an official/exchange source outranks a social
    post at equal raw credibility. Advisory only — never sizes or approves."""
    items = output.evidence or []
    if not items:
        return 0.0
    mean_q = evidence_quality_score(items)
    score = 0.7 * mean_q + 0.3 * float(output.source_coverage_score)
    return round(min(1.0, max(0.0, score)), 6)


class ProbabilityEstimator:
    def __init__(self, *, calibration: Optional[CalibrationAdapter] = None,
                 ensemble: Optional[ForecastEnsemble] = None):
        self.calibration = calibration or CalibrationAdapter()
        self.ensemble = ensemble or ForecastEnsemble()
        self.min_sources = _i("RESEARCH_MIN_SOURCE_COUNT", 2)
        self.min_evidence = _f("RESEARCH_MIN_EVIDENCE_SCORE", 0.35)
        self.max_ambiguity = _f("RESEARCH_MAX_AMBIGUITY_SCORE", 0.35)
        self.stale_seconds = _i("RESEARCH_ESTIMATE_STALE_SECONDS", 900)

    def estimate(self, output: GrokProbabilityOutput, *, p_market: float | None = None,
                 p_model: float | None = None, research_run_id: str | None = None,
                 venue: str = "polymarket", mode: str = "online_paper",
                 allow_low_source: bool = False, ts_ms: int | None = None
                 ) -> ProbabilityEstimateBundle:
        ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
        p_llm = float(output.fair_probability)
        p_cal = self.calibration.apply(p_llm)
        ev_score = evidence_score_of(output)
        source_count = len(output.evidence or [])

        # Source-quality-weighted evidence scores -> decayed confidence + research
        # uncertainty. Confidence decays when evidence is old, contradictory, or
        # weakly tied to the market's resolution (advisory only — no sizing).
        scores = score_evidence(output.evidence, now_ms=ts,
                                source_coverage=float(output.source_coverage_score))
        decayed_conf = confidence_decay(output.confidence, scores)
        research_uncertainty = research_uncertainty_from(scores)
        # market-SPECIFIC relevance (ties evidence to THIS market's question), and
        # the research contribution that survives into the ensemble (advisory only).
        from .market_rules import market_specific_relevance_score
        from .validators import research_contribution
        market_relevance = market_specific_relevance_score(
            output.evidence, question=str(output.resolution_notes or ""),
            asset=str(output.market_id or ""))

        blend = self.ensemble.combine(
            p_market=p_market, p_llm=p_cal, p_model=p_model,
            confidence=decayed_conf, evidence_score=ev_score,
            ambiguity_score=output.ambiguity_score, recency_score=scores.recency,
            contradiction_score=scores.contradiction, diversity_score=scores.diversity)

        no_trade: str | None = None
        if output.no_trade_recommendation:
            no_trade = output.no_trade_reason or "grok_no_trade"
        elif confident_but_ambiguous(output.confidence, output.ambiguity_score,
                                     high_confidence=0.8,
                                     ambiguity_threshold=self.max_ambiguity):
            # High-confidence research on an ambiguous market must NOT trade —
            # research confidence can never override settlement ambiguity.
            no_trade = "research_confident_but_ambiguous"
        elif ev_score < self.min_evidence:
            no_trade = "low_evidence"
        elif output.ambiguity_score > self.max_ambiguity:
            no_trade = "high_ambiguity"
        elif source_count < self.min_sources and not allow_low_source:
            no_trade = "insufficient_sources"

        bundle = ProbabilityEstimateBundle(
            research_run_id=research_run_id, venue=venue, market_id=output.market_id,
            asset_id=output.asset_id, outcome=output.outcome, ts_ms=ts,
            p_market_mid=p_market, p_llm_raw=round(p_llm, 6), p_model=p_model,
            p_calibrated=p_cal if p_cal is not None else 0.5,
            p_ensemble=blend["p_ensemble"], confidence=round(float(output.confidence), 6),
            ambiguity_score=round(float(output.ambiguity_score), 6),
            evidence_score=ev_score, source_count=source_count,
            recency_score=scores.recency, source_diversity_score=scores.diversity,
            contradiction_score=scores.contradiction,
            settlement_relevance_score=scores.settlement_relevance,
            research_uncertainty=research_uncertainty,
            decayed_confidence=decayed_conf,
            calibration_version=self.calibration.version, ensemble_version=self.ensemble.version,
            stale_after_ts_ms=ts + self.stale_seconds * 1000, no_trade_reason=no_trade,
            diagnostics={"blend": blend, "mode": mode,
                         "calibration_method": getattr(self.calibration, "method", "shrink"),
                         "calibration_version": self.calibration.version,
                         "evidence_scores": scores.to_dict(),
                         "market_relevance_score": market_relevance,
                         "research_contribution": research_contribution(
                             p_market if p_market is not None else blend["p_ensemble"],
                             p_cal if p_cal is not None else blend["p_ensemble"],
                             blend["p_ensemble"]),
                         "key_assumptions": list(output.key_assumptions or []),
                         "do_not_trade_if": list(output.do_not_trade_if or [])})
        return bundle
