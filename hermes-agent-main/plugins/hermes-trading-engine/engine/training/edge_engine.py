"""True executable-edge engine (v2).

Trades are only opened on genuine net edge after costs + uncertainty — never on
price extremes. For BUY YES the executable price is the best ASK; for BUY NO it
is derived from the YES book (``1 - best_bid_yes``) when a dedicated NO book is
not modelled.

    gross_edge       = p_final(outcome) - executable_price
    net_edge         = gross_edge - (spread + slippage + ambiguity + stale +
                                     evidence + calibration + liquidity + fee) penalties
    uncertainty_band = base + ambiguity + spread + stale + evidence + calibration terms
    TRADE only if net_edge > min_net_edge + uncertainty_band  (and all gates pass)

Quant scope — *Risk Management & Portfolio Optimization* (net-edge-after-costs +
uncertainty-band gating), *Execution Engine CLOB v2 simulation* (executable
best-ask / derived NO book pricing), and *Backtesting & Simulation*. A stale or
inconsistent Chainlink oracle on a LINKED market is a hard no-trade gate; new
checks can only make the engine MORE selective, never more aggressive. PAPER
ONLY — this engine evaluates edge, it never submits, sizes, approves, or arms an
order.

In the priority hierarchy this net-edge-after-costs is the gate for both
priority-2 calibrated statistical mispricing and priority-3 directional
predictive trades; :mod:`engine.training.signal_resolver` classifies which of the
two a passing edge represents (priority-1 certified Bregman arbitrage preempts
both).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.markets import universe_manager as um
from engine.research.ambiguity import confident_but_ambiguous

from .probability_stack import ProbabilityEstimate, _liq_quality

# Canonical no-trade reason vocabulary (v2).
NO_TRADE_REASONS = (
    "no_fresh_book", "no_bbo", "no_executable_price",
    "no_model_or_research_probability", "offline_stub_blocked", "edge_too_low",
    "uncertainty_too_high", "spread_too_wide", "depth_too_thin",
    "ambiguity_too_high", "evidence_too_weak", "stale_research",
    "duplicate_event_exposure", "max_open_trades", "risk_rejected",
    "paperbroker_rejected", "chainlink_stale_or_irrelevant",
    # market-dependency-graph correlated-cluster exposure cap (additive; only
    # ever makes the gate stricter — see engine.training.dependency_graph).
    "correlated_cluster_exposure",
    # research is advisory-only: a HIGH-confidence research estimate on an
    # ambiguous market is held to a stricter ambiguity bar (research can never
    # override settlement ambiguity). Additive — only ever stricter.
    "research_confident_but_ambiguous",
    # profitability governor: edge does not survive costs/timing, or the market is
    # graylisted/blacklisted for repeated bad after-cost behaviour. Hard gate —
    # only ever makes the engine MORE selective.
    "negative_after_cost_edge", "graylisted_market",
)

# Profitability-governor hard-gate reasons (a candidate rejected here is NEVER
# live-ready; aggressive paper may still explore a graylisted market tiny+labeled).
PROFITABILITY_GATE_REASONS = frozenset({"negative_after_cost_edge", "graylisted_market"})


def is_profitability_gate_reason(reason: str) -> bool:
    return reason in PROFITABILITY_GATE_REASONS

# Weak-research reasons: in AGGRESSIVE paper mode these near-quality misses may
# be explored with a tiny, explicitly-labelled paper trade (never bypassing a
# hard risk/quality gate). They are NOT explorable in conservative mode.
WEAK_RESEARCH_REASONS = frozenset({"evidence_too_weak", "no_model_or_research_probability"})

# The decisive trade reason and the two NEAR-MISS reasons. A near-miss passed
# EVERY hard gate (fresh book, executable price, spread, depth, ambiguity,
# Chainlink, research, duplicate-event, max-open-trades, risk) but did not clear
# the net-edge / uncertainty threshold. Active learning may only ever fill paper
# budget from near-misses — never from a hard-gate rejection.
TRADE_REASON = "trade"
NEAR_MISS_REASONS = frozenset({"edge_too_low", "uncertainty_too_high"})
HARD_GATE_REASONS = frozenset(set(NO_TRADE_REASONS) - NEAR_MISS_REASONS)


def is_hard_gate_reason(reason: str) -> bool:
    """True when a candidate was rejected by a mandatory risk/quality gate and is
    therefore NEVER eligible for active-learning exploration (stale book, invalid
    market, stale/irrelevant Chainlink, thin depth, wide spread, risk cap, ...)."""
    return reason in HARD_GATE_REASONS


def is_near_miss(reason: str) -> bool:
    """True when a candidate passed all hard gates but missed the edge threshold —
    i.e. it is safe + eligible for an exploratory (feedback) paper trade."""
    return reason in NEAR_MISS_REASONS


def is_weak_research_reason(reason: str) -> bool:
    """True for a weak/absent-research rejection (eligible for tiny aggressive
    exploration only — never a stale-book/ambiguity/risk hard gate)."""
    return reason in WEAK_RESEARCH_REASONS


def is_explorable(reason: str, *, aggressive_weak_research: bool = False) -> bool:
    """Whether a rejected candidate may receive a tiny exploratory paper trade.

    Always true for near-misses (passed every hard gate). In AGGRESSIVE mode it is
    additionally true for weak-research rejections — but NEVER for a hard
    risk/quality gate (stale book, thin depth, wide spread, ambiguity, stale or
    irrelevant Chainlink, correlated-cluster cap, max-open, risk rejection)."""
    if is_near_miss(reason):
        return True
    return bool(aggressive_weak_research) and is_weak_research_reason(reason)


def bregman_preempts_directional(opp) -> bool:
    """Whether a Bregman opportunity may PREEMPT all directional trades.

    Bregman is the flagship priority-1 strategy, but it only outranks directional
    edge AFTER its certificate passes: certified, strictly-positive profit lower
    bound, and (when attached) a proven risk-free full hedge. A non-certified
    candidate never preempts — directional evaluation proceeds normally. Quant
    scope — *Signal Generation (Bregman priority)* + *Compliance/Security*."""
    if opp is None:
        return False
    cert = getattr(opp, "certificate", None)
    if cert is not None and not bool(getattr(cert, "risk_free", False)):
        return False
    return bool(getattr(opp, "certified", False)
                and float(getattr(opp, "profit_lower_bound", 0.0) or 0.0) > 0.0)


def overfit_adjusted_min_edge(base: float, penalty: float, *,
                              conservative: float = 0.03) -> float:
    """Raise the minimum net-edge threshold toward a conservative value as the
    overfit penalty rises (anti-overfitting). ``penalty=0`` keeps the aggressive
    threshold; ``penalty=1`` reverts to ``conservative``. This only ever makes
    the entry gate STRICTER, never looser."""
    p = max(0.0, min(1.0, float(penalty)))
    return (1.0 - p) * float(base) + p * float(conservative)


@dataclass
class EdgeResult:
    market_id: str
    outcome: str
    side: str
    executable_price: Optional[float]
    p_final: float
    gross_edge: float
    cost_penalty: float
    net_edge: float
    uncertainty_band: float
    threshold: float
    should_trade: bool
    reason: str
    cost_components: dict = field(default_factory=dict)
    band_components: dict = field(default_factory=dict)
    # 6A: CONSERVATIVE after-cost edge using the unfavorable ensemble CI bound of the
    # probability (net_edge minus the CI half-width on the taken side). A readiness trade
    # is only "credible positive expectancy" when this lower bound clears the floor.
    after_cost_edge_lower_bound: float = 0.0
    credible_margin: float = 0.0
    credible_positive_expectancy: bool = False
    # Chainlink advisory diagnostics (0/""/False when Chainlink not wired)
    chainlink_confidence: float = 0.0
    chainlink_feed: str = ""
    chainlink_no_trade: bool = False
    # controlled-experiment strategy-variant annotation (set by the trainer when
    # experiments are enabled; additive — never affects the edge math).
    strategy_variant: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 5)
        return d


class EdgeEngine:
    def __init__(self, cfg):
        self.cfg = cfg

    def _no_book_prices(self, rec) -> tuple:
        """Return (no_best_ask, no_best_bid) derived from the YES book."""
        ybid = um._as_float(rec.raw.get("bestBid"), 0.0)
        yask = um._as_float(rec.raw.get("bestAsk"), 0.0)
        no_ask = (1.0 - ybid) if ybid else 0.0   # buy NO = sell YES at YES bid
        no_bid = (1.0 - yask) if yask else 0.0
        return no_ask, no_bid

    def evaluate(self, est: ProbabilityEstimate, rec, *, outcome: str = "YES",
                 open_event_groups: Optional[set] = None, open_trades: int = 0,
                 cluster_id: Optional[str] = None,
                 open_clusters: Optional[set] = None) -> EdgeResult:
        cfg = self.cfg
        outcome = outcome.upper()
        if outcome == "NO":
            no_ask, _ = self._no_book_prices(rec)
            executable_price = no_ask if no_ask > 0 else None
            p_final = 1.0 - est.p_final
        else:
            executable_price = est.best_ask
            p_final = est.p_final

        cl_conf = float(getattr(est, "chainlink_confidence", 0.0))
        cl_feed = str(getattr(est, "chainlink_feed", ""))
        cl_block = bool(getattr(est, "chainlink_no_trade", False))

        def no(reason: str) -> EdgeResult:
            return EdgeResult(
                market_id=est.market_id, outcome=outcome, side="BUY",
                executable_price=executable_price, p_final=p_final, gross_edge=0.0,
                cost_penalty=0.0, net_edge=0.0, uncertainty_band=0.0,
                threshold=float(cfg.min_net_edge), should_trade=False, reason=reason,
                chainlink_confidence=cl_conf, chainlink_feed=cl_feed,
                chainlink_no_trade=cl_block)

        # ---- hard gates ----
        # Chainlink risk gate: a stale/missing/inconsistent oracle on a LINKED
        # market blocks the trade (set only when the Chainlink scanner is wired).
        if getattr(est, "chainlink_no_trade", False):
            return no("chainlink_stale_or_irrelevant")
        if not est.fresh_book:
            return no("no_fresh_book")
        if executable_price is None or executable_price <= 0:
            return no("no_executable_price")
        if est.spread > float(cfg.max_spread):
            return no("spread_too_wide")
        if rec.top_depth_usd < float(cfg.min_depth_at_price):
            return no("depth_too_thin")
        if est.ambiguity_score > float(cfg.max_ambiguity_score):
            return no("ambiguity_too_high")
        # Research is advisory-only: a high-confidence research estimate on an
        # ambiguous market is held to a stricter ambiguity bar. Research
        # confidence can NEVER override settlement ambiguity (only ever stricter).
        if est.research_usable and confident_but_ambiguous(
                getattr(est, "confidence", 0.0), est.ambiguity_score,
                high_confidence=float(getattr(cfg, "research_high_confidence", 0.8)),
                ambiguity_threshold=float(cfg.max_ambiguity_score),
                confident_frac=float(getattr(cfg, "research_confident_ambiguity_frac", 0.6))):
            return no("research_confident_but_ambiguous")
        if (est.research_source == "offline_research_stub"
                and not bool(cfg.allow_offline_stub_trading) and not est.model_has_edge):
            return no("offline_stub_blocked")
        if bool(cfg.require_research_or_model_edge) and not (
                est.research_usable or est.model_has_edge):
            return no("no_model_or_research_probability")
        if est.research_usable and est.evidence_score < float(cfg.min_evidence_score):
            return no("evidence_too_weak")
        if getattr(est, "research_age_s", None) is not None and \
                est.research_age_s > float(cfg.research_max_age_s):
            return no("stale_research")
        if open_event_groups is not None and rec.group_key in open_event_groups:
            return no("duplicate_event_exposure")
        # Market-dependency-graph correlated-cluster cap: refuse a trade that would
        # add to an already-open correlated cluster (prevents overexposure to one
        # cluster). Optional + additive — only tightens when the trainer wires a
        # graph cluster map; default (None) is a no-op.
        if open_clusters and cluster_id is not None and cluster_id in open_clusters:
            return no("correlated_cluster_exposure")
        if open_trades >= int(cfg.max_open_trades):
            return no("max_open_trades")

        # ---- edge math ----
        gross_edge = p_final - executable_price
        low_liq = 1.0 - _liq_quality(est.liquidity_usd)
        cost_components = {
            "fee": float(cfg.taker_fee_bps) / 10000.0,
            "spread": float(cfg.spread_penalty_weight) * est.spread,
            "slippage": float(cfg.slippage_penalty_weight) * (float(cfg.slippage_bps) / 10000.0),
            "ambiguity": float(cfg.ambiguity_penalty_weight) * est.ambiguity_score,
            "stale": float(cfg.stale_penalty_weight) * est.stale_score,
            "evidence": float(cfg.evidence_penalty_weight) * (1.0 - est.evidence_score),
            "calibration": float(cfg.calibration_penalty_weight) * est.calibration_error,
            "liquidity": float(cfg.liquidity_penalty_weight) * low_liq,
        }
        cost_penalty = sum(cost_components.values())

        band_components = {
            "base": float(cfg.base_uncertainty),
            "ambiguity": float(cfg.ambiguity_uncertainty_weight) * est.ambiguity_score,
            "spread": float(cfg.spread_uncertainty_weight) * est.spread,
            "stale": float(cfg.stale_uncertainty_weight) * est.stale_score,
            "evidence": float(cfg.evidence_uncertainty_weight) * (1.0 - est.evidence_score),
            "calibration": float(cfg.calibration_uncertainty_weight) * est.calibration_error,
        }
        uncertainty_band = sum(band_components.values())

        net_edge = gross_edge - cost_penalty
        threshold = float(cfg.min_net_edge) + uncertainty_band
        if net_edge <= float(cfg.min_net_edge):
            reason = "edge_too_low"
            should = False
        elif net_edge <= threshold:
            reason = "uncertainty_too_high"
            should = False
        else:
            reason = "trade"
            should = True
        # 6A: conservative after-cost edge using the UNFAVORABLE ensemble CI bound for the
        # taken side. margin = how far the probability could be against us (CI half-width
        # on this side); after_cost_edge_lower_bound = net_edge - margin. A trade only has
        # "credible positive expectancy" when this lower bound clears the credible floor.
        ci_lo = float(getattr(est, "confidence_interval_low", 0.0) or 0.0)
        ci_hi = float(getattr(est, "confidence_interval_high", 0.0) or 0.0)
        p_yes = float(est.p_final)
        if ci_hi > ci_lo:                      # CI populated -> use it
            margin = max(0.0, (p_yes - ci_lo) if outcome != "NO" else (ci_hi - p_yes))
        else:
            margin = 0.0
        after_cost_lb = round(net_edge - margin, 6)
        min_credible = float(getattr(cfg, "min_credible_after_cost_edge", 0.0))
        credible = bool(after_cost_lb > min_credible)
        return EdgeResult(
            market_id=est.market_id, outcome=outcome, side="BUY",
            executable_price=executable_price, p_final=p_final, gross_edge=gross_edge,
            cost_penalty=cost_penalty, net_edge=net_edge, uncertainty_band=uncertainty_band,
            threshold=threshold, should_trade=should, reason=reason,
            cost_components=cost_components, band_components=band_components,
            after_cost_edge_lower_bound=after_cost_lb, credible_margin=round(margin, 6),
            credible_positive_expectancy=credible,
            chainlink_confidence=cl_conf, chainlink_feed=cl_feed,
            chainlink_no_trade=cl_block)

    def best_side(self, est: ProbabilityEstimate, rec, *, open_event_groups=None,
                  open_trades: int = 0, cluster_id: Optional[str] = None,
                  open_clusters: Optional[set] = None) -> EdgeResult:
        """Evaluate BUY YES and BUY NO; return the tradable side with higher net
        edge (or the YES no-trade result if neither trades)."""
        yes = self.evaluate(est, rec, outcome="YES", open_event_groups=open_event_groups,
                            open_trades=open_trades, cluster_id=cluster_id,
                            open_clusters=open_clusters)
        no = self.evaluate(est, rec, outcome="NO", open_event_groups=open_event_groups,
                           open_trades=open_trades, cluster_id=cluster_id,
                           open_clusters=open_clusters)
        tradables = [r for r in (yes, no) if r.should_trade]
        if tradables:
            return max(tradables, key=lambda r: r.net_edge)
        return yes


def after_cost_capital_edge(edge: EdgeResult, *, fill_failure: float = 0.0,
                            adverse_selection: float = 0.0,
                            timing_decay: float = 0.0) -> float:
    """Realistic AFTER-COST edge for capital allocation.

    Nets the directional ``net_edge`` (already cost-adjusted by the EdgeEngine)
    against the CLOB-v2 + timing costs the pre-trade math does not see (fill
    failure, adverse-selection markout, timing decay). The adaptive capital
    allocator funds only a strictly positive result — read-only, no sizing."""
    base = float(getattr(edge, "net_edge", 0.0) or 0.0)
    extra = (max(0.0, float(fill_failure)) + max(0.0, float(adverse_selection))
             + max(0.0, float(timing_decay)))
    return round(base - extra, 8)
