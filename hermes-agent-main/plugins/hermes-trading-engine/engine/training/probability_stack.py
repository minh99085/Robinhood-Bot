"""ProbabilityStack — conservative fair-value ensemble for Polymarket.

For each candidate it computes:

* ``p_market_mid``  — midpoint of best bid/ask (executable reference).
* ``p_model``       — deterministic feature model (+ optional learned category
                      calibration). With no learning yet it equals the market
                      mid (no independent alpha), so a fresh trainer NEVER
                      trades on the model alone.
* ``p_research``    — Grok/research estimate (research-only). The OFFLINE stub
                      is NOT trusted for trading unless explicitly allowed.
* ``p_final``       — shrink-toward-market ensemble:
                      ``p_final = p_market + shrink * (p_raw - p_market)``.

``shrink`` shrinks harder when the spread is wide, liquidity is low, evidence is
weak, ambiguity is high, the book is stale, time-to-close is short, or model
calibration is poor — i.e. we only move away from the market price when the
signal is well-supported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from engine.markets import universe_manager as um
from engine.research.ensemble import decompose_uncertainty

logger = logging.getLogger("hte.training.probability_stack")

_REAL_RESEARCH_SOURCES = ("grok_online", "grok_cache")


def market_mid(rec: "um.MarketRecord") -> float:
    bid = um._as_float(rec.raw.get("bestBid"), 0.0)
    ask = um._as_float(rec.raw.get("bestAsk"), 0.0)
    if bid and ask:
        return (bid + ask) / 2.0
    if rec.yes_price is not None:
        return float(rec.yes_price)
    return 0.5


def best_ask(rec: "um.MarketRecord") -> Optional[float]:
    ask = um._as_float(rec.raw.get("bestAsk"), 0.0)
    return ask if ask > 0 else None


def has_fresh_book(rec: "um.MarketRecord", max_age_s: float = 30.0) -> bool:
    """A usable CLOB book = both sides present and (if known) not stale."""
    bid = um._as_float(rec.raw.get("bestBid"), 0.0)
    ask = um._as_float(rec.raw.get("bestAsk"), 0.0)
    if not (bid > 0 and ask > 0):
        return False
    if rec.book_age_s is not None and rec.book_age_s > max_age_s:
        return False
    return True


@dataclass
class ProbabilityEstimate:
    market_id: str
    p_market_mid: float
    p_model: float
    p_research: float
    p_raw: float
    p_final: float
    shrink: float
    confidence: float
    research_source: str
    research_usable: bool
    model_has_edge: bool
    ambiguity_score: float
    evidence_score: float
    stale_score: float
    spread: float
    liquidity_usd: float
    calibration_error: float
    fresh_book: bool
    best_ask: Optional[float]
    # optional Chainlink-conditioned inputs (0/False when Chainlink not wired)
    chainlink_confidence: float = 0.0
    chainlink_no_trade: bool = False
    chainlink_reason: str = ""
    chainlink_feed: str = ""
    # ---- institutional calibration extension (backward-compatible) ----
    # All optional with conservative defaults so existing construction sites and
    # the executable `p_final`/edge gate are completely unaffected.
    calibrated_probability: float = 0.0
    confidence_interval_low: float = 0.0
    confidence_interval_high: float = 0.0
    uncertainty_components: dict = field(default_factory=dict)
    effective_sample_size: float = 0.0
    calibration_method: str = "identity"
    chainlink_features: dict = field(default_factory=dict)
    bregman_group_id: str = ""
    no_trade_probability_reason: str = ""
    # research-channel uncertainty (from the uncertainty decomposition); a more
    # uncertain research signal lowers trust + is a higher-value paper sample.
    research_uncertainty: float = 0.0

    def __post_init__(self) -> None:
        # Default the calibrated probability + interval to the executable fair
        # value so an un-calibrated estimate is still internally consistent.
        if self.calibrated_probability == 0.0:
            self.calibrated_probability = self.p_final
        if self.confidence_interval_low == 0.0 and self.confidence_interval_high == 0.0:
            self.confidence_interval_low = self.calibrated_probability
            self.confidence_interval_high = self.calibrated_probability

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d


class ProbabilityStack:
    def __init__(self, cfg, learner=None, chainlink=None, calibrator=None):
        self.cfg = cfg
        self.learner = learner
        # optional engine.chainlink_scanner.ChainlinkScanner (additive, default off)
        self.chainlink = chainlink
        # optional engine.calibration_models.InstitutionalCalibrator (additive).
        # When present it annotates `calibrated_probability` + a confidence
        # interval; it NEVER changes the executable `p_final` or the edge gate.
        self.calibrator = calibrator

    # -- component estimators ------------------------------------------------
    def _p_model(self, rec, mid: float) -> float:
        """Deterministic feature model. Defaults to the market mid (no alpha);
        a learned per-category calibration bias is the only thing that moves it,
        so a fresh trainer never trades on the model alone."""
        bias = 0.0
        if self.learner is not None:
            bias = float(self.learner.category_bias(rec.category))
        return max(0.02, min(0.98, mid + bias))

    def _ambiguity(self, rec) -> float:
        raw_amb = um._as_float(rec.raw.get("ambiguity"), None)
        if raw_amb is not None:
            return max(0.0, min(1.0, raw_amb))
        amb = 0.0 if rec.has_resolution_text else 0.6
        # extreme prices near 0/1 are often noisier / harder to fade
        if rec.yes_price is not None and (rec.yes_price < 0.05 or rec.yes_price > 0.95):
            amb = max(amb, 0.4)
        return amb

    def _evidence(self, rec, source: str, confidence: float) -> float:
        base = {"grok_online": 0.7, "grok_cache": 0.6}.get(source, 0.2)
        desc_bonus = min(0.2, len(str(rec.raw.get("description") or "")) / 3000.0)
        return max(0.0, min(1.0, base + desc_bonus * confidence))

    # -- main ----------------------------------------------------------------
    def estimate(self, rec, signal_model, *, now: Optional[float] = None) -> ProbabilityEstimate:
        now = now or time.time()
        mid = market_mid(rec)
        p_model = self._p_model(rec, mid)
        model_has_edge = abs(p_model - mid) >= 0.005

        sig = signal_model.evaluate(rec)
        source = getattr(sig, "source", "simulated")
        p_research = max(0.02, min(0.98, float(getattr(sig, "fair_value", mid))))
        confidence = float(getattr(sig, "confidence", 0.5) or 0.5)

        allow_stub = bool(getattr(self.cfg, "allow_offline_stub_trading", False))
        research_usable = source in _REAL_RESEARCH_SOURCES or (
            allow_stub and source in ("offline_research_stub", "simulated"))

        ambiguity = self._ambiguity(rec)
        evidence = self._evidence(rec, source, confidence)
        stale_score = 0.0
        if rec.book_age_s is not None:
            stale_score = max(0.0, min(1.0, rec.book_age_s / 30.0))
        elif not has_fresh_book(rec):
            stale_score = 1.0
        calib_err = float(self.learner.calibration_error()) if self.learner else 0.0

        # p_raw: only blend in research when it is trustworthy for trading
        if research_usable:
            w_res = max(0.0, min(1.0, confidence))
            p_raw = (1 - w_res) * p_model + w_res * p_research
        else:
            p_raw = p_model  # == mid unless a learned bias exists

        # shrink toward market (v2): start at the conservative base and only move
        # away from the market price when the signal is well-supported. Clamp to
        # [min_shrink_factor, max_shrink_factor].
        base = float(getattr(self.cfg, "base_shrink_factor", 0.25))
        lo = float(getattr(self.cfg, "min_shrink_factor", 0.05))
        hi = float(getattr(self.cfg, "max_shrink_factor", 0.60))
        max_spread = max(1e-6, float(getattr(self.cfg, "max_spread", 0.08)))
        shrink = base
        shrink -= 0.20 * max(0.0, min(1.0, float(rec.spread or 0.0) / max_spread))
        shrink -= 0.15 * (1.0 - _liq_quality(rec.liquidity_usd))
        shrink -= 0.20 * (1.0 - evidence)
        shrink -= 0.20 * ambiguity
        shrink -= 0.20 * stale_score
        shrink -= 0.30 * max(0.0, min(1.0, calib_err * 2.0))
        # only widen when research is trustworthy AND quality is strong
        if research_usable and evidence > 0.6 and ambiguity < 0.2 and stale_score < 0.1:
            shrink += 0.15
        shrink = max(lo, min(hi, shrink))

        p_final = max(0.02, min(0.98, mid + shrink * (p_raw - mid)))

        # Chainlink-conditioned adjustment (optional, additive). Only nudges the
        # probability for markets linked to a fresh, relevant oracle; sets a
        # no-trade flag when the linked oracle is stale/missing/inconsistent.
        cl_conf, cl_block, cl_reason, cl_feed = 0.0, False, "", ""
        cl_features: dict = {}
        cl_linked = False
        if self.chainlink is not None:
            try:
                sig = self.chainlink.signal_for_market(rec, p_base=p_final, now=now)
                p_final = max(0.02, min(0.98, sig.apply(p_final)))
                cl_conf, cl_block = sig.confidence, sig.no_trade
                cl_reason = ",".join(sig.reasons)
                cl_feed = sig.feed_key or ""
                cl_features = dict(getattr(sig, "features", {}) or {})
                cl_linked = sig.feed_key is not None
            except Exception:  # noqa: BLE001 — Chainlink must never break the stack
                logger.debug("chainlink signal failed for %s", rec.market_id,
                             exc_info=True)

        fresh = has_fresh_book(rec)

        # ---- uncertainty decomposition (always populated) ----
        uncertainty = decompose_uncertainty(
            spread=float(rec.spread or 0.0), max_spread=max_spread,
            calibration_error=calib_err, model_has_edge=model_has_edge,
            research_usable=research_usable, confidence=confidence,
            evidence_score=evidence, chainlink_confidence=cl_conf,
            chainlink_linked=cl_linked, chainlink_no_trade=cl_block,
            liquidity_usd=float(rec.liquidity_usd or 0.0), ambiguity_score=ambiguity,
            stale_score=stale_score)

        # ---- institutional calibration (annotation only; p_final unchanged) ----
        if self.calibrator is not None:
            p_cal, ci_lo, ci_hi = self.calibrator.transform_with_interval(p_final)
            cal_method = self.calibrator.calibration_method
            ess = float(self.calibrator.effective_sample_size)
        else:
            p_cal = p_final
            cal_method = "identity"
            ess = 0.0
            # interval from the total uncertainty when no calibrator is fitted
            half = 0.25 * uncertainty.get("total", 0.0)
            ci_lo = max(0.0, p_cal - half)
            ci_hi = min(1.0, p_cal + half)

        # ---- Bregman fair-probability grouping (linked oracle feed) ----
        bregman_group_id = cl_feed if (cl_linked and not cl_block) else ""

        # research-channel uncertainty (advisory; surfaced for trust + active
        # learning). Sourced from the always-populated uncertainty decomposition.
        research_uncertainty = float(uncertainty.get("research", 0.0))

        # ---- advisory probability-level no-trade reason ----
        no_trade_reason = self._no_trade_probability_reason(
            cl_block=cl_block, fresh=fresh, ambiguity=ambiguity,
            research_usable=research_usable, evidence=evidence,
            stale_score=stale_score, confidence=confidence)

        return ProbabilityEstimate(
            market_id=rec.market_id, p_market_mid=mid, p_model=p_model,
            p_research=p_research, p_raw=p_raw, p_final=p_final, shrink=shrink,
            confidence=confidence, research_source=source,
            research_usable=research_usable, model_has_edge=model_has_edge,
            ambiguity_score=ambiguity, evidence_score=evidence,
            stale_score=stale_score, spread=float(rec.spread or 0.0),
            liquidity_usd=float(rec.liquidity_usd or 0.0), calibration_error=calib_err,
            fresh_book=fresh, best_ask=best_ask(rec),
            chainlink_confidence=cl_conf, chainlink_no_trade=cl_block,
            chainlink_reason=cl_reason, chainlink_feed=cl_feed,
            calibrated_probability=p_cal, confidence_interval_low=ci_lo,
            confidence_interval_high=ci_hi, uncertainty_components=uncertainty,
            effective_sample_size=ess, calibration_method=cal_method,
            chainlink_features=cl_features, bregman_group_id=bregman_group_id,
            no_trade_probability_reason=no_trade_reason,
            research_uncertainty=research_uncertainty)

    def _no_trade_probability_reason(self, *, cl_block: bool, fresh: bool,
                                     ambiguity: float, research_usable: bool,
                                     evidence: float, stale_score: float,
                                     confidence: float = 0.0) -> str:
        """Advisory probability-level no-trade reason (the executable gate lives in
        EdgeEngine; this surfaces WHY the probability itself is untrustworthy)."""
        from engine.research.ambiguity import confident_but_ambiguous
        cfg = self.cfg
        max_amb = float(getattr(cfg, "max_ambiguity_score", 0.35))
        if cl_block:
            return "chainlink_stale_or_irrelevant"
        if not fresh:
            return "stale_book"
        # research confidence must not override settlement ambiguity
        if research_usable and confident_but_ambiguous(
                confidence, ambiguity,
                high_confidence=float(getattr(cfg, "research_high_confidence", 0.8)),
                ambiguity_threshold=max_amb,
                confident_frac=float(getattr(cfg, "research_confident_ambiguity_frac", 0.6))):
            return "research_confident_but_ambiguous"
        if ambiguity > max_amb:
            return "high_ambiguity"
        if research_usable and evidence < float(getattr(cfg, "min_evidence_score", 0.5)):
            return "weak_evidence"
        if stale_score >= 1.0:
            return "stale_data"
        return ""


def feedback_uncertainty(est: "ProbabilityEstimate") -> float:
    """Uncertainty magnitude in [0,1] for active-learning feedback value.

    Prefers the decomposed total uncertainty; falls back to the calibrated
    confidence-interval width. A more uncertain estimate is a higher-value paper
    sample (Statistical Modeling / active learning). Read-only."""
    u = getattr(est, "uncertainty_components", None)
    if isinstance(u, dict) and u.get("total") is not None:
        return max(0.0, min(1.0, float(u["total"])))
    lo = float(getattr(est, "confidence_interval_low", 0.0) or 0.0)
    hi = float(getattr(est, "confidence_interval_high", 0.0) or 0.0)
    return max(0.0, min(1.0, hi - lo))


def timing_decay_proxy(est: "ProbabilityEstimate") -> float:
    """Pre-trade timing-decay proxy in [0,1] for the profitability governor: a
    wider spread + lower liquidity + staler book means a directional edge decays
    faster before it can fill. Read-only; feeds the after-cost edge net-out."""
    spread = float(getattr(est, "spread", 0.0) or 0.0)
    stale = float(getattr(est, "stale_score", 0.0) or 0.0)
    liq = float(getattr(est, "liquidity_usd", 0.0) or 0.0)
    liq_q = _liq_quality(liq)
    return max(0.0, min(1.0, 0.5 * min(1.0, spread / 0.08) + 0.3 * (1.0 - liq_q) + 0.2 * stale))


def overfit_adjusted_shrink(base: float, penalty: float, *,
                            conservative: float = 0.25) -> float:
    """Pull the shrink-toward-market factor back to a conservative value as the
    overfit penalty rises (anti-overfitting). A smaller shrink keeps fair value
    closer to the market price, suppressing fragile, overfit edges. ``penalty=0``
    keeps the aggressive shrink; ``penalty=1`` reverts to ``conservative``."""
    p = max(0.0, min(1.0, float(penalty)))
    return (1.0 - p) * float(base) + p * float(conservative)


def _liq_quality(liq: float) -> float:
    import math
    liq = max(0.0, float(liq or 0.0))
    if liq <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log1p(liq) / math.log1p(100_000.0)))
