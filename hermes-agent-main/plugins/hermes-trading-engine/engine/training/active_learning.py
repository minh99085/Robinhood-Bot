"""Active-learning candidate selection for aggressive PAPER mode.

Aggressive paper training should trade MORE — but *usefully*: every extra paper
trade should be where the model learns the most, never a reckless bypass of a
risk gate. This module scores the *feedback value* of each candidate and fills
the unused paper budget (after Bregman P1 + edge P2/P3) with the highest-value,
gate-passing near-misses, subject to a strict paper-only exploration budget and
diversity / per-category sample-target constraints.

Selection priority (unchanged): **Bregman arbitrage is priority 1** (reserved
first), edge trades are exploitation, and active learning only ever fills *idle*
budget with small exploratory trades on candidates that already passed EVERY
hard gate (a near-miss). A hard-gate rejection (stale book, invalid market,
stale/irrelevant Chainlink, thin depth, wide spread, ambiguity, risk cap, ...)
can never be selected.

Quant scope documented here:

* Data Acquisition & Ingestion — consumes the scanner's ranked candidates +
  per-candidate edge evaluations (offline; no network).
* Feature Engineering — ``feedback_value_score`` combines uncertainty, category
  under-sampling, liquidity quality, time-to-resolution, Chainlink relevance,
  calibration weakness, Bregman-group relevance, and expected label availability.
* Statistical Modeling — feedback value targets samples that most reduce the
  learner's calibration error (active learning).
* Signal Generation — selection output drives which paper trades open.
* Bregman arbitrage priority — P1 slots are reserved before any exploration.
* Risk Management — exploration NEVER bypasses a hard gate; sizes clamp to the
  paper order-notional ceiling; a strict exploration budget caps total risk.
* Portfolio Optimization — diversity caps stop over-trading one event/category;
  per-category sample targets steer coverage.
* Backtesting / Robustness — deterministic, tie-broken by market_id.
* CLOB v2 simulation — exploratory trades still route through the normal paper
  OMS/broker (this module only SELECTS; it never sizes/approves/places orders).
* Monitoring — rich diagnostics (skipped / edge / feedback / bregman / hard gate).
* Compliance / Security / Operational Excellence — PAPER ONLY, no secrets, no
  network, fully auditable component breakdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .edge_engine import TRADE_REASON, is_hard_gate_reason, is_near_miss


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x or 0.0)))


@dataclass
class FeedbackValueWeights:
    """Weights for :func:`feedback_value_score` (sum to 1.0)."""

    uncertainty: float = 0.20
    category_undersampling: float = 0.18
    liquidity_quality: float = 0.12
    time_to_resolution: float = 0.10
    chainlink_relevance: float = 0.12
    calibration_gap: float = 0.13
    bregman_relevance: float = 0.08
    expected_label_availability: float = 0.07

    def total(self) -> float:
        return round(self.uncertainty + self.category_undersampling
                     + self.liquidity_quality + self.time_to_resolution
                     + self.chainlink_relevance + self.calibration_gap
                     + self.bregman_relevance + self.expected_label_availability, 10)


_DEFAULT_WEIGHTS = FeedbackValueWeights()


def _ttr_component(time_to_resolution_s: Optional[float]) -> float:
    """Prefer markets that resolve *soon* (label arrives faster) but not so soon
    there's no time to act. None -> neutral."""
    if time_to_resolution_s is None:
        return 0.4
    days = max(0.0, float(time_to_resolution_s) / 86400.0)
    if days < 0.5:
        return 0.3
    return _clamp01(1.0 - (days - 0.5) / 30.0)


def feedback_value_score(*, uncertainty: float, category_samples: float,
                         category_target: float, liquidity_quality: float,
                         time_to_resolution_s: Optional[float],
                         chainlink_relevance: float, calibration_gap: float,
                         bregman_relevance: float,
                         expected_label_availability: float,
                         weights: Optional[FeedbackValueWeights] = None) -> tuple:
    """Return (score in [0,1], components dict). Higher = more to learn.

    Each component is normalized to [0,1] then weighted. ``calibration_gap`` is
    normalized against a 0.25 reference (a large reliability gap). Category
    under-sampling is ``1 - samples/target`` (0 once a category hits its target,
    so aggressive mode stops over-sampling it)."""
    w = weights or _DEFAULT_WEIGHTS
    target = float(category_target) if category_target else 0.0
    comp = {
        "uncertainty": _clamp01(uncertainty),
        "category_undersampling": _clamp01(1.0 - (float(category_samples) / target))
        if target > 0 else 0.0,
        "liquidity_quality": _clamp01(liquidity_quality),
        "time_to_resolution": _ttr_component(time_to_resolution_s),
        "chainlink_relevance": _clamp01(chainlink_relevance),
        "calibration_gap": _clamp01(float(calibration_gap) / 0.25),
        "bregman_relevance": _clamp01(bregman_relevance),
        "expected_label_availability": _clamp01(expected_label_availability),
    }
    score = (w.uncertainty * comp["uncertainty"]
             + w.category_undersampling * comp["category_undersampling"]
             + w.liquidity_quality * comp["liquidity_quality"]
             + w.time_to_resolution * comp["time_to_resolution"]
             + w.chainlink_relevance * comp["chainlink_relevance"]
             + w.calibration_gap * comp["calibration_gap"]
             + w.bregman_relevance * comp["bregman_relevance"]
             + w.expected_label_availability * comp["expected_label_availability"])
    return round(_clamp01(score), 6), {k: round(v, 6) for k, v in comp.items()}


@dataclass
class ActiveLearningResult:
    selected: list = field(default_factory=list)   # [{market_id, mode, notional, ...}]
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"selected": list(self.selected), "diagnostics": dict(self.diagnostics)}


class ActiveLearningSelector:
    """Select which candidates open as paper trades, by priority then feedback
    value, under a strict paper-only exploration budget + diversity caps."""

    def __init__(self, cfg=None, learner=None):
        self.cfg = cfg
        self.learner = learner

    def _g(self, name: str, default):
        return getattr(self.cfg, name, default) if self.cfg is not None else default

    def _feedback_value(self, c: dict) -> float:
        fv = c.get("feedback_value")
        if fv is not None:
            return float(fv)
        feats = c.get("features")
        if isinstance(feats, dict):
            try:
                return feedback_value_score(**feats)[0]
            except TypeError:
                return 0.0
        return 0.0

    def score_candidate(self, *, rec, est, edge, reason: str, learner=None,
                        now: Optional[float] = None) -> dict:
        """PASS-6: per-candidate active-learning score + classification.

        Explainable score (each term in [0,1], penalties subtracted):
            active_learning_score = uncertainty + calibration_gap + category_under
              + disagreement + near_miss_profitability + execution_quality
              - ambiguity_penalty - stale_penalty - spread_penalty - depth_penalty
        Returns the score, components, ``learning_bucket``, ``active_learning_reason``,
        and ``expected_information_value``. Read-only — never sizes or places."""
        cfg = self.cfg
        learner = learner or self.learner
        max_spread = max(1e-6, float(self._g("exploration_max_spread",
                                              self._g("max_spread", 0.08))))
        min_depth = float(self._g("exploration_min_depth_at_price",
                                  self._g("min_depth_at_price", 25.0)))
        target = float(self._g("category_sample_target", 50))
        min_edge_floor = float(self._g("min_after_cost_edge", 0.01)) or 0.01

        uncertainty = _clamp01(getattr(est, "total_uncertainty", None)
                               if getattr(est, "total_uncertainty", None) is not None
                               else (1.0 - float(getattr(est, "confidence", 0.5) or 0.5)))
        mid = float(getattr(est, "p_market_mid", 0.5) or 0.5)
        calib_gap = 0.0
        if learner is not None and hasattr(learner, "calibration_gap_at"):
            try:
                calib_gap = float(learner.calibration_gap_at(mid) or 0.0)
            except Exception:  # noqa: BLE001
                calib_gap = 0.0
        calibration_gap_score = _clamp01(calib_gap / 0.25)
        cat = getattr(rec, "category", None)
        samples = 0
        if learner is not None and hasattr(learner, "category_samples"):
            try:
                samples = int(learner.category_samples(cat) or 0)
            except Exception:  # noqa: BLE001
                samples = 0
        category_under = _clamp01(1.0 - samples / target) if target > 0 else 0.0
        p_final = float(getattr(edge, "p_final", mid) or mid)
        disagreement = _clamp01(abs(p_final - mid) / 0.20)
        net_edge = float(getattr(edge, "net_edge", 0.0) or 0.0)
        near_miss_profit = _clamp01(net_edge / min_edge_floor) if net_edge > 0 else 0.0
        spread = float(getattr(est, "spread", 0.0) or 0.0)
        depth = float(getattr(rec, "top_depth_usd", 0.0) or 0.0)
        fresh = bool(getattr(est, "fresh_book", True))
        amb = float(getattr(est, "ambiguity_score", 0.0) or 0.0)
        exec_quality = _clamp01((1.0 - min(1.0, spread / max_spread))
                                * (1.0 if depth >= min_depth else depth / max(1e-9, min_depth))
                                * (1.0 if fresh else 0.0))
        ambiguity_penalty = _clamp01(amb)
        stale_penalty = 0.0 if fresh else 0.5
        spread_penalty = _clamp01(spread / max_spread) * 0.3
        depth_penalty = 0.0 if depth >= min_depth else _clamp01(1.0 - depth / max(1e-9, min_depth)) * 0.3
        comps = {
            "uncertainty_score": round(uncertainty, 4),
            "calibration_gap_score": round(calibration_gap_score, 4),
            "category_under_sample_score": round(category_under, 4),
            "disagreement_score": round(disagreement, 4),
            "near_miss_profitability_score": round(near_miss_profit, 4),
            "execution_quality_score": round(exec_quality, 4),
            "ambiguity_penalty": round(ambiguity_penalty, 4),
            "stale_book_penalty": round(stale_penalty, 4),
            "spread_penalty": round(spread_penalty, 4),
            "depth_penalty": round(depth_penalty, 4),
        }
        score = (uncertainty + calibration_gap_score + category_under + disagreement
                 + near_miss_profit + exec_quality
                 - ambiguity_penalty - stale_penalty - spread_penalty - depth_penalty)
        # classify the dominant learning bucket (eligible buckets only)
        ranked = sorted(
            [("near_miss_positive_edge", near_miss_profit),
             ("model_uncertain_high_liquidity", uncertainty * _clamp01(depth / 50_000.0 + 0.2)),
             ("category_under_sampled", category_under),
             ("calibration_gap_bucket", calibration_gap_score),
             ("chainlink_disagreement_case", disagreement)],
            key=lambda kv: kv[1], reverse=True)
        bucket, top_val = ranked[0]
        if top_val <= 0.0:
            bucket = "not_eligible_for_learning"
        return {
            "active_learning_score": round(score, 6),
            "active_learning_components": comps,
            "learning_bucket": bucket,
            "active_learning_reason": f"{bucket}:{round(top_val, 4)}",
            "expected_information_value": round(_clamp01(score / 6.0), 6),
            "uncertainty_score": comps["uncertainty_score"],
            "execution_quality_score": comps["execution_quality_score"],
        }

    def select(self, candidates: list, *, budget: int, bregman_selected: int = 0,
               now: Optional[float] = None, category_counts: Optional[dict] = None,
               exploration_budget_usd: Optional[float] = None,
               exploration_notional_usd: Optional[float] = None) -> ActiveLearningResult:
        enabled = bool(self._g("active_learning_enabled", False))
        min_edge = float(self._g("exploration_min_edge", -0.05))
        split = max(0.0, min(1.0, float(self._g("exploration_split", 0.5))))
        target = float(self._g("category_sample_target", 50))
        max_cat = int(self._g("max_explore_per_category", 3))
        max_evt = int(self._g("max_explore_per_event", 1))
        notional = float(exploration_notional_usd
                         if exploration_notional_usd is not None
                         else self._g("exploration_notional_usd", 2.0))
        # PAPER hard clamp: exploratory size can NEVER exceed the paper
        # order-notional ceiling (cannot bypass risk caps).
        cap = self._g("max_order_notional_usd", None)
        if cap is not None:
            notional = min(notional, float(cap))
        budget_usd = float(exploration_budget_usd
                           if exploration_budget_usd is not None
                           else self._g("exploration_budget_usd", 20.0))
        category_counts = dict(category_counts or {})
        if not category_counts and self.learner is not None:
            try:
                category_counts = {k: int(v.get("n", 0))
                                   for k, v in getattr(self.learner, "categories", {}).items()}
            except Exception:  # noqa: BLE001
                category_counts = {}

        # --- classify candidates by their edge decision ---
        edge_c: list = []
        feed_c: list = []
        hard = 0
        bregman_n = 0
        skipped = 0
        for c in candidates:
            if c.get("bregman"):
                bregman_n += 1
                continue
            reason = c.get("edge_reason")
            if reason is None and c.get("edge") is not None:
                reason = getattr(c.get("edge"), "reason", None)
            net_edge = c.get("net_edge")
            if net_edge is None and c.get("edge") is not None:
                net_edge = getattr(c.get("edge"), "net_edge", 0.0)
            net_edge = float(net_edge or 0.0)
            if reason == TRADE_REASON:
                edge_c.append(c)
            elif is_hard_gate_reason(reason):
                hard += 1
            elif is_near_miss(reason):
                if net_edge >= min_edge:
                    feed_c.append(c)
                else:
                    skipped += 1
            else:
                skipped += 1

        bregman_reserved = max(int(bregman_selected), bregman_n)
        slots = max(0, int(budget) - bregman_reserved)

        # Exploration/exploitation split: when active learning is on AND there are
        # eligible near-misses, RESERVE up to split*slots for exploration so
        # aggressive mode always learns even when edge trades exist.
        reserved_explore = int(slots * split) if (enabled and feed_c) else 0
        max_edge = max(0, slots - reserved_explore)

        selected: list = []
        # 1) exploitation — edge trades (highest net edge first)
        for c in sorted(edge_c, key=lambda c: (-float(c.get("net_edge") or 0.0), c["market_id"])):
            if len([s for s in selected if s["mode"] == "edge"]) >= max_edge:
                break
            selected.append({"market_id": c["market_id"], "mode": "edge",
                             "category": c.get("category"), "group_key": c.get("group_key"),
                             "net_edge": float(c.get("net_edge") or 0.0),
                             "feedback_value": self._feedback_value(c),
                             "notional": float(self._g("fixed_notional_usd", notional))})

        # 2) exploration — fill remaining idle slots by feedback value
        diversity_skipped = 0
        budget_skipped = 0
        used_usd = 0.0
        if enabled:
            explore_slots = slots - len(selected)
            per_cat: dict = {}
            per_evt: dict = {}

            def _adj(c: dict) -> float:
                fv = self._feedback_value(c)
                cat = c.get("category")
                # per-category sample target: a category already at/over target is
                # strongly deprioritized so aggressive mode broadens coverage.
                if target > 0 and category_counts.get(cat, 0) >= target:
                    fv *= 0.01
                return fv

            for c in sorted(feed_c, key=lambda c: (-_adj(c), c["market_id"])):
                if len([s for s in selected if s["mode"] == "feedback"]) >= explore_slots:
                    break
                cat, grp = c.get("category"), c.get("group_key")
                if per_cat.get(cat, 0) >= max_cat:
                    diversity_skipped += 1
                    continue
                if per_evt.get(grp, 0) >= max_evt:
                    diversity_skipped += 1
                    continue
                if used_usd + notional > budget_usd + 1e-9:
                    budget_skipped += 1
                    continue
                used_usd += notional
                per_cat[cat] = per_cat.get(cat, 0) + 1
                per_evt[grp] = per_evt.get(grp, 0) + 1
                selected.append({"market_id": c["market_id"], "mode": "feedback",
                                 "category": cat, "group_key": grp,
                                 "net_edge": float(c.get("net_edge") or 0.0),
                                 "feedback_value": self._feedback_value(c),
                                 "notional": round(notional, 6)})

        n_edge = sum(1 for s in selected if s["mode"] == "edge")
        n_feed = sum(1 for s in selected if s["mode"] == "feedback")
        feedback_values = [s["feedback_value"] for s in selected if s["mode"] == "feedback"]
        diagnostics = {
            "candidates_skipped": len(candidates) - n_edge - n_feed - bregman_n - hard,
            "selected_for_edge": n_edge,
            "selected_for_feedback": n_feed,
            "selected_for_bregman": bregman_reserved,
            "rejected_by_hard_gate": hard,
            "exploration_budget_used": round(used_usd, 6),
            "exploration_budget_usd": round(budget_usd, 6),
            "exploration_notional_usd": round(notional, 6),
            "diversity_skipped": diversity_skipped,
            "budget_skipped": budget_skipped,
            "slots": slots,
            "slots_used": len(selected),
            "reserved_explore": reserved_explore,
            "feedback_value_sum": round(sum(feedback_values), 6),
            "feedback_per_risk_unit": round(sum(feedback_values) / used_usd, 6)
            if used_usd > 0 else 0.0,
        }
        return ActiveLearningResult(selected=selected, diagnostics=diagnostics)
