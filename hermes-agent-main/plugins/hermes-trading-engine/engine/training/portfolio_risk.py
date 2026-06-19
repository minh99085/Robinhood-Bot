"""Tier-2 institutional portfolio-risk engine (PAPER ONLY, pure/deterministic).

Adds a PORTFOLIO-level risk layer on top of the per-trade correlation + risk gates:

* VaR / CVaR (historical) of realized paper returns,
* concentration: exposure by event / category / cluster, the Herfindahl-Hirschman index
  (HHI), and the largest single-event / single-category exposure as a fraction of equity,
* a candidate gate/cap that BLOCKS or SIZE-CAPS a new paper trade when it would breach an
  event / category concentration limit (correlation-aware exposure netting).

TIGHTEN-ONLY + read-only: it can only block or shrink a paper trade, never enlarge one, and
never enables live trading. Pure (no I/O) so it is safe on the hot path; the heavy report is
built off-tick.
"""

from __future__ import annotations

from dataclasses import dataclass


def _f(x, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def historical_var(returns: list, *, alpha: float = 0.95) -> float:
    """Historical VaR (<=0): the ``1-alpha`` worst-quantile realized return."""
    xs = sorted(_f(r) for r in (returns or []))
    if not xs:
        return 0.0
    idx = max(0, min(len(xs) - 1, int((1.0 - alpha) * len(xs))))
    return round(min(0.0, xs[idx]), 6)


def historical_cvar(returns: list, *, alpha: float = 0.95) -> float:
    """Historical CVaR / Expected Shortfall (<=0): mean of the worst ``1-alpha`` tail."""
    xs = sorted(_f(r) for r in (returns or []))
    if not xs:
        return 0.0
    k = max(1, int((1.0 - alpha) * len(xs)))
    tail = xs[:k]
    return round(min(0.0, sum(tail) / len(tail)), 6)


def _exposures(positions: list) -> "tuple[dict, dict, dict, float]":
    """Open-exposure (USD cost) by event / category / cluster + total."""
    by_event: dict = {}
    by_cat: dict = {}
    by_cluster: dict = {}
    total = 0.0
    for p in (positions or []):
        cost = _f(getattr(p, "cost", 0.0))
        if cost <= 0.0:
            continue
        total += cost
        ev = str(getattr(p, "group_key", "") or "")
        cat = str(getattr(p, "category", "") or "uncategorized")
        cl = str(getattr(p, "cluster_id", "") or ev or "")
        by_event[ev] = by_event.get(ev, 0.0) + cost
        by_cat[cat] = by_cat.get(cat, 0.0) + cost
        if cl:
            by_cluster[cl] = by_cluster.get(cl, 0.0) + cost
    return by_event, by_cat, by_cluster, total


def _hhi(buckets: dict, total: float) -> float:
    """Herfindahl-Hirschman concentration index in [0,1] (1 = a single bucket)."""
    if total <= 0.0:
        return 0.0
    return round(sum((v / total) ** 2 for v in buckets.values()), 6)


def concentration_report(positions: list, *, bankroll: float) -> dict:
    by_event, by_cat, by_cluster, total = _exposures(positions)
    eq = max(1e-9, float(bankroll))
    top_event = max(by_event.items(), key=lambda kv: kv[1], default=("", 0.0))
    top_cat = max(by_cat.items(), key=lambda kv: kv[1], default=("", 0.0))
    return {
        "total_exposure_usd": round(total, 4),
        "exposure_frac_of_bankroll": round(total / eq, 6),
        "event_count": len(by_event), "category_count": len(by_cat),
        "cluster_count": len(by_cluster),
        "event_hhi": _hhi(by_event, total), "category_hhi": _hhi(by_cat, total),
        "max_event_exposure_usd": round(top_event[1], 4),
        "max_event_exposure_frac": round(top_event[1] / eq, 6),
        "max_category_exposure_usd": round(top_cat[1], 4),
        "max_category_exposure_frac": round(top_cat[1] / eq, 6),
    }


@dataclass
class PortfolioRiskDecision:
    allow: bool
    capped_notional_usd: float
    reasons: list

    def to_dict(self) -> dict:
        return {"allow": self.allow,
                "capped_notional_usd": round(self.capped_notional_usd, 4),
                "reasons": list(self.reasons)}


class PortfolioRiskEngine:
    """Portfolio-level concentration gate/cap. Reads config caps as FRACTIONS of bankroll."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        g = lambda n, d: _f(getattr(cfg, n, d), d)  # noqa: E731
        self.max_event_frac = g("max_event_exposure_frac", 0.20)
        self.max_category_frac = g("max_category_exposure_frac", 0.40)
        self.max_total_frac = g("max_portfolio_exposure_frac", 0.80)
        self.cvar_limit_frac = g("portfolio_cvar_limit_frac", 0.0)   # 0 = off

    def check_candidate(self, *, notional_usd: float, event_key: str, category: str,
                        positions: list, bankroll: float,
                        recent_returns=None) -> PortfolioRiskDecision:
        """Allow / size-cap / block a new paper trade by portfolio concentration. Caps the
        notional to keep each event / category / total exposure within its bankroll-fraction
        limit; blocks only when even the floor size would breach (caller treats as shadow).
        TIGHTEN-ONLY — the returned notional never exceeds the requested one."""
        eq = max(1e-9, float(bankroll))
        by_event, by_cat, _cl, total = _exposures(positions)
        req = max(0.0, float(notional_usd))
        cap = req
        reasons: list = []

        def _headroom(used: float, frac_cap: float) -> float:
            return max(0.0, frac_cap * eq - used)

        ev_used = by_event.get(str(event_key or ""), 0.0)
        ev_room = _headroom(ev_used, self.max_event_frac)
        if cap > ev_room:
            cap = ev_room
            reasons.append("event_concentration_cap")
        cat_used = by_cat.get(str(category or "uncategorized"), 0.0)
        cat_room = _headroom(cat_used, self.max_category_frac)
        if cap > cat_room:
            cap = cat_room
            reasons.append("category_concentration_cap")
        tot_room = _headroom(total, self.max_total_frac)
        if cap > tot_room:
            cap = tot_room
            reasons.append("portfolio_exposure_cap")

        # optional CVaR throttle: when the realized-return CVaR is worse than the limit,
        # halve new risk (risk-off) — never increases size.
        if self.cvar_limit_frac > 0.0 and recent_returns:
            cvar = historical_cvar(recent_returns)
            if cvar < -abs(self.cvar_limit_frac):
                cap = min(cap, req * 0.5)
                reasons.append("cvar_throttle")

        allow = cap > 0.0
        return PortfolioRiskDecision(allow=allow, capped_notional_usd=round(max(0.0, cap), 4),
                                     reasons=reasons)

    def report(self, *, positions: list, bankroll: float, recent_returns=None) -> dict:
        rep = {
            "schema": "portfolio_risk/1.0", "paper_only": True,
            "live_trading_enabled": False,
            "max_event_exposure_frac": self.max_event_frac,
            "max_category_exposure_frac": self.max_category_frac,
            "max_portfolio_exposure_frac": self.max_total_frac,
            "portfolio_cvar_limit_frac": self.cvar_limit_frac,
            "var_95": historical_var(recent_returns or []),
            "cvar_95": historical_cvar(recent_returns or []),
            "return_samples": len(recent_returns or []),
        }
        rep.update(concentration_report(positions, bankroll=bankroll))
        return rep
