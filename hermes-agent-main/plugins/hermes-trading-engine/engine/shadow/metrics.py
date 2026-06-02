"""Shadow session metrics (Phase 7).

Standalone, deterministic helpers (fill ratio, edge capture, venue breakdown,
markout-by-horizon, calibration) plus a session aggregator that reads the
shadow_* tables. All math tolerates empty/missing data.

Quant scope — *Backtesting & Simulation* + *Live Trading & Monitoring*: shadow
session metrics (fill ratio, edge capture, expected-vs-realized via markout,
rejection reasons) feed the signal hit-rate / realized-edge / Sharpe-contribution
reporting used to validate the priority hierarchy.
"""

from __future__ import annotations

import math
from typing import Optional

_EPS = 1e-9


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fill_ratio(order_count: int, filled_count: int) -> float:
    if order_count <= 0:
        return 0.0
    return round(filled_count / order_count, 6)


def edge_capture(predicted_edges: list[float], realized_pnls: list[float]) -> Optional[float]:
    pe = sum(x for x in predicted_edges if x is not None)
    rp = sum(x for x in realized_pnls if x is not None)
    if abs(pe) < _EPS:
        return None
    return round(rp / pe, 6)


def by_venue(rows: list[dict], value_key: Optional[str] = None) -> dict:
    out: dict[str, float] = {}
    for r in rows:
        v = r.get("venue") or "unknown"
        out[v] = out.get(v, 0.0) + (1.0 if value_key is None else (_f(r.get(value_key), 0.0) or 0.0))
    return dict(sorted(out.items()))


def markout_by_horizon(observations: list[dict]) -> dict:
    agg: dict[int, list[float]] = {}
    for o in observations:
        m = _f(o.get("markout"))
        if m is None:
            continue
        agg.setdefault(int(o.get("horizon_ms") or 0), []).append(m)
    return {str(h): round(sum(v) / len(v), 6) for h, v in sorted(agg.items()) if v}


def brier_score(pairs: list[tuple]) -> Optional[float]:
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 6)


def log_loss(pairs: list[tuple]) -> Optional[float]:
    if not pairs:
        return None
    tot = 0.0
    for p, y in pairs:
        p = min(1 - _EPS, max(_EPS, p))
        tot += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return round(tot / len(pairs), 6)


def ece(pairs: list[tuple], n_buckets: int = 10) -> Optional[float]:
    if not pairs:
        return None
    buckets: list[list[tuple]] = [[] for _ in range(n_buckets)]
    for p, y in pairs:
        buckets[min(n_buckets - 1, max(0, int(p * n_buckets)))].append((p, y))
    n = len(pairs)
    e = 0.0
    for b in buckets:
        if not b:
            continue
        ap = sum(p for p, _ in b) / len(b)
        fr = sum(y for _, y in b) / len(b)
        e += (len(b) / n) * abs(ap - fr)
    return round(e, 6)


def compute_session_metrics(store, session_id: str, config=None, counters: Optional[dict] = None) -> dict:
    """Aggregate metrics from shadow_* tables. Best-effort; never raises."""
    counters = counters or {}
    try:
        decisions = store.get_shadow_rows("shadow_decisions", session_id)
        orders = store.get_shadow_rows("shadow_orders", session_id)
        fills = store.get_shadow_rows("shadow_fills", session_id)
        observations = store.get_shadow_rows("shadow_observations", session_id)
        candidates = store.get_shadow_rows("shadow_candidates", session_id)
    except Exception:  # noqa: BLE001
        decisions, orders, fills, observations, candidates = [], [], [], [], []

    n_orders = len(orders)
    filled = sum(1 for o in orders if str(o.get("status")) in ("FILLED", "PARTIALLY_FILLED"))
    approved = sum(1 for d in decisions if d.get("decision") == "APPROVED_SHADOW")
    risk_rej = sum(1 for d in decisions if d.get("decision") == "RISK_REJECTED")
    abstain = sum(1 for d in decisions if d.get("decision") == "ABSTAINED")
    proposed = sum(1 for d in decisions if d.get("decision") in ("PROPOSED", "APPROVED_SHADOW", "RISK_REJECTED"))
    errors = sum(1 for d in decisions if d.get("decision") == "ERROR")
    total_fees = sum(_f(f.get("fee"), 0.0) or 0.0 for f in fills)

    rejection_reasons: dict[str, int] = {}
    for d in decisions:
        if d.get("decision") in ("ABSTAINED", "RISK_REJECTED"):
            rejection_reasons[d.get("reason") or "unknown"] = \
                rejection_reasons.get(d.get("reason") or "unknown", 0) + 1

    return {
        "decision_count": len(decisions), "candidate_count": len(candidates),
        "selected_candidate_count": sum(1 for c in candidates if c.get("selected")),
        "proposal_count": proposed, "approved_shadow_order_count": approved,
        "rejected_proposal_count": risk_rej, "abstention_count": abstain, "error_count": errors,
        "shadow_order_count": n_orders, "shadow_fill_count": len(fills),
        "fill_ratio": fill_ratio(n_orders, filled),
        "risk_approval_rate": round(approved / proposed, 6) if proposed else 0.0,
        "risk_rejection_rate": round(risk_rej / proposed, 6) if proposed else 0.0,
        "reject_rate": round((risk_rej + abstain) / len(decisions), 6) if decisions else 0.0,
        "rejection_reasons": rejection_reasons, "total_fees": round(total_fees, 6),
        "markout_by_horizon_ms": markout_by_horizon(observations),
        "pnl_by_venue": by_venue(fills),
        "risk_bypass_count": int(counters.get("risk_bypass_count", 0)),
        "unhandled_exception_count": int(counters.get("unhandled_exception_count", 0)),
        "live_order_endpoint_calls": int(counters.get("live_order_endpoint_calls", 0)),
    }
