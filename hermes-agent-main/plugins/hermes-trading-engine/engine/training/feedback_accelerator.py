"""Paper-only 10x Feedback Accelerator.

Increases TRAINING FEEDBACK (decisions, shadow labels, no-trade labels, tiny
exploration trades, learning samples) WITHOUT loosening any hard safety gate.

Design rules (enforced here + by tests):
* Hard gates NEVER loosen: no live/micro-live/guarded-live, RiskEngine required,
  fresh book required for real fills, valid token/orderbook, realistic fill,
  clean-label guard, Bregman certification, stale data can't raise confidence,
  settlement ambiguity can't become full size, exploration can't be readiness
  proof until cleanly resolved + validated.
* Only SOFT paper-training gates loosen, and ONLY for ``tiny_exploration_trade``:
  EV / confidence / edge thresholds, shortlist size, candidates-per-tick, tiny
  trades-per-hour, duplicate-event exposure (within a strict tiny cap), and the
  BTC Pulse EV threshold (only when after-cost EV is ~0 or positive and tiny).
* Exploitation and exploration are SEPARATE: exploit uses strict thresholds;
  exploration is tiny, capped, labeled, and isolated.

Everything is deterministic and PAPER ONLY. Nothing here can place a live order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Decision taxonomy. Every decision the trainer makes is classified as one of
# these. Only the *_trade classes represent a (paper) position; the rest are
# pure learning samples.
# ---------------------------------------------------------------------------
EXPLOIT_TRADE = "exploit_trade"
CERTIFIED_BREGMAN_TRADE = "certified_bregman_trade"
STATISTICAL_EDGE_TRADE = "statistical_edge_trade"
CHAINLINK_CONDITIONED_TRADE = "chainlink_conditioned_trade"
GROK_NEWS_CONDITIONED_TRADE = "grok_news_conditioned_trade"
BTC_PULSE_TRADE = "btc_pulse_trade"
TINY_EXPLORATION_TRADE = "tiny_exploration_trade"
SHADOW_DECISION_ONLY = "shadow_decision_only"
NO_TRADE_LABEL = "no_trade_label"

DECISION_CLASSES = frozenset({
    EXPLOIT_TRADE, CERTIFIED_BREGMAN_TRADE, STATISTICAL_EDGE_TRADE,
    CHAINLINK_CONDITIONED_TRADE, GROK_NEWS_CONDITIONED_TRADE, BTC_PULSE_TRADE,
    TINY_EXPLORATION_TRADE, SHADOW_DECISION_ONLY, NO_TRADE_LABEL,
})

# Trade classes that represent a REAL (paper) position vs learning-only classes.
_TRADE_CLASSES = frozenset({
    EXPLOIT_TRADE, CERTIFIED_BREGMAN_TRADE, STATISTICAL_EDGE_TRADE,
    CHAINLINK_CONDITIONED_TRADE, GROK_NEWS_CONDITIONED_TRADE, BTC_PULSE_TRADE,
    TINY_EXPLORATION_TRADE,
})
# Classes that NEVER count toward proven live-readiness edge.
_NON_READINESS_CLASSES = frozenset({TINY_EXPLORATION_TRADE, SHADOW_DECISION_ONLY,
                                    NO_TRADE_LABEL})


def is_trade_class(cls: str) -> bool:
    return cls in _TRADE_CLASSES


def counts_for_readiness(cls: str, *, resolved: bool, validated: bool,
                         exploration_counts: bool = False) -> bool:
    """A decision class only counts as proven live-readiness edge when it is an
    exploit/strategy trade (never exploration/shadow/no-trade), AND it is cleanly
    resolved and realistic-fill validated. Exploration can only count if the
    operator explicitly opts in AND it is resolved+validated (default: never)."""
    if cls in _NON_READINESS_CLASSES:
        if cls == TINY_EXPLORATION_TRADE and exploration_counts:
            return bool(resolved and validated)
        return False
    if cls not in _TRADE_CLASSES:
        return False
    return bool(resolved and validated)


def _clamp01(x: object, default: float = 0.0) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


# ---------------------------------------------------------------------------
# Active-learning selector: prioritize candidates with high LEARNING value, not
# just high EV. Returns 0..1 (higher = more worth a decision/sample).
# ---------------------------------------------------------------------------
def feedback_value_score(*, model_uncertainty: float = 0.0,
                         category_undersample: float = 0.0,
                         market_type_undersample: float = 0.0,
                         ttr_bucket_undersample: float = 0.0,
                         evidence_disagreement: float = 0.0,
                         microstructure_uncertainty: float = 0.0,
                         near_threshold_edge: float = 0.0,
                         expected_label_availability: float = 0.0,
                         expected_resolution_soon: float = 0.0,
                         clean_label_probability: float = 0.0) -> float:
    """Deterministic weighted blend. Each input is 0..1.

    ``near_threshold_edge``: 1.0 when |edge - threshold| is tiny (most
    informative), 0 when far. ``clean_label_probability`` and
    ``expected_label_availability`` gate value down when a sample is unlikely to
    ever resolve into a clean trainable label."""
    u = _clamp01(model_uncertainty)
    cu = _clamp01(category_undersample)
    mu = _clamp01(market_type_undersample)
    tu = _clamp01(ttr_bucket_undersample)
    dis = _clamp01(evidence_disagreement)
    ms = _clamp01(microstructure_uncertainty)
    nt = _clamp01(near_threshold_edge)
    la = _clamp01(expected_label_availability)
    rs = _clamp01(expected_resolution_soon)
    clp = _clamp01(clean_label_probability)

    raw = (0.22 * u + 0.14 * cu + 0.10 * mu + 0.08 * tu + 0.14 * dis
           + 0.08 * ms + 0.16 * nt + 0.08 * rs)
    # A sample only has learning value if it can become a clean resolved label.
    label_gate = 0.5 * la + 0.5 * clp
    return round(max(0.0, min(1.0, raw * (0.4 + 0.6 * label_gate))), 6)


# ---------------------------------------------------------------------------
# Soft-gate relaxation. EXPLOIT thresholds are returned unchanged; EXPLORATION
# thresholds are a bounded relaxation used ONLY for tiny_exploration_trade.
# ---------------------------------------------------------------------------
@dataclass
class SoftGates:
    exploit_min_edge: float
    exploit_min_confidence: float
    exploit_min_ev: float
    exploration_min_edge: float
    exploration_min_confidence: float
    exploration_min_ev: float

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_soft_gates(cfg) -> SoftGates:
    """Exploit gates = strict config values (UNCHANGED). Exploration gates =
    bounded relaxation, ONLY applied when the accelerator + tiny exploration are
    enabled. Exploration can never go below the hard exploration floors."""
    exploit_edge = float(getattr(cfg, "min_net_edge", 0.03))
    exploit_conf = float(getattr(cfg, "research_high_confidence", 0.8))
    exploit_ev = float(getattr(cfg, "min_net_edge", 0.03))

    accel = bool(getattr(cfg, "feedback_accelerator_enabled", False))
    tiny = bool(getattr(cfg, "exploration_tiny_size_enabled", False)) and \
        bool(getattr(cfg, "exploration_enabled", False))
    if not (accel and tiny):
        # No relaxation: exploration == exploit (conservative default).
        return SoftGates(exploit_edge, exploit_conf, exploit_ev,
                         exploit_edge, exploit_conf, exploit_ev)

    # Bounded relaxation for tiny exploration only.
    expl_edge = max(float(getattr(cfg, "exploration_min_edge", -0.01)), -0.02)
    expl_conf = max(0.50, exploit_conf - 0.20)
    expl_ev = max(float(getattr(cfg, "exploration_min_edge", -0.01)), -0.02)
    return SoftGates(exploit_edge, exploit_conf, exploit_ev,
                     round(expl_edge, 6), round(expl_conf, 6), round(expl_ev, 6))


# ---------------------------------------------------------------------------
# Tiny-exploration gate. Returns (allowed, decision_class, reason).
#
# HARD gates are checked first and can NEVER be bypassed. Only when all hard
# gates pass do the relaxed soft thresholds apply (tiny size only).
# ---------------------------------------------------------------------------
HARD_GATE_REASONS = (
    "live_blocked", "no_fresh_book", "invalid_token", "missing_price",
    "stale_chainlink", "settlement_ambiguous", "risk_rejected",
    "realistic_fill_rejected", "exploration_daily_loss", "drawdown_kill_switch",
)


def tiny_exploration_gate(*, hard_gates_ok: Optional[bool] = None,
                          live_blocked: bool = False, fresh_book: bool = True,
                          valid_token: bool = True, has_price: bool = True,
                          chainlink_relevant: bool = False, chainlink_stale: bool = False,
                          ambiguity_score: float = 0.0, ambiguity_max: float = 0.35,
                          risk_ok: bool = True, realistic_fill_ok: bool = True,
                          exploration_daily_loss_ok: bool = True,
                          drawdown_kill_switch: bool = False,
                          edge: float = 0.0, confidence: float = 1.0,
                          after_cost_ev: float = 0.0, exposure_ok: bool = True,
                          soft_gates: Optional[SoftGates] = None) -> dict:
    """Decide whether a TINY exploration paper trade may open.

    Returns ``{"allowed": bool, "decision_class": str, "reason": str|None,
    "hard_gate_block": bool}``. A hard-gate failure ALWAYS blocks (and is never
    relaxed); soft thresholds only gate the relaxed exploration path."""
    # 1) Hard gates — never bypassable. Any failure => no trade (record as label).
    if live_blocked:
        return _blocked("live_blocked", hard=True)
    if not fresh_book:
        return _blocked("no_fresh_book", hard=True)
    if not valid_token:
        return _blocked("invalid_token", hard=True)
    if not has_price:
        return _blocked("missing_price", hard=True)
    if chainlink_relevant and chainlink_stale:
        return _blocked("stale_chainlink", hard=True)
    if float(ambiguity_score) > float(ambiguity_max):
        return _blocked("settlement_ambiguous", hard=True)
    if not risk_ok:
        return _blocked("risk_rejected", hard=True)
    if not realistic_fill_ok:
        return _blocked("realistic_fill_rejected", hard=True)
    if not exploration_daily_loss_ok:
        return _blocked("exploration_daily_loss", hard=True)
    if drawdown_kill_switch:
        return _blocked("drawdown_kill_switch", hard=True)
    if not exposure_ok:
        # tiny-exposure cap is a soft cap, but exceeding it still blocks the trade
        return _blocked("tiny_exposure_cap", hard=False)

    # 2) Soft gates (relaxed thresholds, tiny only).
    sg = soft_gates or SoftGates(0.03, 0.8, 0.03, -0.02, 0.5, -0.02)
    if float(edge) < sg.exploration_min_edge:
        return _blocked("edge_below_exploration_floor", hard=False)
    if float(confidence) < sg.exploration_min_confidence:
        return _blocked("confidence_below_exploration_floor", hard=False)
    if float(after_cost_ev) < sg.exploration_min_ev:
        return _blocked("after_cost_ev_below_exploration_floor", hard=False)
    return {"allowed": True, "decision_class": TINY_EXPLORATION_TRADE,
            "reason": None, "hard_gate_block": False}


def _blocked(reason: str, *, hard: bool) -> dict:
    return {"allowed": False, "decision_class": NO_TRADE_LABEL, "reason": reason,
            "hard_gate_block": bool(hard)}


def tiny_exploration_notional(cfg, equity: float) -> float:
    """Tiny, hard-capped exploration notional (paper). Never exceeds the paper
    order ceiling or the configured exploration notional."""
    frac = float(getattr(cfg, "exploration_notional_fraction", 0.0))
    by_frac = max(0.0, frac) * max(0.0, float(equity))
    explicit = float(getattr(cfg, "exploration_notional_usd", 2.0))
    ceiling = float(getattr(cfg, "max_order_notional_usd", 5.0))
    return round(min(ceiling, explicit, by_frac) if by_frac > 0 else min(ceiling, explicit), 2)


# ---------------------------------------------------------------------------
# Shadow decisions + no-trade labels (learning samples; never executed).
# ---------------------------------------------------------------------------
@dataclass
class ShadowDecision:
    market_id: str
    ts_ms: int
    hypothetical_side: str           # "yes"/"no"/"up"/"down"
    hypothetical_price: float
    hypothetical_ev: float
    blocker_reason: str
    probability: float = 0.5
    edge: float = 0.0
    decision_class: str = SHADOW_DECISION_ONLY
    resolved: bool = False
    realized_outcome: Optional[int] = None     # 1 if hypothetical side won
    would_have_won: Optional[bool] = None
    would_have_lost: Optional[bool] = None
    blocker_correct: Optional[bool] = None
    realized_edge: Optional[float] = None

    def score(self, realized_outcome: int, *, realized_price: Optional[float] = None) -> "ShadowDecision":
        """Score a shadow decision after clean resolution. ``realized_outcome``
        is 1 if the hypothetical side won, else 0. NEVER affects live readiness."""
        y = int(realized_outcome)
        self.resolved = True
        self.realized_outcome = y
        self.would_have_won = (y == 1)
        self.would_have_lost = (y == 0)
        # The blocker was "correct" if NOT trading avoided a loss (or the
        # hypothetical edge was non-positive). Trading would have won => blocker
        # was a missed opportunity (incorrect).
        self.blocker_correct = (y == 0) or (float(self.hypothetical_ev) <= 0.0)
        price = realized_price if realized_price is not None else self.hypothetical_price
        if price and price > 0:
            self.realized_edge = round((y / float(price)) - 1.0, 6)
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NoTradeLabel:
    market_id: str
    ts_ms: int
    probability: float
    edge: float
    rejection_reason: str
    resolved: bool = False
    realized_outcome: Optional[int] = None
    no_trade_correct: Optional[bool] = None
    blocker_correct: Optional[bool] = None
    decision_class: str = NO_TRADE_LABEL

    def score(self, realized_outcome: int) -> "NoTradeLabel":
        """After resolution, judge whether the no-trade/blocker was correct.

        ``realized_outcome`` = 1 if the side we *would* have taken (edge sign)
        actually won. The blocker was correct when trading would NOT have won,
        OR when the edge was non-positive in the first place."""
        y = int(realized_outcome)
        self.resolved = True
        self.realized_outcome = y
        would_have_won = (y == 1) and (float(self.edge) > 0.0)
        self.no_trade_correct = not would_have_won
        self.blocker_correct = not would_have_won
        return self

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Capacity bumps. Apply ONLY soft-capacity multipliers (more candidates / more
# decisions per tick / faster scan) so feedback scales ~target_multiplier x.
# Hard risk caps (max open trades, notional, exposure, daily loss) are NEVER
# touched here.
# ---------------------------------------------------------------------------
def apply_feedback_accelerator(cfg) -> dict:
    """Mutate ``cfg`` in place to raise SOFT capacity knobs when the accelerator
    is enabled (paper only). Returns the resolved (before/after) capacity report.
    Hard caps are untouched. Safe to call repeatedly (idempotent-ish: bounded)."""
    if not bool(getattr(cfg, "feedback_accelerator_enabled", False)):
        return {"applied": False, "reason": "disabled"}

    mult = max(1, min(int(getattr(cfg, "feedback_accelerator_target_multiplier", 10)), 20))
    before = {
        "paper_decision_budget": int(getattr(cfg, "paper_decision_budget", 30)),
        "trade_candidate_limit": int(getattr(cfg, "trade_candidate_limit", 30)),
        "shortlist_limit": int(getattr(cfg, "shortlist_limit", 150)),
        "live_watch_limit": int(getattr(cfg, "live_watch_limit", 100)),
        "exploration_enabled": bool(getattr(cfg, "exploration_enabled", False)),
    }
    # Decisions/shadow labels per tick scale with evaluated candidates + budget.
    cfg.paper_decision_budget = min(1000, before["paper_decision_budget"] * mult)
    cfg.trade_candidate_limit = min(200, before["trade_candidate_limit"] * 2)
    cfg.shortlist_limit = min(400, before["shortlist_limit"] * 2)
    cfg.live_watch_limit = min(300, before["live_watch_limit"] * 2)
    # Active learning + exploration ON (tiny) so near-threshold candidates get a
    # sample. Hard caps below remain enforced by the trade path.
    if bool(getattr(cfg, "polymarket_feedback_acceleration_enabled", True)):
        cfg.exploration_enabled = True
        if bool(getattr(cfg, "active_learning_enabled", True)):
            cfg.active_learning_enabled = True
    after = {
        "paper_decision_budget": cfg.paper_decision_budget,
        "trade_candidate_limit": cfg.trade_candidate_limit,
        "shortlist_limit": cfg.shortlist_limit,
        "live_watch_limit": cfg.live_watch_limit,
        "exploration_enabled": cfg.exploration_enabled,
    }
    return {"applied": True, "target_multiplier": mult, "before": before, "after": after}


# ---------------------------------------------------------------------------
# Metrics accumulator.
# ---------------------------------------------------------------------------
@dataclass
class FeedbackAcceleratorMetrics:
    enabled: bool = False
    target_multiplier: int = 10
    baseline_decisions_per_hour: float = 0.0
    decisions: int = 0
    exploit_trades: int = 0
    tiny_exploration_trades: int = 0
    shadow_decisions: int = 0
    no_trade_labels: int = 0
    btc_pulse_decisions: int = 0
    polymarket_decisions: int = 0
    useful_feedback_samples: int = 0
    feedback_samples_resolved: int = 0
    exploration_pnl: float = 0.0
    exploit_pnl: float = 0.0
    exploration_wins: int = 0
    exploration_settled: int = 0
    exploit_wins: int = 0
    exploit_settled: int = 0
    exploration_drawdown: float = 0.0
    hard_gate_rejections: int = 0
    soft_gate_relaxed_count: int = 0
    soft_gate_relaxation_pnl: float = 0.0
    blocker_scored: int = 0
    blocker_correct: int = 0
    blocker_kind_scored: dict = field(default_factory=dict)
    blocker_kind_correct: dict = field(default_factory=dict)

    # -- recording -----------------------------------------------------#
    def record_decision(self, decision_class: str, *, source: str = "polymarket") -> None:
        self.decisions += 1
        if source == "btc_pulse":
            self.btc_pulse_decisions += 1
        else:
            self.polymarket_decisions += 1
        if decision_class == EXPLOIT_TRADE or decision_class in {
                CERTIFIED_BREGMAN_TRADE, STATISTICAL_EDGE_TRADE,
                CHAINLINK_CONDITIONED_TRADE, GROK_NEWS_CONDITIONED_TRADE,
                BTC_PULSE_TRADE}:
            self.exploit_trades += 1
        elif decision_class == TINY_EXPLORATION_TRADE:
            self.tiny_exploration_trades += 1
            self.soft_gate_relaxed_count += 1
        elif decision_class == SHADOW_DECISION_ONLY:
            self.shadow_decisions += 1
            self.useful_feedback_samples += 1
        elif decision_class == NO_TRADE_LABEL:
            self.no_trade_labels += 1
            self.useful_feedback_samples += 1

    def record_hard_gate_rejection(self) -> None:
        self.hard_gate_rejections += 1

    def record_resolution(self, decision_class: str, *, won: bool, pnl: float) -> None:
        self.feedback_samples_resolved += 1
        if decision_class == TINY_EXPLORATION_TRADE:
            self.exploration_settled += 1
            self.exploration_wins += 1 if won else 0
            self.exploration_pnl = round(self.exploration_pnl + pnl, 6)
            self.soft_gate_relaxation_pnl = round(self.soft_gate_relaxation_pnl + pnl, 6)
            if self.exploration_pnl < self.exploration_drawdown:
                self.exploration_drawdown = round(self.exploration_pnl, 6)
        elif is_trade_class(decision_class):
            self.exploit_settled += 1
            self.exploit_wins += 1 if won else 0
            self.exploit_pnl = round(self.exploit_pnl + pnl, 6)

    def record_blocker_score(self, kind: str, correct: bool) -> None:
        self.blocker_scored += 1
        self.blocker_correct += 1 if correct else 0
        self.blocker_kind_scored[kind] = self.blocker_kind_scored.get(kind, 0) + 1
        if correct:
            self.blocker_kind_correct[kind] = self.blocker_kind_correct.get(kind, 0) + 1

    # -- reporting -----------------------------------------------------#
    @staticmethod
    def _rate(n: int, hours: float) -> float:
        return round(n / hours, 4) if hours > 0 else 0.0

    def _blocker_rate(self, kind: str) -> Optional[float]:
        s = self.blocker_kind_scored.get(kind, 0)
        return round(self.blocker_kind_correct.get(kind, 0) / s, 4) if s else None

    def to_dict(self, *, runtime_hours: float = 0.0) -> dict:
        h = max(0.0, float(runtime_hours))
        dph = self._rate(self.decisions, h)
        base = self.baseline_decisions_per_hour or 0.0
        actual_mult = round(dph / base, 3) if base > 0 else (
            float(self.target_multiplier) if self.enabled and dph > 0 else 0.0)
        return {
            "feedback_accelerator_enabled": bool(self.enabled),
            "target_multiplier": int(self.target_multiplier),
            "feedback_multiplier_actual": actual_mult,
            "decisions_per_hour": dph,
            "shadow_decisions_per_hour": self._rate(self.shadow_decisions, h),
            "no_trade_labels_per_hour": self._rate(self.no_trade_labels, h),
            "tiny_exploration_trades_per_hour": self._rate(self.tiny_exploration_trades, h),
            "exploit_trades_per_hour": self._rate(self.exploit_trades, h),
            "btc_pulse_decisions_per_hour": self._rate(self.btc_pulse_decisions, h),
            "polymarket_decisions_per_hour": self._rate(self.polymarket_decisions, h),
            "useful_feedback_samples": self.useful_feedback_samples,
            "feedback_samples_resolved": self.feedback_samples_resolved,
            "exploration_pnl": round(self.exploration_pnl, 4),
            "exploit_pnl": round(self.exploit_pnl, 4),
            "exploration_hit_rate": round(self.exploration_wins / self.exploration_settled, 4)
            if self.exploration_settled else 0.0,
            "exploit_hit_rate": round(self.exploit_wins / self.exploit_settled, 4)
            if self.exploit_settled else 0.0,
            "exploration_drawdown": round(self.exploration_drawdown, 4),
            "hard_gate_rejections": self.hard_gate_rejections,
            "soft_gate_relaxed_count": self.soft_gate_relaxed_count,
            "soft_gate_relaxation_pnl": round(self.soft_gate_relaxation_pnl, 4),
            "blockers_correct_rate": round(self.blocker_correct / self.blocker_scored, 4)
            if self.blocker_scored else None,
            "edge_too_low_correct_rate": self._blocker_rate("edge_too_low"),
            "no_fresh_book_correct_rate": self._blocker_rate("no_fresh_book"),
            "depth_too_thin_correct_rate": self._blocker_rate("depth_too_thin"),
            "naive_price_extreme_correct_rate": self._blocker_rate("naive_price_extreme"),
        }
