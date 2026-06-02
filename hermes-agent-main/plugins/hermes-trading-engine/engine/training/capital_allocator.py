"""Adaptive capital allocation for micro-live readiness (PAPER ONLY).

This module is the capital-allocation brain that sits on top of — and only ever
*tightens* — the mandatory ``RiskEngine`` / ``TrainingRiskGate`` / portfolio
caps. It allocates paper capital only to **proven, calibrated, after-cost edge**
while defending against drawdown, correlation, settlement ambiguity, and
execution failure. It NEVER enables live trading, never sizes for real money,
and never lets research/Grok approve or grow an order.

Quant scope covered here:

* **Data Acquisition & Ingestion / Preprocessing** — consumes already-ingested,
  validated Polymarket CLOB v2 + Chainlink features via the caller; this module
  treats them as read-only risk inputs (liquidity, spread, label quality).
* **Statistical & Probabilistic Modeling** — fractional-Kelly sizing off the
  CALIBRATED probability, with hard multiplicative haircuts for every modelling
  / market risk (uncertainty, calibration error, ambiguity, adverse selection).
* **Signal Generation & Strategy Development w/ Bregman priority** — capital is
  split into ordered buckets; a *certified* Bregman bundle is the first-priority
  bucket and pre-empts directional edge — but ONLY when certification passes.
* **Risk Management & Portfolio Optimization** — drawdown governor + portfolio
  constraints (market / event / correlated-cluster / strategy exposure, daily
  loss, open capital lock) + CVaR / expected-shortfall reporting.
* **Backtesting & Simulation / Robustness** — pure, deterministic, stdlib-only
  functions so replay + walk-forward can re-derive every sizing decision.
* **CLOB v2 Execution** — slippage / adverse-selection / liquidity haircuts feed
  directly from the execution-realism layer.
* **Live Trading & Monitoring** — drawdown governor downgrades to conservative
  paper mode on a loss streak, drawdown breach, calibration degradation, or an
  execution-quality failure.
* **Compliance / Security / Operational Excellence** — negative-expectancy
  strategies can NEVER grow their allocation unless explicitly tagged tiny
  exploration AND capped; every rejection carries an auditable reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import math

from .portfolio import cvar as _cvar
from .portfolio import kelly_size_usd, max_drawdown as _max_drawdown

__all__ = [
    "BUCKET_BREGMAN", "BUCKET_STATISTICAL", "BUCKET_CHAINLINK",
    "BUCKET_DIRECTIONAL", "BUCKET_EXPLORATION", "CAPITAL_BUCKETS",
    "ACTION_TRADE", "ACTION_REDUCE", "ACTION_PAUSE", "ACTION_DOWNGRADE",
    "kelly_haircut", "kelly_haircut_components", "kelly_haircut_size_usd",
    "DrawdownGovernorLimits", "drawdown_governor",
    "PortfolioConstraints", "check_portfolio_constraints",
    "CapitalCandidate", "AllocationDecision", "AdaptiveCapitalAllocator",
    "summarize_sizing_rejections", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
]

# --------------------------------------------------------------------------- #
# capital buckets — ordered by priority (Bregman first, exploration last)
# --------------------------------------------------------------------------- #
BUCKET_BREGMAN = "certified_bregman"
BUCKET_STATISTICAL = "statistical_mispricing"
BUCKET_CHAINLINK = "chainlink_conditioned"
BUCKET_DIRECTIONAL = "directional"
BUCKET_EXPLORATION = "tiny_exploration"

CAPITAL_BUCKETS = (
    BUCKET_BREGMAN, BUCKET_STATISTICAL, BUCKET_CHAINLINK,
    BUCKET_DIRECTIONAL, BUCKET_EXPLORATION,
)
_BUCKET_PRIORITY = {b: i for i, b in enumerate(CAPITAL_BUCKETS)}

# drawdown-governor actions, ordered by severity
ACTION_TRADE = "trade"
ACTION_REDUCE = "reduce"
ACTION_PAUSE = "pause_strategy"
ACTION_DOWNGRADE = "downgrade_conservative"
_ACTION_SEVERITY = {ACTION_TRADE: 0, ACTION_REDUCE: 1, ACTION_PAUSE: 2,
                    ACTION_DOWNGRADE: 3}

# map strategy name -> capital bucket
_STRATEGY_BUCKET = {
    "bregman": BUCKET_BREGMAN, "bregman_arbitrage": BUCKET_BREGMAN,
    "statistical_mispricing": BUCKET_STATISTICAL, "statistical_edge": BUCKET_STATISTICAL,
    "statistical": BUCKET_STATISTICAL,
    "chainlink_edge": BUCKET_CHAINLINK, "chainlink_conditioned": BUCKET_CHAINLINK,
    "chainlink": BUCKET_CHAINLINK,
    "directional": BUCKET_DIRECTIONAL, "directional_edge": BUCKET_DIRECTIONAL,
    "exploration": BUCKET_EXPLORATION, "tiny_exploration": BUCKET_EXPLORATION,
}

_EPS = 1e-9


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


# --------------------------------------------------------------------------- #
# fractional-Kelly hard haircuts
# --------------------------------------------------------------------------- #
# Default per-factor weight: at the worst input each factor RETAINS ``1 - w`` of
# the size, so the product of all nine factors collapses toward zero. 0.5 keeps
# any single haircut from zeroing a trade on its own while the joint worst case
# (0.5**9 ~= 0.002) is effectively a no-trade.
_DEFAULT_HAIRCUT_WEIGHTS = {
    "uncertainty": 0.5,
    "calibration_error": 0.5,
    "liquidity": 0.5,            # quality: higher is better
    "spread": 0.5,
    "slippage": 0.5,
    "label_quality": 0.5,        # quality: higher is better
    "event_correlation": 0.5,
    "settlement_ambiguity": 0.5,
    "adverse_selection": 0.5,
}
# factors expressed as a 0..1 QUALITY (higher = better) rather than a 0..1 risk
_QUALITY_FACTORS = frozenset({"liquidity", "label_quality"})


def kelly_haircut_components(*, weights: Optional[dict] = None, **factors) -> dict:
    """Per-factor retained-size multipliers (each in ``[0, 1]``).

    Risk factors (``uncertainty``, ``calibration_error``, ``spread``,
    ``slippage``, ``event_correlation``, ``settlement_ambiguity``,
    ``adverse_selection``) are 0..1 penalties (higher = worse). Quality factors
    (``liquidity``, ``label_quality``) are 0..1 scores (higher = better). A
    missing risk factor defaults to 0 (no haircut); a missing quality factor
    defaults to 1 (full quality)."""
    w = dict(_DEFAULT_HAIRCUT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items()})
    out: dict = {}
    for name, weight in _DEFAULT_HAIRCUT_WEIGHTS.items():
        weight = float(w.get(name, weight))
        if name in _QUALITY_FACTORS:
            quality = _clamp(factors.get(name, 1.0))
            penalty = 1.0 - quality
        else:
            penalty = _clamp(factors.get(name, 0.0))
        out[name] = round(_clamp(1.0 - weight * penalty), 8)
    return out


def kelly_haircut(*, weights: Optional[dict] = None, **factors) -> float:
    """Combined multiplicative fractional-Kelly haircut in ``[0, 1]``.

    The product of every per-factor retained multiplier — monotonically
    non-increasing in each risk factor, monotonically non-decreasing in each
    quality factor, ``1.0`` for benign inputs and ``~0`` when all risks are
    maxed. Never increases the base size."""
    factor = 1.0
    for v in kelly_haircut_components(weights=weights, **factors).values():
        factor *= v
    return round(_clamp(factor), 8)


def kelly_haircut_size_usd(base_kelly_usd: float, *, max_size_usd: float,
                           weights: Optional[dict] = None, **factors) -> float:
    """Apply the Kelly haircut to a base notional and hard-clamp to the paper
    order ceiling. Never exceeds ``base_kelly_usd`` nor ``max_size_usd``."""
    factor = kelly_haircut(weights=weights, **factors)
    raw = max(0.0, float(base_kelly_usd)) * factor
    return round(max(0.0, min(raw, float(max_size_usd))), 6)


# --------------------------------------------------------------------------- #
# drawdown governor
# --------------------------------------------------------------------------- #
@dataclass
class DrawdownGovernorLimits:
    """Thresholds for the drawdown governor (PAPER ONLY)."""

    max_loss_streak: int = 6              # reduce size above this streak
    pause_loss_streak: int = 10           # pause the strategy above this streak
    max_drawdown_usd: float = 50.0        # hard drawdown budget -> downgrade
    soft_drawdown_fraction: float = 0.5   # reduce once this fraction is used
    calibration_instability_limit: float = 0.15
    execution_quality_floor: float = 0.5  # min acceptable realised fill quality

    @classmethod
    def from_config(cls, cfg) -> "DrawdownGovernorLimits":
        max_streak = int(getattr(cfg, "dd_governor_max_loss_streak",
                                 max(1, int(getattr(cfg, "ks_max_loss_streak", 10)) // 2)))
        pause_streak = int(getattr(cfg, "dd_governor_pause_loss_streak",
                                   int(getattr(cfg, "ks_max_loss_streak", 10))))
        return cls(
            max_loss_streak=max_streak,
            pause_loss_streak=max(pause_streak, max_streak),
            max_drawdown_usd=float(getattr(cfg, "max_drawdown_usd", 50.0)),
            soft_drawdown_fraction=float(getattr(cfg, "dd_governor_soft_fraction", 0.5)),
            calibration_instability_limit=float(
                getattr(cfg, "dd_governor_calibration_limit", 0.15)),
            execution_quality_floor=float(
                getattr(cfg, "dd_governor_execution_floor", 0.5)))


def drawdown_governor(*, loss_streak: int = 0, drawdown: float = 0.0,
                      max_drawdown_usd: float = 50.0,
                      calibration_instability: float = 0.0,
                      execution_quality: float = 1.0,
                      limits: Optional[DrawdownGovernorLimits] = None) -> dict:
    """Decide how aggressively capital may flow given degraded conditions.

    Returns ``{"action", "size_multiplier", "reasons"}`` where ``action`` is one
    of ``trade`` / ``reduce`` / ``pause_strategy`` / ``downgrade_conservative``
    and ``size_multiplier`` is monotonically non-increasing in every stress
    dimension (drawdown, loss streak, calibration instability) and decreasing as
    execution quality falls below the floor. A hard drawdown breach downgrades
    to conservative paper mode; a long loss streak or severe calibration
    breakdown pauses the strategy. PAPER ONLY."""
    lim = limits or DrawdownGovernorLimits(max_drawdown_usd=float(max_drawdown_usd))
    max_dd = float(max_drawdown_usd if max_drawdown_usd else lim.max_drawdown_usd) or 1.0
    streak = max(0, int(loss_streak))
    dd = max(0.0, float(drawdown))
    calib = max(0.0, float(calibration_instability))
    exec_q = _clamp(execution_quality)

    # graduated, monotone multiplier (1.0 when fully benign)
    dd_frac = _clamp(dd / max_dd)
    streak_frac = _clamp(streak / max(1, lim.pause_loss_streak))
    calib_frac = _clamp(calib / max(_EPS, lim.calibration_instability_limit))
    exec_pen = _clamp((lim.execution_quality_floor - exec_q)
                      / max(_EPS, lim.execution_quality_floor)) if exec_q < lim.execution_quality_floor else 0.0
    mult = ((1.0 - dd_frac) * (1.0 - 0.6 * streak_frac)
            * (1.0 - 0.5 * calib_frac) * (1.0 - 0.7 * exec_pen))
    mult = round(_clamp(mult), 6)

    reasons: list = []
    action = ACTION_TRADE

    def _escalate(act: str) -> None:
        nonlocal action
        if _ACTION_SEVERITY[act] > _ACTION_SEVERITY[action]:
            action = act

    if dd >= max_dd - _EPS:
        reasons.append("drawdown_breach")
        _escalate(ACTION_DOWNGRADE)
    elif dd >= lim.soft_drawdown_fraction * max_dd:
        reasons.append("drawdown_soft_limit")
        _escalate(ACTION_REDUCE)

    if streak >= lim.pause_loss_streak:
        reasons.append("loss_streak_pause")
        _escalate(ACTION_PAUSE)
    elif streak >= lim.max_loss_streak:
        reasons.append("loss_streak_reduce")
        _escalate(ACTION_REDUCE)

    if calib >= 2.0 * lim.calibration_instability_limit:
        reasons.append("calibration_breakdown")
        _escalate(ACTION_PAUSE)
    elif calib >= lim.calibration_instability_limit:
        reasons.append("calibration_degradation")
        _escalate(ACTION_REDUCE)

    if exec_q < lim.execution_quality_floor:
        reasons.append("execution_quality_failure")
        _escalate(ACTION_REDUCE)

    if action in (ACTION_PAUSE, ACTION_DOWNGRADE):
        mult = 0.0
    elif action == ACTION_TRADE:
        mult = 1.0
    else:  # reduce — keep the graduated multiplier strictly below 1
        mult = round(min(mult, 0.99), 6)
    return {"action": action, "size_multiplier": mult, "reasons": reasons}


# --------------------------------------------------------------------------- #
# portfolio constraints
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioConstraints:
    """Hard portfolio exposure caps (USD). Only ever tighten the mandatory gate."""

    max_market_exposure_usd: float = 20.0
    max_event_exposure_usd: float = 20.0
    max_cluster_exposure_usd: float = 40.0          # correlated-cluster exposure
    max_strategy_exposure_usd: float = 40.0
    max_daily_loss_usd: float = 50.0
    max_open_capital_lock_usd: float = 100.0        # total deployed capital lock

    @classmethod
    def from_config(cls, cfg) -> "PortfolioConstraints":
        return cls(
            max_market_exposure_usd=float(getattr(cfg, "max_market_exposure_usd", 20.0)),
            max_event_exposure_usd=float(getattr(cfg, "max_event_exposure_usd",
                                                  getattr(cfg, "max_market_exposure_usd", 20.0))),
            max_cluster_exposure_usd=float(getattr(cfg, "max_correlated_cluster_exposure_usd",
                                                   getattr(cfg, "max_category_exposure_usd", 40.0))),
            max_strategy_exposure_usd=float(getattr(cfg, "max_strategy_exposure_usd",
                                                    getattr(cfg, "max_category_exposure_usd", 40.0))),
            max_daily_loss_usd=float(getattr(cfg, "max_daily_loss_usd", 50.0)),
            max_open_capital_lock_usd=float(getattr(cfg, "max_open_capital_lock_usd",
                                                    getattr(cfg, "max_total_exposure_usd", 100.0))))


def check_portfolio_constraints(*, notional: float, constraints: PortfolioConstraints,
                                market_exposure: float = 0.0, event_exposure: float = 0.0,
                                cluster_exposure: float = 0.0, strategy_exposure: float = 0.0,
                                open_capital_lock: float = 0.0,
                                day_pnl: float = 0.0) -> tuple:
    """Return ``(ok, reason)``. Additive on top of the mandatory risk gate."""
    c = constraints
    n = max(0.0, float(notional))
    if day_pnl <= -abs(c.max_daily_loss_usd):
        return False, "daily_loss_cap"
    if market_exposure + n > c.max_market_exposure_usd + _EPS:
        return False, "market_exposure_cap"
    if event_exposure + n > c.max_event_exposure_usd + _EPS:
        return False, "event_exposure_cap"
    if cluster_exposure + n > c.max_cluster_exposure_usd + _EPS:
        return False, "correlated_cluster_exposure_cap"
    if strategy_exposure + n > c.max_strategy_exposure_usd + _EPS:
        return False, "strategy_exposure_cap"
    if open_capital_lock + n > c.max_open_capital_lock_usd + _EPS:
        return False, "open_capital_lock_cap"
    return True, "ok"


# --------------------------------------------------------------------------- #
# candidate + decision
# --------------------------------------------------------------------------- #
@dataclass
class CapitalCandidate:
    """A single allocation candidate (PAPER ONLY).

    ``net_after_cost_edge`` is the realistic AFTER-COST edge (CLOB v2 fills,
    fees, slippage, ambiguity, timing) — the only edge capital is allowed to
    chase. ``p_final`` is the CALIBRATED probability used for Kelly sizing."""

    strategy: str = "directional"
    market_id: str = ""
    event_group: str = ""
    cluster: str = ""
    price: float = 0.5
    p_final: float = 0.5
    gross_edge: float = 0.0
    net_after_cost_edge: float = 0.0
    bregman: bool = False
    bregman_certified: bool = False
    exploration: bool = False
    feedback_value: float = 0.0
    # risk-haircut inputs (0..1; quality factors higher = better)
    uncertainty: float = 0.0
    calibration_error: float = 0.0
    liquidity: float = 1.0
    spread: float = 0.0
    slippage: float = 0.0
    label_quality: float = 1.0
    event_correlation: float = 0.0
    settlement_ambiguity: float = 0.0
    adverse_selection: float = 0.0

    def haircut_factors(self) -> dict:
        return dict(uncertainty=self.uncertainty, calibration_error=self.calibration_error,
                    liquidity=self.liquidity, spread=self.spread, slippage=self.slippage,
                    label_quality=self.label_quality, event_correlation=self.event_correlation,
                    settlement_ambiguity=self.settlement_ambiguity,
                    adverse_selection=self.adverse_selection)


@dataclass
class AllocationDecision:
    approved: bool
    bucket: str
    notional_usd: float
    strategy: str = ""
    market_id: str = ""
    base_kelly_usd: float = 0.0
    haircut: float = 1.0
    governor_multiplier: float = 1.0
    haircut_components: dict = field(default_factory=dict)
    reason: str = "ok"
    exploration: bool = False
    net_after_cost_edge: float = 0.0
    expected_profit: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _bucket_for(candidate: CapitalCandidate) -> str:
    if bool(getattr(candidate, "exploration", False)):
        return BUCKET_EXPLORATION
    if bool(getattr(candidate, "bregman", False)):
        return BUCKET_BREGMAN
    return _STRATEGY_BUCKET.get(str(getattr(candidate, "strategy", "")).lower(),
                                BUCKET_DIRECTIONAL)


# --------------------------------------------------------------------------- #
# the allocator
# --------------------------------------------------------------------------- #
class AdaptiveCapitalAllocator:
    """Allocate paper capital only to proven, calibrated, after-cost edge.

    Bregman certified opportunities are the first-priority bucket and only ever
    pre-empt directional edge when certification passes. Negative-expectancy
    candidates can NEVER grow their allocation unless explicitly tagged tiny
    exploration AND capped at ``exploration_notional_usd``. PAPER ONLY."""

    def __init__(self, cfg=None, *, bucket_caps: Optional[dict] = None,
                 constraints: Optional[PortfolioConstraints] = None,
                 dd_limits: Optional[DrawdownGovernorLimits] = None,
                 max_size_usd: Optional[float] = None,
                 exploration_notional_usd: Optional[float] = None):
        self.cfg = cfg
        self.bankroll = float(getattr(cfg, "starting_bankroll", 500.0)) if cfg else 500.0
        self.kelly_fraction = float(getattr(cfg, "kelly_fraction", 0.10)) if cfg else 0.10
        self.kelly_max_fraction = float(getattr(cfg, "kelly_max_fraction", 0.05)) if cfg else 0.05
        if max_size_usd is not None:
            self.max_size_usd = float(max_size_usd)
        elif cfg is not None:
            self.max_size_usd = float(getattr(cfg, "max_order_notional_usd",
                                              getattr(cfg, "max_kelly_size_usd", 5.0)))
        else:
            self.max_size_usd = 5.0
        if exploration_notional_usd is not None:
            self.exploration_notional_usd = float(exploration_notional_usd)
        else:
            self.exploration_notional_usd = float(
                getattr(cfg, "exploration_notional_usd", 2.0)) if cfg else 2.0
        self.constraints = constraints or (PortfolioConstraints.from_config(cfg)
                                           if cfg else PortfolioConstraints())
        self.dd_limits = dd_limits or (DrawdownGovernorLimits.from_config(cfg)
                                       if cfg else DrawdownGovernorLimits())
        self.bucket_caps = self._default_bucket_caps()
        if bucket_caps:
            self.bucket_caps.update({k: float(v) for k, v in bucket_caps.items()})
        self.min_after_cost_edge = float(getattr(cfg, "capital_min_after_cost_edge", 0.0)) if cfg else 0.0

    # -- bucket caps --------------------------------------------------------
    def _default_bucket_caps(self) -> dict:
        cfg = self.cfg
        if cfg is not None:
            total = float(getattr(cfg, "max_total_exposure_usd", 100.0))
            bregman = float(getattr(cfg, "max_bregman_bundle_exposure_usd", 30.0))
            explore = float(getattr(cfg, "exploration_budget_usd", 20.0))
        else:
            total, bregman, explore = 100.0, 30.0, 20.0
        return {
            BUCKET_BREGMAN: bregman,
            BUCKET_STATISTICAL: total,
            BUCKET_CHAINLINK: total,
            BUCKET_DIRECTIONAL: total,
            BUCKET_EXPLORATION: explore,
        }

    # -- base (pre-haircut) Kelly size --------------------------------------
    def _base_kelly(self, candidate: CapitalCandidate, bucket: str) -> float:
        if bucket == BUCKET_EXPLORATION:
            return round(min(self.exploration_notional_usd, self.max_size_usd), 6)
        price = float(getattr(candidate, "price", 0.5)) or 0.5
        p = float(getattr(candidate, "p_final", 0.5))
        return kelly_size_usd(p, price, bankroll=self.bankroll,
                              kelly_fraction=self.kelly_fraction,
                              max_fraction=self.kelly_max_fraction,
                              max_size_usd=self.max_size_usd)

    # -- single allocation --------------------------------------------------
    def allocate(self, candidate: CapitalCandidate, *,
                 market_exposure: float = 0.0, event_exposure: float = 0.0,
                 cluster_exposure: float = 0.0, strategy_exposure: float = 0.0,
                 open_capital_lock: float = 0.0, day_pnl: float = 0.0,
                 bucket_exposure: Optional[dict] = None,
                 loss_streak: int = 0, drawdown: float = 0.0,
                 max_drawdown_usd: Optional[float] = None,
                 calibration_instability: float = 0.0,
                 execution_quality: float = 1.0) -> AllocationDecision:
        bucket = _bucket_for(candidate)
        exploration = bool(getattr(candidate, "exploration", False))
        net = float(getattr(candidate, "net_after_cost_edge", 0.0))
        certified = bool(getattr(candidate, "bregman_certified", False))
        is_bregman = bool(getattr(candidate, "bregman", False))

        def _reject(reason: str) -> AllocationDecision:
            return AllocationDecision(
                approved=False, bucket=bucket, notional_usd=0.0,
                strategy=str(getattr(candidate, "strategy", "")),
                market_id=str(getattr(candidate, "market_id", "")),
                reason=reason, exploration=exploration, net_after_cost_edge=net)

        # 1) Bregman priority gate: a Bregman bundle is funded ONLY when certified.
        if is_bregman and not certified:
            return _reject("bregman_not_certified")

        # 2) Expectancy gate: only proven positive after-cost edge gets capital,
        #    UNLESS the candidate is an explicitly-tagged tiny exploration probe.
        positive = (net > self.min_after_cost_edge) or (is_bregman and certified)
        if not positive and not exploration:
            return _reject("negative_expectancy")

        # 3) Drawdown governor — degraded conditions cut size / pause / downgrade.
        gov = drawdown_governor(
            loss_streak=loss_streak, drawdown=drawdown,
            max_drawdown_usd=float(max_drawdown_usd if max_drawdown_usd is not None
                                   else self.dd_limits.max_drawdown_usd),
            calibration_instability=calibration_instability,
            execution_quality=execution_quality, limits=self.dd_limits)
        if gov["action"] == ACTION_DOWNGRADE:
            return _reject("drawdown_governor_downgrade")
        if gov["action"] == ACTION_PAUSE:
            return _reject("drawdown_governor_pause")
        gov_mult = float(gov["size_multiplier"])

        # 4) Fractional-Kelly base size + hard risk haircuts.
        base = self._base_kelly(candidate, bucket)
        comps = kelly_haircut_components(**candidate.haircut_factors())
        haircut = 1.0
        for v in comps.values():
            haircut *= v
        haircut = round(_clamp(haircut), 8)
        size = base * haircut * gov_mult

        # 5) Bucket cap + tiny-exploration hard cap.
        bx = dict(bucket_exposure or {})
        bucket_room = max(0.0, float(self.bucket_caps.get(bucket, self.max_size_usd))
                          - float(bx.get(bucket, 0.0)))
        size = min(size, bucket_room, self.max_size_usd)
        if bucket == BUCKET_EXPLORATION:
            size = min(size, self.exploration_notional_usd)
        size = round(max(0.0, size), 6)
        if size <= 0.0:
            return _reject("zero_after_bucket_cap")

        # 6) Portfolio exposure constraints (market / event / cluster / strategy /
        #    daily loss / open capital lock).
        ok, preason = check_portfolio_constraints(
            notional=size, constraints=self.constraints,
            market_exposure=market_exposure, event_exposure=event_exposure,
            cluster_exposure=cluster_exposure, strategy_exposure=strategy_exposure,
            open_capital_lock=open_capital_lock, day_pnl=day_pnl)
        if not ok:
            return _reject("portfolio_constraint:" + preason)

        return AllocationDecision(
            approved=True, bucket=bucket, notional_usd=size,
            strategy=str(getattr(candidate, "strategy", "")),
            market_id=str(getattr(candidate, "market_id", "")),
            base_kelly_usd=round(base, 6), haircut=haircut,
            governor_multiplier=round(gov_mult, 6), haircut_components=comps,
            reason="ok", exploration=exploration, net_after_cost_edge=net,
            expected_profit=round(size * max(0.0, net), 6))

    # -- batch allocation (priority-ordered) --------------------------------
    def allocate_batch(self, candidates, **kwargs) -> list:
        """Allocate a list of candidates in bucket-priority order (certified
        Bregman first). Tracks per-bucket exposure so earlier (higher-priority)
        funded candidates consume the bucket budget before lower-priority ones.
        Returns decisions in priority order."""
        ordered = sorted(
            candidates,
            key=lambda c: (_BUCKET_PRIORITY.get(_bucket_for(c), len(CAPITAL_BUCKETS)),
                           -float(getattr(c, "net_after_cost_edge", 0.0))))
        bucket_exposure: dict = {}
        decisions: list = []
        for cand in ordered:
            dec = self.allocate(cand, bucket_exposure=bucket_exposure, **kwargs)
            if dec.approved:
                bucket_exposure[dec.bucket] = round(
                    bucket_exposure.get(dec.bucket, 0.0) + dec.notional_usd, 6)
            decisions.append(dec)
        return decisions

    # -- capital allocation report ------------------------------------------
    def capital_allocation_report(self, decisions, *, returns=None,
                                  equity_curve=None, feedback_events: int = 0) -> dict:
        """Capital allocation + risk report (Risk Management & Portfolio
        Optimization + Live Trading & Monitoring).

        Reports expected return, expected shortfall / CVaR, exposure
        concentration, capital efficiency, feedback generated per unit of risk,
        the per-bucket allocation split, and the rejected-sizing-reason tally."""
        approved = [d for d in decisions if getattr(d, "approved", False)]
        bucket_alloc: dict = {}
        for d in approved:
            bucket_alloc[d.bucket] = round(bucket_alloc.get(d.bucket, 0.0)
                                           + float(d.notional_usd), 6)
        total = round(sum(bucket_alloc.values()), 6)
        expected_return = round(sum(float(getattr(d, "expected_profit", 0.0))
                                    for d in approved), 6)
        es = _cvar(list(returns or []),
                   alpha=float(getattr(self.cfg, "cvar_alpha", 0.95)) if self.cfg else 0.95)
        dd = _max_drawdown(list(equity_curve or []))
        if total > _EPS:
            concentration = round(sum((v / total) ** 2 for v in bucket_alloc.values()), 6)
            capital_efficiency = round(expected_return / total, 6)
            feedback_per_risk_unit = round(float(feedback_events) / total, 6)
        else:
            concentration = capital_efficiency = feedback_per_risk_unit = 0.0
        return {
            "buckets": list(CAPITAL_BUCKETS),
            "bucket_allocations": bucket_alloc,
            "bucket_caps": dict(self.bucket_caps),
            "total_allocated": total,
            "approved_count": len(approved),
            "rejected_count": len(decisions) - len(approved),
            "expected_return": expected_return,
            "expected_shortfall": es,
            "cvar": es,
            "max_drawdown": dd,
            "sharpe": sharpe_ratio(returns),
            "sortino": sortino_ratio(returns),
            "calmar": calmar_ratio(returns, equity_curve),
            "concentration": concentration,
            "capital_efficiency": capital_efficiency,
            "feedback_per_risk_unit": feedback_per_risk_unit,
            "rejected_sizing_reasons": summarize_sizing_rejections(decisions),
        }


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def sharpe_ratio(returns) -> float:
    """Sharpe ratio of a return series (mean / stdev). 0 with <2 points or no
    dispersion. Deterministic, stdlib-only."""
    xs = [float(r) for r in (returns or [])]
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    var = _mean([(x - mu) ** 2 for x in xs])
    sd = math.sqrt(var)
    return round(mu / sd, 6) if sd > _EPS else 0.0


def sortino_ratio(returns) -> float:
    """Sortino ratio (mean / downside deviation). Penalises only negative returns."""
    xs = [float(r) for r in (returns or [])]
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    downs = [min(0.0, x) ** 2 for x in xs]
    dd = math.sqrt(_mean(downs))
    return round(mu / dd, 6) if dd > _EPS else 0.0


def calmar_ratio(returns, equity_curve) -> float:
    """Calmar ratio (total return / max drawdown). 0 when there is no drawdown."""
    mdd = _max_drawdown(list(equity_curve or []))
    if mdd <= _EPS:
        return 0.0
    total_return = sum(float(r) for r in (returns or []))
    return round(total_return / mdd, 6)


def summarize_sizing_rejections(decisions) -> dict:
    """Tally rejected-sizing reasons across a set of allocation decisions
    (Compliance / auditability)."""
    out: dict = {}
    for d in decisions:
        if getattr(d, "approved", False):
            continue
        reason = str(getattr(d, "reason", "rejected"))
        out[reason] = out.get(reason, 0) + 1
    return out
