"""Portfolio optimization + sizing (PAPER ONLY, pure, deterministic).

Capital-allocation math the trainer uses on top of the mandatory RiskEngine gate
(:mod:`engine.risk`). Nothing here places an order or relaxes a risk cap — it
only ever produces a *smaller* size and a clear reason.

Core pieces:

* :func:`fractional_kelly_size` — fractional-Kelly stake for a binary edge.
* :func:`value_at_risk` / :func:`cvar` — tail-risk (Expected Shortfall) of a
  realized-return sample.
* :func:`drawdown_throttle` — size multiplier that decays to 0 across a drawdown
  band (hard halt past the floor).
* :class:`PortfolioOptimizer` — allocates capital across candidates and
  **prefers guaranteed after-cost arbitrage over probabilistic edge**, honoring
  per-event, correlated-cluster, total-exposure, CVaR, and drawdown limits.

Quant responsibilities
----------------------
* **Quant researcher** — Kelly fraction, CVaR confidence + limit, drawdown band,
  exposure caps; validates against backtests.
* **Quant developer** — owns this pure optimizer + sizing (typed, tested).
* **Risk management / portfolio** — guaranteed-arb-first allocation, correlated
  + per-event caps, CVaR + drawdown throttles.
* **Trader / CLOB v2 execution** — receives only sizes that already passed
  realism + risk; arbitrage legs still each go through the RiskEngine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

logger = logging.getLogger("hte.portfolio")


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Sizing + tail risk
# --------------------------------------------------------------------------- #
def fractional_kelly_size(*, edge: float, price: float, bankroll: float,
                          fraction: float = 0.25, cap_frac: float = 0.10) -> float:
    """Fractional-Kelly stake (USD) for a binary contract bought at ``price``.

    The contract pays $1 if it resolves YES. ``edge`` is the modeled probability
    advantage over the market: win prob ``q = price + edge``. Net odds
    ``b = (1 - price) / price``. Full Kelly fraction of bankroll is
    ``f* = (q*b - (1-q)) / b``; we stake ``fraction * f*`` bounded to
    ``[0, cap_frac]`` of bankroll. Non-positive edge / degenerate price -> 0.
    Pure + deterministic.
    """
    p = _f(price)
    e = _f(edge)
    bank = max(0.0, _f(bankroll))
    frac = max(0.0, _f(fraction))
    cap = max(0.0, _f(cap_frac))
    if bank <= 0 or e <= 0 or not (0.0 < p < 1.0):
        return 0.0
    q = min(1.0, max(0.0, p + e))
    b = (1.0 - p) / p
    if b <= 0:
        return 0.0
    f_star = (q * b - (1.0 - q)) / b
    if f_star <= 0:
        return 0.0
    f = min(cap, frac * f_star)
    return round(max(0.0, f) * bank, 8)


def value_at_risk(returns: Sequence[float], alpha: float = 0.95) -> float:
    """Historical VaR at confidence ``alpha`` as a (typically negative) return.

    The ``(1-alpha)`` worst quantile of the sample. Empty sample -> 0.0. Pure."""
    xs = sorted(_f(r) for r in (returns or []))
    if not xs:
        return 0.0
    a = min(0.999999, max(0.0, _f(alpha, 0.95)))
    idx = int((1.0 - a) * len(xs))
    idx = min(max(idx, 0), len(xs) - 1)
    return round(xs[idx], 10)


def cvar(returns: Sequence[float], alpha: float = 0.95) -> float:
    """Conditional VaR / Expected Shortfall at ``alpha`` (mean of the worst tail).

    Returned as a return number (losses are negative). Empty sample -> 0.0.
    Pure + deterministic."""
    xs = sorted(_f(r) for r in (returns or []))
    if not xs:
        return 0.0
    a = min(0.999999, max(0.0, _f(alpha, 0.95)))
    k = max(1, int((1.0 - a) * len(xs)))
    tail = xs[:k]
    return round(sum(tail) / len(tail), 10)


def drawdown_throttle(drawdown: float, *, soft: float = 0.10, hard: float = 0.20) -> float:
    """Size multiplier in ``[0, 1]`` based on current drawdown fraction (pure).

    ``drawdown`` is a positive fraction of peak equity lost. Full size at/below
    ``soft``; linearly throttled to 0 between ``soft`` and ``hard``; hard halt
    (0) at/above ``hard``."""
    dd = max(0.0, _f(drawdown))
    s = max(0.0, _f(soft, 0.10))
    h = max(s + 1e-9, _f(hard, 0.20))
    if dd <= s:
        return 1.0
    if dd >= h:
        return 0.0
    return round(1.0 - (dd - s) / (h - s), 8)


# --------------------------------------------------------------------------- #
# Portfolio optimizer
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioCaps:
    """Portfolio-level exposure + risk limits (fractions of equity)."""

    max_total_exposure_frac: float = 0.60
    max_event_exposure_frac: float = 0.25
    max_cluster_exposure_frac: float = 0.30
    cvar_limit_frac: float = 0.15        # |CVaR| budget; tighter scaling beyond
    cvar_alpha: float = 0.95
    dd_soft: float = 0.10
    dd_hard: float = 0.20
    kelly_fraction: float = 0.25
    kelly_cap_frac: float = 0.10


@dataclass
class Candidate:
    """A sizing candidate. ``kind`` is ``"arbitrage"`` or ``"edge"``."""

    id: str
    kind: str = "edge"
    certified: bool = False           # arbitrage: certificate proven (theoretical)
    executable: bool = True           # arbitrage: EXECUTABLE_AFTER_COST_CERTIFIED
    fantasy: bool = False             # fill realism failed -> size 0
    after_cost_profit: float = 0.0    # arbitrage: certified worst-case profit
    desired_notional: float = 0.0     # arbitrage: depth-bounded set notional
    edge: float = 0.0                 # edge: probability advantage
    price: float = 0.5                # edge: buy price
    event_id: str = ""
    cluster_id: str = ""


@dataclass
class Allocation:
    id: str
    kind: str
    notional: float
    reason: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class PortfolioOptimizer:
    """Allocates capital, preferring guaranteed after-cost arbitrage (pure)."""

    def __init__(self, caps: Optional[PortfolioCaps] = None):
        self.caps = caps or PortfolioCaps()

    def allocate(self, candidates: Sequence[Candidate], *, equity: float,
                 drawdown: float = 0.0, returns: Optional[Sequence[float]] = None,
                 event_exposure: Optional[dict] = None,
                 cluster_exposure: Optional[dict] = None,
                 total_exposure: float = 0.0) -> list[Allocation]:
        """Return per-candidate sized allocations (USD).

        Order of priority: (1) certified, non-fantasy **arbitrage** (guaranteed
        after-cost profit) is funded first up to caps; (2) probabilistic **edge**
        with fractional Kelly fills remaining capacity. Fantasy fills -> 0. The
        drawdown throttle and a CVaR-budget scale apply to edge sizing; arbitrage
        is guaranteed after-cost so it is only capped by exposure limits.
        """
        eq = max(0.0, _f(equity))
        caps = self.caps
        allocs: list[Allocation] = []
        if eq <= 0:
            return [Allocation(c.id, c.kind, 0.0, "no_equity") for c in candidates]

        throttle = drawdown_throttle(drawdown, soft=caps.dd_soft, hard=caps.dd_hard)
        es = cvar(returns, caps.cvar_alpha) if returns else 0.0
        cvar_budget = caps.cvar_limit_frac
        cvar_scale = 1.0
        if es < 0 and cvar_budget > 0:
            over = abs(es) / cvar_budget
            cvar_scale = 1.0 if over <= 1.0 else round(1.0 / over, 8)

        total_cap = caps.max_total_exposure_frac * eq
        event_cap = caps.max_event_exposure_frac * eq
        cluster_cap = caps.max_cluster_exposure_frac * eq
        used_total = max(0.0, _f(total_exposure))
        used_event = dict(event_exposure or {})
        used_cluster = dict(cluster_exposure or {})

        def _room(c: Candidate) -> float:
            r_total = total_cap - used_total
            r_event = event_cap - used_event.get(c.event_id, 0.0) if c.event_id else r_total
            r_cluster = (cluster_cap - used_cluster.get(c.cluster_id, 0.0)
                         if c.cluster_id else r_total)
            return max(0.0, min(r_total, r_event, r_cluster))

        def _commit(c: Candidate, notional: float) -> None:
            nonlocal used_total
            used_total += notional
            if c.event_id:
                used_event[c.event_id] = used_event.get(c.event_id, 0.0) + notional
            if c.cluster_id:
                used_cluster[c.cluster_id] = used_cluster.get(c.cluster_id, 0.0) + notional

        arb = [c for c in candidates if c.kind == "arbitrage"]
        edge = [c for c in candidates if c.kind != "arbitrage"]
        # Guaranteed arbitrage first, best worst-case profit first.
        arb.sort(key=lambda c: _f(c.after_cost_profit), reverse=True)
        edge.sort(key=lambda c: _f(c.edge), reverse=True)

        for c in arb:
            if c.fantasy:
                allocs.append(Allocation(c.id, c.kind, 0.0, "fantasy_fill_rejected"))
                continue
            if not c.certified or _f(c.after_cost_profit) <= 0:
                allocs.append(Allocation(c.id, c.kind, 0.0, "uncertified_no_size"))
                continue
            if not c.executable:
                # certified-theoretical but not after-cost executable -> log, no size
                allocs.append(Allocation(c.id, c.kind, 0.0, "not_executable_after_cost"))
                continue
            room = _room(c)
            size = min(max(0.0, _f(c.desired_notional)), room)
            if size <= 0:
                allocs.append(Allocation(c.id, c.kind, 0.0, "exposure_cap"))
                continue
            _commit(c, size)
            allocs.append(Allocation(c.id, c.kind, round(size, 8),
                                     "certified_arbitrage_priority"))

        for c in edge:
            if c.fantasy:
                allocs.append(Allocation(c.id, c.kind, 0.0, "fantasy_fill_rejected"))
                continue
            if throttle <= 0:
                allocs.append(Allocation(c.id, c.kind, 0.0, "drawdown_halt"))
                continue
            kelly = fractional_kelly_size(edge=c.edge, price=c.price, bankroll=eq,
                                          fraction=caps.kelly_fraction,
                                          cap_frac=caps.kelly_cap_frac)
            want = kelly * throttle * cvar_scale
            room = _room(c)
            size = min(want, room)
            if size <= 0:
                reason = ("no_edge" if kelly <= 0 else "exposure_cap")
                allocs.append(Allocation(c.id, c.kind, 0.0, reason))
                continue
            _commit(c, size)
            reason = "edge_kelly"
            if cvar_scale < 1.0:
                reason += "+cvar_scaled"
            if throttle < 1.0:
                reason += "+dd_throttled"
            allocs.append(Allocation(c.id, c.kind, round(size, 8), reason))

        logger.info("portfolio allocate: equity=%.2f throttle=%.2f cvar_scale=%.2f "
                    "arb=%d edge=%d", eq, throttle, cvar_scale, len(arb), len(edge))
        return allocs
