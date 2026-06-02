"""ForecastEnsemble — conservative combination of probability components.

Combines market-implied probability, the (calibrated) LLM probability, and an
optional model probability. The LLM weight is shrunk when confidence is low,
evidence is weak, or ambiguity is high. Extreme blends are clamped unless
evidence is exceptionally strong. All intermediate components are returned for
audit. It NEVER produces a position size.
"""

from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from typing import Optional

ENSEMBLE_VERSION = "v1"
DYNAMIC_ENSEMBLE_VERSION = "dyn-v1"

logger = logging.getLogger("hte.research.ensemble")

# Uncertainty sources surfaced by :func:`decompose_uncertainty`.
UNCERTAINTY_SOURCES = ("market", "model", "research", "chainlink", "liquidity",
                       "ambiguity", "stale")


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def _liq_quality(liq: float) -> float:
    """Liquidity regime quality in [0, 1] (log-scaled, $100k -> ~1.0)."""
    liq = max(0.0, float(liq or 0.0))
    if liq <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log1p(liq) / math.log1p(100_000.0)))


def _ttr_factor(time_to_resolution_s: Optional[float]) -> float:
    """Time-to-resolution regime in [0, 1]: short horizons damp deviation.

    Unknown (``None``) -> 1.0 (no penalty); a 7-day horizon saturates at 1.0.
    """
    if time_to_resolution_s is None:
        return 1.0
    t = max(0.0, float(time_to_resolution_s))
    ref = 7 * 24 * 3600.0
    return max(0.0, min(1.0, math.log1p(t) / math.log1p(ref)))


class ForecastEnsemble:
    def __init__(self, *, market_weight: float | None = None, llm_weight: float | None = None,
                 model_weight: float | None = None, clamp_low: float | None = None,
                 clamp_high: float | None = None):
        self.market_weight = market_weight if market_weight is not None else _f("RESEARCH_ENSEMBLE_MARKET_WEIGHT", 0.50)
        self.llm_weight = llm_weight if llm_weight is not None else _f("RESEARCH_ENSEMBLE_LLM_WEIGHT", 0.30)
        self.model_weight = model_weight if model_weight is not None else _f("RESEARCH_ENSEMBLE_MODEL_WEIGHT", 0.20)
        self.clamp_low = clamp_low if clamp_low is not None else _f("RESEARCH_EXTREME_PROB_CLAMP_LOW", 0.05)
        self.clamp_high = clamp_high if clamp_high is not None else _f("RESEARCH_EXTREME_PROB_CLAMP_HIGH", 0.95)
        self.version = ENSEMBLE_VERSION

    def combine(self, *, p_market: float | None, p_llm: float | None, p_model: float | None,
                confidence: float = 0.5, evidence_score: float = 0.5,
                ambiguity_score: float = 0.0) -> dict:
        conf = min(1.0, max(0.0, confidence))
        ev = min(1.0, max(0.0, evidence_score))
        amb = min(1.0, max(0.0, ambiguity_score))

        # LLM weight is reduced by low confidence, low evidence, and high ambiguity.
        llm_quality = conf * (0.4 + 0.6 * ev) * (1.0 - amb)
        w_market = self.market_weight if p_market is not None else 0.0
        w_llm = self.llm_weight * llm_quality if p_llm is not None else 0.0
        w_model = self.model_weight if p_model is not None else 0.0
        total = w_market + w_llm + w_model

        if total <= 0:
            # nothing usable -> fall back to market, else neutral
            blended = p_market if p_market is not None else 0.5
        else:
            acc = 0.0
            if p_market is not None:
                acc += w_market * p_market
            if p_llm is not None:
                acc += w_llm * p_llm
            if p_model is not None:
                acc += w_model * p_model
            blended = acc / total

        strong_evidence = ev >= 0.8 and conf >= 0.8
        clamped = blended
        if not strong_evidence:
            clamped = min(self.clamp_high, max(self.clamp_low, blended))

        return {
            "p_ensemble": round(clamped, 6),
            "p_ensemble_unclamped": round(blended, 6),
            "weights": {"market": round(w_market, 6), "llm": round(w_llm, 6),
                        "model": round(w_model, 6)},
            "llm_quality": round(llm_quality, 6),
            "clamped": clamped != blended,
            "ensemble_version": self.version,
        }


# --------------------------------------------------------------------------- #
# Uncertainty decomposition (Statistical & Probabilistic Modeling)
# --------------------------------------------------------------------------- #
def decompose_uncertainty(*, spread: float = 0.0, max_spread: float = 0.08,
                          calibration_error: float = 0.0,
                          model_has_edge: bool = True,
                          research_usable: bool = True, confidence: float = 0.5,
                          evidence_score: float = 0.5,
                          chainlink_confidence: float = 0.0,
                          chainlink_linked: bool = False,
                          chainlink_no_trade: bool = False,
                          liquidity_usd: float = 0.0, ambiguity_score: float = 0.0,
                          stale_score: float = 0.0) -> dict:
    """Decompose total fair-probability uncertainty into named sources.

    Returns a dict with every source in :data:`UNCERTAINTY_SOURCES` plus
    ``total``, all in ``[0, 1]``. ``total`` is the *noisy-or* combination
    ``1 - Π(1 - source)``, which is monotonically non-decreasing in every source
    (more uncertainty in any channel can only raise the total).

    * ``market``    — book spread regime (wide spread -> uncertain price).
    * ``model``     — model calibration error + lack of independent edge.
    * ``research``  — weak/absent research evidence + low confidence.
    * ``chainlink`` — stale/blocked oracle (1.0) or low oracle confidence; an
                      unlinked market contributes 0 (the oracle abstains).
    * ``liquidity`` — thin liquidity regime.
    * ``ambiguity`` — settlement ambiguity.
    * ``stale``     — stale market data.
    """
    market = _clamp(spread / max_spread) if max_spread > 0 else 0.0
    model = _clamp((0.0 if model_has_edge else 0.3) + 2.0 * max(0.0, calibration_error))
    if research_usable:
        research = _clamp(0.5 * (1.0 - _clamp(evidence_score))
                          + 0.5 * (1.0 - _clamp(confidence)))
    else:
        research = 0.7
    if chainlink_no_trade:
        chainlink = 1.0
    elif chainlink_linked:
        chainlink = _clamp(1.0 - _clamp(chainlink_confidence))
    else:
        chainlink = 0.0
    liquidity = _clamp(1.0 - _liq_quality(liquidity_usd))
    ambiguity = _clamp(ambiguity_score)
    stale = _clamp(stale_score)

    components = {"market": market, "model": model, "research": research,
                  "chainlink": chainlink, "liquidity": liquidity,
                  "ambiguity": ambiguity, "stale": stale}
    prod = 1.0
    for v in components.values():
        prod *= (1.0 - v)
    total = _clamp(1.0 - prod)
    return {k: round(v, 6) for k, v in components.items()} | {"total": round(total, 6)}


# --------------------------------------------------------------------------- #
# Dynamic forecast ensemble (Signal Generation & Strategy Development)
# --------------------------------------------------------------------------- #
class DynamicForecastEnsemble:
    """Conservative, regime-aware probability ensemble.

    The ensemble blends the market midpoint with optional signal components
    (a learned model + research/Grok estimate + a fresh Chainlink oracle nudge),
    then applies a *conservatism multiplier* that shrinks the blended deviation
    back toward the market price. The multiplier is a product of per-regime
    dampeners (spread, liquidity, evidence, ambiguity, stale book, calibration
    history, time-to-resolution). Because the signal blend is independent of
    those regime factors, the deviation from the market price is **monotonically
    non-increasing** as any single regime worsens — the ensemble can never become
    more aggressive when evidence weakens, the book/oracle goes stale, ambiguity
    rises, liquidity drops, calibration worsens, or time-to-resolution shortens.

    Grok is consumed strictly as a probability INPUT (``p_research``); this class
    never sizes, approves, places, arms, or bypasses any risk control.
    """

    version = DYNAMIC_ENSEMBLE_VERSION

    def __init__(self, *, base_shrink: float = 0.60, min_shrink: float = 0.0,
                 max_shrink: float = 0.90, max_spread: float = 0.08,
                 market_weight: float = 1.0, model_weight: float = 0.25,
                 research_weight: float = 0.60, chainlink_weight: float = 0.50):
        self.base_shrink = base_shrink
        self.min_shrink = min_shrink
        self.max_shrink = max_shrink
        self.max_spread = max(1e-6, max_spread)
        self.market_weight = market_weight
        self.model_weight = model_weight
        self.research_weight = research_weight
        self.chainlink_weight = chainlink_weight

    def combine(self, *, p_market: Optional[float], p_model: Optional[float] = None,
                p_research: Optional[float] = None, category_bias: float = 0.0,
                research_confidence: float = 0.5, research_usable: bool = True,
                evidence_score: float = 0.5, chainlink_adjustment: float = 0.0,
                chainlink_confidence: float = 0.0, chainlink_linked: bool = False,
                chainlink_no_trade: bool = False,
                chainlink_features: Optional[dict] = None, liquidity_usd: float = 0.0,
                spread: float = 0.0, time_to_resolution_s: Optional[float] = None,
                ambiguity_score: float = 0.0, stale_score: float = 0.0,
                calibration_error: float = 0.0,
                calibration_history: Optional[list] = None,
                confidence: float = 0.5) -> dict:
        pm = _clamp01(p_market) if p_market is not None else 0.5

        # calibration history (reliability rows) -> a single calibration error
        if calibration_history and not calibration_error:
            gaps = [abs(float(r.get("avg_predicted", 0.0)) - float(r.get("realized_frequency", 0.0)))
                    for r in calibration_history
                    if r.get("avg_predicted") is not None
                    and r.get("realized_frequency") is not None]
            calibration_error = (sum(gaps) / len(gaps)) if gaps else 0.0

        # --- signal blend (weights depend only on intrinsic signal quality) ---
        weights = {"market": 0.0, "model": 0.0, "research": 0.0, "chainlink": 0.0}
        contributions: list[tuple[float, float]] = []
        if p_model is not None:
            w = self.model_weight
            weights["model"] = round(w, 6)
            contributions.append((w, _clamp01(p_model + category_bias)))
        if p_research is not None and research_usable:
            w = self.research_weight * _clamp(research_confidence)
            weights["research"] = round(w, 6)
            contributions.append((w, _clamp01(p_research)))
        if chainlink_linked and not chainlink_no_trade and chainlink_confidence > 0.0:
            w = self.chainlink_weight * _clamp(chainlink_confidence)
            weights["chainlink"] = round(w, 6)
            contributions.append((w, _clamp01(pm + chainlink_adjustment)))
        signal_w = sum(w for w, _ in contributions)
        weights["market"] = round(max(0.0, 1.0 - signal_w), 6)

        raw_deviation = sum(w * (target - pm) for w, target in contributions)
        p_signal = _clamp01(pm + raw_deviation)

        # --- conservatism multiplier (all regime/quality dampeners) ---
        if chainlink_linked and chainlink_no_trade:
            conservatism = 0.0          # stale/blocked oracle -> pin to market
        else:
            conservatism = self.base_shrink
            conservatism *= _clamp(1.0 - spread / self.max_spread)
            conservatism *= _liq_quality(liquidity_usd)
            conservatism *= (0.3 + 0.7 * _clamp(evidence_score))
            conservatism *= (1.0 - _clamp(ambiguity_score))
            conservatism *= (1.0 - _clamp(stale_score))
            conservatism *= _clamp(1.0 - 2.0 * max(0.0, calibration_error))
            conservatism *= _ttr_factor(time_to_resolution_s)
            conservatism = max(self.min_shrink, min(self.max_shrink, conservatism))

        p_ensemble = _clamp01(pm + conservatism * (p_signal - pm))
        uncertainty = decompose_uncertainty(
            spread=spread, max_spread=self.max_spread,
            calibration_error=calibration_error,
            model_has_edge=(p_model is not None and abs((p_model or pm) - pm) >= 0.005),
            research_usable=research_usable, confidence=research_confidence,
            evidence_score=evidence_score, chainlink_confidence=chainlink_confidence,
            chainlink_linked=chainlink_linked, chainlink_no_trade=chainlink_no_trade,
            liquidity_usd=liquidity_usd, ambiguity_score=ambiguity_score,
            stale_score=stale_score)

        logger.debug("dynamic_ensemble pm=%.4f p_signal=%.4f conservatism=%.4f "
                     "p_ensemble=%.4f", pm, p_signal, conservatism, p_ensemble)
        return {
            "p_ensemble": round(p_ensemble, 6),
            "p_signal": round(p_signal, 6),
            "raw_deviation": round(raw_deviation, 6),
            "conservatism": round(conservatism, 6),
            "weights": weights,
            "uncertainty": uncertainty,
            "version": self.version,
        }


def _clamp01(p: float) -> float:
    try:
        return min(1.0, max(0.0, float(p)))
    except (TypeError, ValueError):
        return 0.5


# --------------------------------------------------------------------------- #
# Bregman arbitrage fair-probability preparation
# --------------------------------------------------------------------------- #
def prepare_bregman_fair_probabilities(estimates: list[dict], *,
                                       exclude_no_trade: bool = True,
                                       prob_key: str = "calibrated_probability"
                                       ) -> dict:
    """Group probability estimates by ``bregman_group_id`` and prepare consistent
    fair-probability inputs for a downstream Bregman-arbitrage projection.

    For each group of related markets (typically markets linked to the same
    Chainlink oracle feed) this computes the **Bregman / squared-loss centroid**
    (the arithmetic mean of the member fair probabilities) and each member's
    deviation from it. This is *input preparation only*: it does NOT execute,
    size, approve, or override certified arbitrage math — it merely organizes the
    fair-probability set so a separate, certified arbitrage routine can project
    onto a consistent simplex.

    Markets flagged with a ``no_trade_probability_reason`` are excluded when
    ``exclude_no_trade`` is set (stale-oracle markets must not pollute the group
    fair value).
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in estimates or []:
        gid = e.get("bregman_group_id")
        if not gid:
            continue
        if exclude_no_trade and e.get("no_trade_probability_reason"):
            continue
        p = e.get(prob_key)
        if p is None:
            p = e.get("p_final")
        if p is None:
            continue
        groups[gid].append({"market_id": e.get("market_id", ""), "fair_probability": float(p)})

    out: dict[str, dict] = {}
    for gid, members in groups.items():
        ps = [m["fair_probability"] for m in members]
        consensus = sum(ps) / len(ps)
        dispersion = (sum((p - consensus) ** 2 for p in ps) / len(ps)) ** 0.5
        for m in members:
            m["deviation"] = round(m["fair_probability"] - consensus, 6)
        out[gid] = {
            "group_id": gid,
            "n": len(members),
            "consensus": round(consensus, 6),
            "dispersion": round(dispersion, 6),
            "members": members,
        }
    return out
