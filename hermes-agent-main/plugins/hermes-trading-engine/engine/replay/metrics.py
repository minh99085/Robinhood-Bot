"""Deterministic replay metrics. Honest after fees, slippage, rejects, partial
fills and stale-data blocks. All functions are divide-by-zero safe and pure.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Optional

MIN_SAMPLE_WARN = 30


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
def equity_series(equity_rows: list[dict]) -> list[float]:
    return [_f(r.get("equity")) for r in equity_rows]


def max_drawdown(equities: list[float]) -> tuple[float, float]:
    """Return (abs_drawdown, pct_drawdown). Empty/short -> (0, 0)."""
    if not equities:
        return 0.0, 0.0
    peak = equities[0]
    mdd_abs = 0.0
    mdd_pct = 0.0
    for v in equities:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > mdd_abs:
            mdd_abs = dd
        if peak > 0 and (dd / peak) > mdd_pct:
            mdd_pct = dd / peak
    return mdd_abs, mdd_pct


def simple_returns(equities: list[float]) -> list[float]:
    out = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev != 0:
            out.append((equities[i] - prev) / prev)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(equities: list[float]) -> float:
    rets = simple_returns(equities)
    sd = _std(rets)
    if sd == 0:
        return 0.0
    return _mean(rets) / sd * math.sqrt(len(rets))


def sortino(equities: list[float]) -> float:
    rets = simple_returns(equities)
    downside = [r for r in rets if r < 0]
    dd = _std(downside)
    if dd == 0:
        return 0.0
    return _mean(rets) / dd * math.sqrt(len(rets))


def volatility(equities: list[float]) -> float:
    return _std(simple_returns(equities))


# --------------------------------------------------------------------------- #
def fill_ratio(orders: list[dict], fills: list[dict]) -> float:
    if not orders:
        return 0.0
    filled = {f.get("client_order_id") for f in fills}
    n_filled = sum(1 for o in orders if o.get("client_order_id") in filled)
    return n_filled / len(orders)


def partial_fill_ratio(orders: list[dict]) -> float:
    if not orders:
        return 0.0
    n = sum(1 for o in orders if o.get("status") == "PARTIALLY_FILLED")
    return n / len(orders)


def fee_total(fills: list[dict]) -> float:
    return sum(_f(f.get("fee")) for f in fills)


def average_fee_per_fill(fills: list[dict]) -> float:
    return fee_total(fills) / len(fills) if fills else 0.0


def rejection_reasons(orders: list[dict], risk_decisions: list[dict]) -> dict:
    c = Counter()
    for o in orders:
        if o.get("reject_reason"):
            c[o["reject_reason"]] += 1
    for d in risk_decisions:
        if not d.get("approved") and d.get("reason"):
            c[d["reason"]] += 1
    return dict(c)


def pnl_by(positions: list[dict], key: str) -> dict:
    agg = defaultdict(float)
    for p in positions:
        k = p.get(key) or "?"
        agg[k] += _f(p.get("realized_pnl")) + _f(p.get("unrealized_pnl"))
    return {k: round(v, 6) for k, v in agg.items()}


# --------------------------------------------------------------------------- #
def summarize(*, config, equity_rows: list[dict], orders: list[dict], fills: list[dict],
              proposals: list[dict], risk_decisions: list[dict], positions: list[dict],
              md_counters: Optional[dict] = None,
              calibration: Optional[dict] = None) -> dict:
    md_counters = md_counters or {}
    equities = equity_series(equity_rows)
    starting_cash = _f(getattr(config, "initial_cash", 0.0))
    ending_equity = equities[-1] if equities else starting_cash
    realized = sum(_f(p.get("realized_pnl")) for p in positions)
    unrealized = sum(_f(p.get("unrealized_pnl")) for p in positions)
    fees = fee_total(fills)
    mdd_abs, mdd_pct = max_drawdown(equities)

    approvals = sum(1 for d in risk_decisions if d.get("approved"))
    n_dec = len(risk_decisions)
    rej = rejection_reasons(orders, risk_decisions)

    warnings = []
    if len(equities) < 3:
        warnings.append("insufficient_equity_samples")
    if len(fills) < MIN_SAMPLE_WARN:
        warnings.append("small_fill_sample")

    metrics = {
        "starting_cash": round(starting_cash, 6),
        "ending_equity": round(ending_equity, 6),
        "total_pnl": round(ending_equity - starting_cash, 6),
        "total_return": round((ending_equity - starting_cash) / starting_cash, 6) if starting_cash else 0.0,
        "realized_pnl": round(realized, 6),
        "unrealized_pnl": round(unrealized, 6),
        "max_drawdown": round(mdd_abs, 6),
        "max_drawdown_pct": round(mdd_pct, 6),
        "volatility": round(volatility(equities), 6),
        "sharpe": round(sharpe(equities), 6),
        "sortino": round(sortino(equities), 6),
        "order_count": len(orders),
        "fill_count": len(fills),
        "proposal_count": len(proposals),
        "fill_ratio": round(fill_ratio(orders, fills), 6),
        "partial_fill_ratio": round(partial_fill_ratio(orders), 6),
        "cancel_count": sum(1 for o in orders if o.get("status") == "CANCELLED"),
        "reject_count": sum(1 for o in orders if o.get("status") in ("REJECTED", "RISK_REJECTED")),
        "rejection_reasons": rej,
        "total_fees": round(fees, 6),
        "average_fee_per_fill": round(average_fee_per_fill(fills), 6),
        "fee_drag_pct": round(fees / starting_cash, 6) if starting_cash else 0.0,
        "risk_approval_rate": round(approvals / n_dec, 6) if n_dec else 0.0,
        "risk_rejection_rate": round((n_dec - approvals) / n_dec, 6) if n_dec else 0.0,
        "stale_data_rejections": rej.get("stale_market_data", 0),
        "excessive_spread_rejections": rej.get("excessive_spread", 0),
        "tick_size_rejections": rej.get("tick_size_changed_requires_refresh", 0),
        "pnl_by_market": pnl_by(positions, "market_id"),
        "pnl_by_asset": pnl_by(positions, "asset_id"),
        "pnl_by_venue": pnl_by(positions, "venue"),
        "events_processed": md_counters.get("events_processed", 0),
        "book_events": md_counters.get("book", 0),
        "price_change_events": md_counters.get("price_change", 0),
        "tick_size_change_events": md_counters.get("tick_size_change", 0),
        "best_bid_ask_events": md_counters.get("best_bid_ask", 0),
        "market_resolved_events": md_counters.get("market_resolved", 0),
        "malformed_events": md_counters.get("malformed", 0),
        "max_gap_between_events_ms": md_counters.get("max_gap_ms", 0),
        "warnings": warnings,
    }
    if calibration:
        metrics["calibration"] = calibration
    return metrics


# ===========================================================================
# Institutional metrics extension (Backtesting & Simulation, Strategy
# Optimization & Robustness Testing). All pure, deterministic, divide-by-zero
# safe. Added without changing the existing `summarize` output.
# ===========================================================================

import logging as _logging

_log = _logging.getLogger("hte.replay.institutional")


def calmar(equities: list[float]) -> float:
    """Cumulative return divided by absolute max drawdown (annualization-free)."""
    if len(equities) < 2 or equities[0] == 0:
        return 0.0
    total_ret = (equities[-1] - equities[0]) / abs(equities[0])
    _, dd = max_drawdown(equities)
    return round(total_ret / abs(dd), 6) if dd not in (0, 0.0) else 0.0


def omega(returns: list[float], threshold: float = 0.0) -> float:
    """Omega ratio: sum of gains above threshold / sum of losses below it."""
    gains = sum(max(0.0, r - threshold) for r in returns)
    losses = sum(max(0.0, threshold - r) for r in returns)
    return round(gains / losses, 6) if losses > 1e-12 else (float("inf") if gains > 0 else 0.0)


def expectancy(trade_pnls: list[float]) -> float:
    """Mean PnL per closed trade."""
    return round(_mean(trade_pnls), 6) if trade_pnls else 0.0


def hit_rate(trade_pnls: list[float]) -> float:
    """Fraction of trades with strictly positive PnL."""
    if not trade_pnls:
        return 0.0
    return round(sum(1 for p in trade_pnls if p > 0) / len(trade_pnls), 6)


def profit_factor(trade_pnls: list[float]) -> float:
    """Gross profit / gross loss."""
    wins = sum(p for p in trade_pnls if p > 0)
    losses = -sum(p for p in trade_pnls if p < 0)
    return round(wins / losses, 6) if losses > 1e-12 else (float("inf") if wins > 0 else 0.0)


def turnover(notional_traded: float, avg_equity: float) -> float:
    """Traded notional relative to average equity."""
    return round(notional_traded / avg_equity, 6) if avg_equity > 1e-12 else 0.0


def slippage_drag(slippage_cost: float, notional_traded: float) -> float:
    """Slippage cost as a fraction of traded notional."""
    return round(slippage_cost / notional_traded, 6) if notional_traded > 1e-12 else 0.0


def fee_drag(fees: float, notional_traded: float) -> float:
    """Fees as a fraction of traded notional."""
    return round(fees / notional_traded, 6) if notional_traded > 1e-12 else 0.0


def drawdown_duration(equities: list[float]) -> int:
    """Longest run (in steps) spent strictly below a prior equity peak."""
    peak = equities[0] if equities else 0.0
    longest = cur = 0
    for e in equities:
        if e >= peak:
            peak = e
            cur = 0
        else:
            cur += 1
            longest = max(longest, cur)
    return longest


def realized_edge(trades: list[dict]) -> float:
    """Mean realized per-unit edge across trades (``realized_pnl`` / ``cost``)."""
    vals = []
    for t in trades or []:
        cost = _f(t.get("cost"), 0.0) or (_f(t.get("entry_price")) * _f(t.get("qty")))
        if cost > 1e-12:
            vals.append(_f(t.get("realized_pnl")) / cost)
    return round(_mean(vals), 6) if vals else 0.0


def expected_vs_realized_edge(trades: list[dict]) -> dict:
    """Mean expected net edge vs mean realized edge (model honesty check)."""
    exp = [_f(t.get("net_edge")) for t in (trades or []) if t.get("net_edge") is not None]
    rea = []
    for t in trades or []:
        cost = _f(t.get("cost"), 0.0) or (_f(t.get("entry_price")) * _f(t.get("qty")))
        if cost > 1e-12:
            rea.append(_f(t.get("realized_pnl")) / cost)
    e, r = (_mean(exp) if exp else 0.0), (_mean(rea) if rea else 0.0)
    return {"expected": round(e, 6), "realized": round(r, 6), "gap": round(e - r, 6)}


def brier_score(predictions: list[float], outcomes: list[float]) -> float:
    """Mean squared error between predicted probabilities and binary outcomes."""
    if not predictions:
        return 0.0
    return round(sum((p - y) ** 2 for p, y in zip(predictions, outcomes)) / len(predictions), 6)


def log_loss(predictions: list[float], outcomes: list[float], eps: float = 1e-9) -> float:
    """Binary cross-entropy (clamped)."""
    if not predictions:
        return 0.0
    s = 0.0
    for p, y in zip(predictions, outcomes):
        p = min(1 - eps, max(eps, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return round(s / len(predictions), 6)


def ece(predictions: list[float], outcomes: list[float], bins: int = 10) -> float:
    """Expected Calibration Error over equal-width probability bins."""
    n = len(predictions)
    if n == 0:
        return 0.0
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(predictions)
               if (lo <= p < hi) or (b == bins - 1 and p == hi)]
        if not idx:
            continue
        conf = sum(predictions[i] for i in idx) / len(idx)
        acc = sum(outcomes[i] for i in idx) / len(idx)
        total += (len(idx) / n) * abs(acc - conf)
    return round(total, 6)


def calibration_error(predictions: list[float], outcomes: list[float]) -> float:
    """Alias for ECE (calibration quality)."""
    return ece(predictions, outcomes)


def calibration_slope_intercept(predictions: list[float], outcomes: list[float]
                                ) -> tuple[float, float]:
    """Cox calibration slope + intercept (1.0 / 0.0 == perfectly calibrated)."""
    from engine.calibration_models import calibration_slope_intercept as _csi
    return _csi(list(zip(predictions, [int(o) for o in outcomes])))


def reliability_buckets(predictions: list[float], outcomes: list[float],
                        bins: int = 10) -> list:
    """Reliability table (predicted vs realized per probability bin)."""
    from engine.calibration_models import reliability_buckets as _rb
    return _rb(list(zip(predictions, [int(o) for o in outcomes])), bins)


def strategy_attribution(trades: list[dict], key: str = "category") -> dict:
    """Realized PnL + trade counts attributed by ``key`` (e.g. category, side,
    outcome). Used for Strategy Development attribution + Live Monitoring."""
    pnl: dict = defaultdict(float)
    cnt: dict = defaultdict(int)
    for t in trades or []:
        k = str(t.get(key, "unknown"))
        pnl[k] = round(pnl[k] + _f(t.get("realized_pnl")), 6)
        cnt[k] += 1
    return {k: {"pnl": pnl[k], "trades": cnt[k]} for k in pnl}


def bregman_metrics(opportunities: Optional[list] = None) -> dict:
    """Aggregate Bregman arbitrage certification results (Bregman arbitrage
    priority + Backtesting & Simulation).

    ``opportunities``: list of ``CertifiedBregmanOpportunity.to_dict()`` (or
    objects). Reports certified profit, false-positive rate (a certification
    self-consistency check: any leg labelled tradable with a non-positive profit
    lower bound), rejection reasons, opportunity persistence, and capital
    efficiency. Pure + divide-by-zero safe.
    """
    opps = opportunities or []

    def g(o, k, default=None):
        return (o.get(k, default) if isinstance(o, dict) else getattr(o, k, default))

    total = len(opps)
    tradable = [o for o in opps if g(o, "is_opportunity", False)]
    certified = [o for o in opps if g(o, "certified", False)]
    certified_profit = round(sum(_f(g(o, "profit_lower_bound", 0.0)) for o in tradable), 6)
    required_capital = round(sum(_f(g(o, "required_capital", 0.0)) for o in tradable), 6)
    # false positive = labelled tradable but not actually profitable after costs
    false_positives = sum(1 for o in tradable if _f(g(o, "profit_lower_bound", 0.0)) <= 0.0)
    persistence = [_f(g(o, "persistence_score", 0.0)) for o in tradable]
    reasons: dict = Counter()
    for o in opps:
        if not g(o, "is_opportunity", False):
            r = g(o, "no_trade_reason", "") or "none"
            reasons[r] += 1
    return {
        "groups_scanned": total,
        "certified_count": len(certified),
        "opportunity_count": len(tradable),
        "certified_profit": certified_profit,
        "required_capital": required_capital,
        "capital_efficiency": round(certified_profit / required_capital, 6) if required_capital > 1e-12 else 0.0,
        "false_positive_rate": round(false_positives / len(tradable), 6) if tradable else 0.0,
        "mean_persistence": round(sum(persistence) / len(persistence), 6) if persistence else 0.0,
        "mean_divergence_gap": round(
            sum(_f(g(o, "divergence_gap", 0.0)) for o in opps) / total, 8) if total else 0.0,
        "rejection_reasons": dict(reasons),
    }


def bregman_replay_analytics(opportunities: Optional[list] = None, *,
                             capital_lock_ticks: float = 1.0) -> dict:
    """Bregman-specific REPLAY analytics (Bregman arbitrage validation).

    Extends :func:`bregman_metrics` with all-leg fill feasibility, depth decay,
    persistence, capital-lock duration, rejected-opportunity reasons, and a
    false-positive check. ``opportunities`` are ``CertifiedBregmanOpportunity``
    dicts (or objects). Pure + deterministic.
    """
    opps = opportunities or []

    def g(o, k, default=None):
        return (o.get(k, default) if isinstance(o, dict) else getattr(o, k, default))

    base = bregman_metrics(opps)
    tradable = [o for o in opps if g(o, "is_opportunity", False)]
    feas = [_f(g(o, "fill_feasibility", 0.0)) for o in tradable]
    all_leg_feas = round(sum(feas) / len(feas), 6) if feas else 0.0
    depth_decay = round(1.0 - all_leg_feas, 6)
    capital_lock = round(sum(_f(g(o, "required_capital", 0.0)) for o in tradable)
                         * float(capital_lock_ticks), 6)
    base.update({
        "all_leg_fill_feasibility": all_leg_feas,
        "depth_decay": depth_decay,
        "persistence": base.get("mean_persistence", 0.0),
        "capital_lock_duration": capital_lock,
        "rejected_opportunity_reasons": base.get("rejection_reasons", {}),
        "false_positive_check_passed": base.get("false_positive_rate", 0.0) == 0.0,
    })
    return base


def chainlink_replay_analytics(signals: Optional[list] = None) -> dict:
    """Chainlink REPLAY analytics: snapshot freshness, matched-market count, stale
    rejection count, oracle deviation, and probability impact. ``signals`` are
    ``ChainlinkSignal.to_dict()`` records (or dicts with ``features``)."""
    sigs = signals or []

    def feat(s, k):
        f = s.get("features") if isinstance(s, dict) else getattr(s, "features", {})
        return (f or {}).get(k)

    matched = [s for s in sigs if (s.get("feed_key") if isinstance(s, dict)
                                   else getattr(s, "feed_key", None))]
    stale = [s for s in sigs
             if (s.get("no_trade") if isinstance(s, dict) else getattr(s, "no_trade", False))]
    fresh_vals = [_f(feat(s, "freshness")) for s in matched if feat(s, "freshness") is not None]
    dev_vals = [abs(_f(feat(s, "deviation"), 0.0)) for s in matched if feat(s, "deviation") is not None]
    impact = [abs(_f(s.get("prob_adjustment") if isinstance(s, dict)
                     else getattr(s, "prob_adjustment", 0.0), 0.0)) for s in sigs]
    return {
        "signal_count": len(sigs),
        "matched_market_count": len(matched),
        "stale_rejection_count": len(stale),
        "snapshot_freshness": round(sum(fresh_vals) / len(fresh_vals), 6) if fresh_vals else 0.0,
        "oracle_deviation": round(sum(dev_vals) / len(dev_vals), 6) if dev_vals else 0.0,
        "probability_impact": round(sum(impact) / len(impact), 6) if impact else 0.0,
    }


def execution_diagnostics(orders: list, fills: list, *, bundles: Optional[list] = None) -> dict:
    """Realistic-execution diagnostics for PAPER/replay reports (CLOB v2 sim +
    Monitoring): fill rate, partial-fill rate, average slippage (fill vs limit),
    average adverse markout, and failed-bundle rate. Defensive + divide-by-zero
    safe; works off plain order/fill/bundle dicts."""
    orders = orders or []
    fills = fills or []
    bundles = bundles or []
    n_orders = len(orders)
    filled = [o for o in orders if str(o.get("status")) in ("FILLED", "PARTIALLY_FILLED")]
    partials = [o for o in orders if str(o.get("status")) == "PARTIALLY_FILLED"]

    slips, markouts = [], []
    for f in fills:
        lp = _f(f.get("limit_price")) if f.get("limit_price") not in (None, "") else None
        px = _f(f.get("price"))
        side = str(f.get("side", "BUY")).upper()
        if lp and px and lp > 0:
            # signed slippage in bps (positive = paid worse than limit on a BUY)
            slips.append((px - lp) / lp * 10000.0 if side == "BUY" else (lp - px) / lp * 10000.0)
        mk = f.get("markout_bps")
        if mk not in (None, ""):
            markouts.append(_f(mk))

    failed_bundles = sum(1 for b in bundles if not b.get("fully_hedged", True))
    return {
        "order_count": n_orders,
        "fill_count": len(filled),
        "fill_rate": round(len(filled) / n_orders, 6) if n_orders else 0.0,
        "partial_fill_rate": round(len(partials) / n_orders, 6) if n_orders else 0.0,
        "avg_slippage_bps": round(sum(slips) / len(slips), 4) if slips else 0.0,
        "avg_markout_bps": round(sum(markouts) / len(markouts), 4) if markouts else 0.0,
        "bundle_count": len(bundles),
        "failed_bundle_rate": round(failed_bundles / len(bundles), 6) if bundles else 0.0,
    }


def dependency_graph_metrics(graph) -> dict:
    """Market-dependency-graph report artifact for replay/training reports
    (Monitoring + Bregman arbitrage structure). Defensive: ``None`` graph -> {}."""
    if graph is None:
        return {}
    try:
        return graph.to_report()
    except Exception:  # noqa: BLE001
        return {}


def cluster_exposure_metrics(positions: Optional[list], graph, *,
                             max_cluster_exposure_usd: float = 50.0) -> dict:
    """Per-correlated-cluster exposure (gross + same-event-hedged net) + the
    clusters breaching the cap (Risk / Portfolio Optimization). Proves the graph
    nets offsetting same-event hedges and flags correlated-cluster overexposure.
    Defensive: missing graph/positions -> empty summary."""
    if graph is None or not positions:
        return {"clusters": {}, "overexposed": [], "max_cluster_exposure_usd": max_cluster_exposure_usd}
    try:
        from engine.training.dependency_graph import ClusterExposureNetter
        netter = ClusterExposureNetter(graph, max_cluster_exposure_usd=max_cluster_exposure_usd)
        exposures = netter.cluster_exposures(positions)
        return {
            "clusters": exposures,
            "overexposed": netter.overexposed(positions),
            "max_cluster_exposure_usd": max_cluster_exposure_usd,
            "gross_total": round(sum(v["gross"] for v in exposures.values()), 6),
            "net_total": round(sum(v["net"] for v in exposures.values()), 6),
        }
    except Exception:  # noqa: BLE001
        return {"clusters": {}, "overexposed": [], "max_cluster_exposure_usd": max_cluster_exposure_usd}


def institutional_metrics(*, equity_rows: Optional[list] = None,
                          equities: Optional[list] = None,
                          trades: Optional[list] = None,
                          decisions: int = 0, rejections: int = 0,
                          explorations: int = 0,
                          predictions: Optional[list] = None,
                          outcomes: Optional[list] = None,
                          fees: float = 0.0, slippage_cost: float = 0.0,
                          notional_traded: float = 0.0,
                          attribution_key: str = "category") -> dict:
    """Compute the full institutional metric suite from replay/paper artifacts.

    Inputs are optional + defensive: missing data yields neutral (0) metrics, no
    exceptions. ``trades`` are closed-trade dicts (``realized_pnl``, ``cost`` or
    ``entry_price``/``qty``, ``net_edge``, plus an attribution key).
    """
    eqs = equities if equities is not None else (
        equity_series(equity_rows) if equity_rows else [])
    rets = simple_returns(eqs)
    trade_pnls = [_f(t.get("realized_pnl")) for t in (trades or [])]
    _, dd = max_drawdown(eqs) if eqs else (0.0, 0.0)
    avg_equity = _mean(eqs) if eqs else 0.0
    trade_count = len(trades or [])
    rejection_rate = round(rejections / decisions, 6) if decisions else 0.0
    exploration_rate = round(explorations / trade_count, 6) if trade_count else 0.0
    preds = predictions or []
    outs = outcomes or []
    metrics = {
        "trade_count": trade_count,
        "decision_count": decisions,
        "rejection_rate": rejection_rate,
        "exploration_rate": exploration_rate,
        "sharpe": sharpe(eqs),
        "sortino": sortino(eqs),
        "calmar": calmar(eqs),
        "omega": omega(rets),
        "volatility": volatility(eqs),
        "max_drawdown": round(dd, 6),
        "drawdown_duration": drawdown_duration(eqs),
        "expectancy": expectancy(trade_pnls),
        "hit_rate": hit_rate(trade_pnls),
        "profit_factor": profit_factor(trade_pnls),
        "turnover": turnover(notional_traded, avg_equity),
        "slippage_drag": slippage_drag(slippage_cost, notional_traded),
        "fee_drag": fee_drag(fees, notional_traded),
        "realized_edge": realized_edge(trades or []),
        "expected_vs_realized_edge": expected_vs_realized_edge(trades or []),
        "brier_score": brier_score(preds, outs),
        "log_loss": log_loss(preds, outs),
        "ece": ece(preds, outs),
        "calibration_error": calibration_error(preds, outs),
        "strategy_attribution": strategy_attribution(trades or [], key=attribution_key),
    }
    if preds and outs:
        slope, intercept = calibration_slope_intercept(preds, outs)
        metrics["calibration_slope"] = slope
        metrics["calibration_intercept"] = intercept
        metrics["reliability_buckets"] = reliability_buckets(preds, outs)
    _log.debug("institutional_metrics computed: trades=%s decisions=%s sharpe=%s",
               trade_count, decisions, metrics["sharpe"])
    return metrics
