"""Paper portfolio + position-sizing algorithms (pure Python, deterministic).

Quant scope — *Risk Management & Portfolio Optimization* + *Bregman arbitrage
capital allocation* + *Strategy Optimization & Robustness Testing*:

Position sizing and portfolio risk controls for the PAPER training engine. This
module is **advisory + analytics only** — it computes sizes and portfolio risk,
but it can NEVER bypass the mandatory gates: every simulated order/bundle still
flows through ``TrainingRiskGate`` + ``RiskEngine`` + the paper broker, and the
hard paper caps in :class:`engine.training.config.TrainingConfig` always clamp
the result. Nothing here places, signs, or submits an order.

Provided:

* **Fractional Kelly** sizing for a calibrated directional probability edge,
  clamped to a hard fraction + a hard USD ceiling.
* **Bregman bundle sizing** from a certified opportunity's worst-case PnL,
  all-leg depth, fill feasibility, capital lock, slippage (already in the
  certified executable prices), and a leg-failure haircut.
* **CVaR / expected shortfall**, drawdown budget, event-level exposure netting,
  correlated-group caps, liquidity-adjusted sizing, and Chainlink-freshness /
  settlement-ambiguity risk penalties.
* A :class:`PortfolioRiskManager` that enforces additive portfolio caps and
  produces the portfolio report (gross / net / event / strategy / Bregman /
  Chainlink-linked exposure, expected shortfall, worst-case leg failure,
  concentration, aggressive exploration budget used, feedback per unit risk).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("hte.training.portfolio")

_EPS = 1e-12


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


# --------------------------------------------------------------------------- #
# directional fractional-Kelly sizing
# --------------------------------------------------------------------------- #
def fractional_kelly(p: float, price: float, *, kelly_fraction: float = 0.25,
                     max_fraction: float = 0.05) -> float:
    """Fractional-Kelly fraction of bankroll for buying a $1 binary outcome.

    For a YES contract bought at ``price`` ``c`` that pays $1 with probability
    ``p``: net odds ``b = (1 - c) / c`` and full-Kelly ``f* = p - (1 - p) / b``.
    The result is scaled by ``kelly_fraction`` and hard-clamped to
    ``[0, max_fraction]`` — it is never negative (no edge -> no size) and never
    exceeds the hard fraction cap.
    """
    c = float(price)
    if c <= 0.0 or c >= 1.0:
        return 0.0
    p = _clamp(p, 0.0, 1.0)
    b = (1.0 - c) / c
    f_star = p - (1.0 - p) / b
    if f_star <= 0.0:
        return 0.0
    return _clamp(f_star * float(kelly_fraction), 0.0, float(max_fraction))


def kelly_size_usd(p: float, price: float, *, bankroll: float, kelly_fraction: float,
                   max_fraction: float, max_size_usd: float) -> float:
    """Fractional-Kelly notional in USD, clamped by the hard per-order ceiling."""
    frac = fractional_kelly(p, price, kelly_fraction=kelly_fraction,
                            max_fraction=max_fraction)
    raw = frac * max(0.0, float(bankroll))
    return round(max(0.0, min(raw, float(max_size_usd))), 6)


def liquidity_adjusted_size(size_usd: float, depth_usd: float, *,
                            max_depth_fraction: float = 0.35) -> float:
    """Cap a notional to a fraction of top-of-book depth (Execution CLOB v2 sim)."""
    cap = max(0.0, float(depth_usd)) * float(max_depth_fraction)
    return round(max(0.0, min(float(size_usd), cap)), 6)


def chainlink_freshness_penalty(size_usd: float, *, chainlink_confidence: float = 0.0,
                                chainlink_no_trade: bool = False,
                                weight: float = 0.5) -> float:
    """Shrink size when a linked Chainlink oracle is stale/low-confidence.

    A blocked oracle (``chainlink_no_trade``) zeroes the size; otherwise the size
    is scaled by ``1 - weight * (1 - confidence)`` (fresh, confident oracle ->
    no penalty). Never increases the size.
    """
    if chainlink_no_trade:
        return 0.0
    conf = _clamp(chainlink_confidence, 0.0, 1.0)
    factor = _clamp(1.0 - float(weight) * (1.0 - conf), 0.0, 1.0)
    return round(max(0.0, float(size_usd)) * factor, 6)


def settlement_ambiguity_penalty(size_usd: float, *, ambiguity: float = 0.0,
                                 max_ambiguity: float = 0.35, weight: float = 1.0) -> float:
    """Shrink size as settlement ambiguity rises (zero at/above ``max_ambiguity``)."""
    amb = max(0.0, float(ambiguity))
    if max_ambiguity <= 0:
        return round(max(0.0, float(size_usd)), 6)
    factor = _clamp(1.0 - float(weight) * (amb / max_ambiguity), 0.0, 1.0)
    return round(max(0.0, float(size_usd)) * factor, 6)


# --------------------------------------------------------------------------- #
# Bregman bundle capital allocation
# --------------------------------------------------------------------------- #
def bregman_bundle_size(opp, *, bankroll: float, max_bundle_usd: float,
                        max_depth_fraction: float = 0.35,
                        leg_failure_haircut: float = 0.5) -> dict:
    """Size a certified Bregman bundle for PAPER capital allocation.

    Scales the certified number of ``sets`` down to fit ``max_bundle_usd`` of
    locked capital and the bankroll, honouring the certified ``fill_feasibility``
    and per-leg depth (``max_depth_fraction``). Returns a sizing dict including
    the **leg-failure haircut** — the capital at risk if a single leg fails to
    fill, leaving a temporarily un-hedged position. An un-certified / non-positive
    opportunity yields zero size.
    """
    certified = bool(getattr(opp, "certified", False)) and getattr(opp, "is_opportunity", False)
    cost_per_set = float(getattr(opp, "cost_per_set", 0.0))
    sets_certified = float(getattr(opp, "sets", 0.0))
    legs = list(getattr(opp, "legs", []) or [])
    profit_lb = float(getattr(opp, "profit_lower_bound", 0.0))
    if not certified or cost_per_set <= 0.0 or sets_certified <= 0.0 or profit_lb <= 0.0:
        return {"tradable": False, "sets": 0.0, "per_leg_notional": [],
                "required_capital": 0.0, "capital_locked": 0.0, "expected_profit": 0.0,
                "worst_case_pnl": 0.0, "worst_case_leg_failure": 0.0,
                "fill_feasibility": float(getattr(opp, "fill_feasibility", 0.0)),
                "reason": "not_certified"}

    profit_per_set = profit_lb / sets_certified
    # cap sets by the bundle capital budget and bankroll
    cap_by_budget = float(max_bundle_usd) / cost_per_set if cost_per_set > 0 else 0.0
    cap_by_bankroll = max(0.0, float(bankroll)) / cost_per_set if cost_per_set > 0 else 0.0
    # per-leg depth ceiling (defensive; certification already applied depth)
    depth_sets = []
    for leg in legs:
        px = float(getattr(leg, "executable_price", 0.0))
        depth = float(getattr(leg, "depth_usd", 0.0))
        if px > 0:
            depth_sets.append(depth * float(max_depth_fraction) / px)
    cap_by_depth = min(depth_sets) if depth_sets else sets_certified
    sets = max(0.0, min(sets_certified, cap_by_budget, cap_by_bankroll, cap_by_depth))
    if sets <= 0.0:
        return {"tradable": False, "sets": 0.0, "per_leg_notional": [],
                "required_capital": 0.0, "capital_locked": 0.0, "expected_profit": 0.0,
                "worst_case_pnl": 0.0, "worst_case_leg_failure": 0.0,
                "fill_feasibility": float(getattr(opp, "fill_feasibility", 0.0)),
                "reason": "zero_after_caps"}

    required_capital = sets * cost_per_set
    expected_profit = sets * profit_per_set
    # worst-case single-leg failure: the most expensive leg fails to fill, so its
    # capital is exposed un-hedged; haircut models the recovery loss on unwind.
    per_leg = [sets * float(getattr(leg, "executable_price", 0.0)) for leg in legs]
    worst_leg = max(per_leg) if per_leg else 0.0
    worst_case_leg_failure = round(float(leg_failure_haircut) * worst_leg, 6)
    result = {
        "tradable": True, "sets": round(sets, 6),
        "per_leg_notional": [round(x, 6) for x in per_leg],
        "required_capital": round(required_capital, 6),
        "capital_locked": round(required_capital, 6),
        "expected_profit": round(expected_profit, 6),
        "worst_case_pnl": round(expected_profit, 6),     # fully hedged -> deterministic
        "worst_case_leg_failure": worst_case_leg_failure,
        "fill_feasibility": float(getattr(opp, "fill_feasibility", 0.0)),
        "reason": "ok",
    }
    logger.info("bregman_bundle_size group=%s sets=%.2f capital=%.2f profit=%.4f "
                "worst_leg_failure=%.4f", getattr(opp, "group_id", "?"), sets,
                required_capital, expected_profit, worst_case_leg_failure)
    return result


# --------------------------------------------------------------------------- #
# CVaR / drawdown
# --------------------------------------------------------------------------- #
def cvar(returns: list[float], *, alpha: float = 0.95) -> float:
    """Conditional Value-at-Risk (expected shortfall) of a return series.

    Returns the (positive) mean loss in the worst ``1 - alpha`` tail. ``0.0`` when
    there are no losses in the tail or no data. Deterministic.
    """
    if not returns:
        return 0.0
    losses = sorted(float(r) for r in returns)        # most negative first
    n = len(losses)
    k = max(1, int(math.ceil((1.0 - float(alpha)) * n)))
    tail = losses[:k]
    mean_tail = sum(tail) / len(tail)
    return round(max(0.0, -mean_tail), 6)


def max_drawdown(equity_curve: list[float]) -> float:
    """Largest peak-to-trough drop (positive magnitude) of an equity curve."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return round(dd, 6)


# --------------------------------------------------------------------------- #
# portfolio state + limits + manager
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioPosition:
    strategy: str = "directional"
    category: str = "uncategorized"
    event_group: str = ""
    notional: float = 0.0
    side: str = "BUY"
    bregman: bool = False
    chainlink_linked: bool = False


@dataclass
class PortfolioState:
    positions: list[PortfolioPosition] = field(default_factory=list)

    def add(self, pos: PortfolioPosition) -> None:
        self.positions.append(pos)

    def _signed(self, pos: PortfolioPosition) -> float:
        return pos.notional if pos.side.upper() == "BUY" else -pos.notional

    def gross(self) -> float:
        return round(sum(abs(p.notional) for p in self.positions), 6)

    def net(self) -> float:
        return round(sum(self._signed(p) for p in self.positions), 6)

    def by_event(self) -> dict:
        out: dict = {}
        for p in self.positions:
            out[p.event_group] = round(out.get(p.event_group, 0.0) + abs(p.notional), 6)
        return out

    def net_by_event(self) -> dict:
        """Event-level exposure NETTING (opposing sides offset within an event)."""
        out: dict = {}
        for p in self.positions:
            out[p.event_group] = round(out.get(p.event_group, 0.0) + self._signed(p), 6)
        return out

    def by_category(self) -> dict:
        out: dict = {}
        for p in self.positions:
            out[p.category] = round(out.get(p.category, 0.0) + abs(p.notional), 6)
        return out

    def by_strategy(self) -> dict:
        out: dict = {}
        for p in self.positions:
            out[p.strategy] = round(out.get(p.strategy, 0.0) + abs(p.notional), 6)
        return out

    def bregman_exposure(self) -> float:
        return round(sum(abs(p.notional) for p in self.positions if p.bregman), 6)

    def chainlink_linked_exposure(self) -> float:
        return round(sum(abs(p.notional) for p in self.positions if p.chainlink_linked), 6)

    def event_exposure(self, event_group: str) -> float:
        return self.by_event().get(event_group, 0.0)

    def category_exposure(self, category: str) -> float:
        return self.by_category().get(category, 0.0)

    def diversity(self) -> int:
        """Number of distinct (event, category) buckets currently held."""
        return len({(p.event_group, p.category) for p in self.positions})

    def concentration(self) -> float:
        """Herfindahl–Hirschman concentration over event exposures (0..1)."""
        ev = self.by_event()
        total = sum(ev.values())
        if total <= 0:
            return 0.0
        return round(sum((v / total) ** 2 for v in ev.values()), 6)


@dataclass
class PortfolioLimits:
    """Hard, additive portfolio caps (USD). Only ever tighten the mandatory gates."""

    max_total_exposure_usd: float = 100.0
    max_event_exposure_usd: float = 20.0
    max_category_exposure_usd: float = 40.0
    max_bregman_bundle_exposure_usd: float = 30.0
    max_daily_loss_usd: float = 50.0
    max_drawdown_usd: float = 50.0
    exploration_budget_usd: float = 20.0
    diversity_target: int = 5
    cvar_alpha: float = 0.95

    @classmethod
    def from_config(cls, cfg) -> "PortfolioLimits":
        return cls(
            max_total_exposure_usd=float(getattr(cfg, "max_total_exposure_usd", 100.0)),
            max_event_exposure_usd=float(getattr(cfg, "max_event_exposure_usd",
                                                  getattr(cfg, "max_market_exposure_usd", 20.0))),
            max_category_exposure_usd=float(getattr(cfg, "max_category_exposure_usd", 40.0)),
            max_bregman_bundle_exposure_usd=float(
                getattr(cfg, "max_bregman_bundle_exposure_usd", 30.0)),
            max_daily_loss_usd=float(getattr(cfg, "max_daily_loss_usd", 50.0)),
            max_drawdown_usd=float(getattr(cfg, "max_drawdown_usd", 50.0)),
            exploration_budget_usd=float(getattr(cfg, "exploration_budget_usd", 20.0)),
            diversity_target=int(getattr(cfg, "diversity_target", 5)),
            cvar_alpha=float(getattr(cfg, "cvar_alpha", 0.95)))


class PortfolioRiskManager:
    """Additive portfolio caps + reporting. NEVER relaxes the mandatory gates."""

    def __init__(self, limits: Optional[PortfolioLimits] = None):
        self.limits = limits or PortfolioLimits()

    def check(self, *, notional: float, state: PortfolioState, strategy: str = "directional",
              category: str = "uncategorized", event_group: str = "",
              bregman: bool = False, chainlink_linked: bool = False,
              day_pnl: float = 0.0, drawdown: float = 0.0,
              exploration_used: float = 0.0, exploratory: bool = False) -> tuple[bool, str]:
        """Return ``(ok, reason)``. Additive on top of TrainingRiskGate/RiskEngine."""
        lim = self.limits
        n = float(notional)
        if state.gross() + n > lim.max_total_exposure_usd + 1e-9:
            return False, "portfolio_total_exposure_cap"
        if event_group and state.event_exposure(event_group) + n > lim.max_event_exposure_usd + 1e-9:
            return False, "event_exposure_cap"
        if state.category_exposure(category) + n > lim.max_category_exposure_usd + 1e-9:
            return False, "category_exposure_cap"
        if bregman and state.bregman_exposure() + n > lim.max_bregman_bundle_exposure_usd + 1e-9:
            return False, "bregman_bundle_exposure_cap"
        if day_pnl <= -abs(lim.max_daily_loss_usd):
            return False, "daily_loss_cap"
        if drawdown >= lim.max_drawdown_usd:
            return False, "drawdown_budget"
        if exploratory and exploration_used + n > lim.exploration_budget_usd + 1e-9:
            return False, "exploration_budget_exhausted"
        return True, "ok"

    def portfolio_report(self, state: PortfolioState, *, day_pnl: float = 0.0,
                         returns: Optional[list[float]] = None,
                         equity_curve: Optional[list[float]] = None,
                         exploration_used: float = 0.0,
                         worst_case_leg_failure: float = 0.0,
                         feedback_events: int = 0) -> dict:
        """Full portfolio risk report (Risk Management & Portfolio Optimization +
        Live Trading & Monitoring)."""
        gross = state.gross()
        es = cvar(returns or [], alpha=self.limits.cvar_alpha)
        dd = max_drawdown(equity_curve or [])
        # feedback generated per unit of risk (gross exposure as the risk unit)
        fpr = round(feedback_events / gross, 6) if gross > _EPS else 0.0
        return {
            "gross_exposure": gross,
            "net_exposure": state.net(),
            "event_exposure": state.by_event(),
            "net_event_exposure": state.net_by_event(),
            "category_exposure": state.by_category(),
            "strategy_exposure": state.by_strategy(),
            "bregman_exposure": state.bregman_exposure(),
            "chainlink_linked_exposure": state.chainlink_linked_exposure(),
            "expected_shortfall": es,
            "max_drawdown": dd,
            "worst_case_leg_failure": round(float(worst_case_leg_failure), 6),
            "concentration": state.concentration(),
            "diversity": state.diversity(),
            "diversity_target": self.limits.diversity_target,
            "aggressive_exploration_budget_usd": self.limits.exploration_budget_usd,
            "aggressive_exploration_budget_used": round(float(exploration_used), 6),
            "feedback_generated_per_risk_unit": fpr,
            "day_pnl": round(float(day_pnl), 6),
        }
