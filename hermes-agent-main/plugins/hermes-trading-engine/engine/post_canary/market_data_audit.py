"""MarketDataAudit (Phase 10). Validates BBO/orderbook freshness, sequence/tick
cleanliness, venue/market status, and spread at submit time."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .schemas import MarketDataAuditResult, aggregate_status, make_check

_OPEN = ("open", "active", "trading")


def _d(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def run(ctx: dict, cfg) -> MarketDataAuditResult:
    md = ctx.get("market_data") or {}
    checks = []

    bbo = md.get("bbo_age_ms")
    if bbo is None:
        checks.append(make_check("bbo_fresh_at_submit", "UNKNOWN", "ERROR",
                                 "no BBO age captured"))
    else:
        checks.append(make_check("bbo_fresh_at_submit",
                                 "PASS" if int(bbo) <= cfg.max_bbo_age_ms else "FAIL", "ERROR",
                                 observed=bbo, threshold=cfg.max_bbo_age_ms))
    ob = md.get("orderbook_age_ms")
    if ob is None:
        checks.append(make_check("orderbook_fresh_at_submit", "UNKNOWN", "ERROR",
                                 "no orderbook age captured"))
    else:
        checks.append(make_check("orderbook_fresh_at_submit",
                                 "PASS" if int(ob) <= cfg.max_orderbook_age_ms else "FAIL",
                                 "ERROR", observed=ob, threshold=cfg.max_orderbook_age_ms))
    spread = _d(md.get("spread"))
    if spread is not None:
        checks.append(make_check("spread_within_limit",
                                 "PASS" if spread <= Decimal(str(cfg.max_spread)) else "FAIL",
                                 "ERROR", observed=spread, threshold=cfg.max_spread))
    if cfg.require_sequence_clean:
        checks.append(make_check("sequence_clean",
                                 "FAIL" if md.get("sequence_gap") else "PASS", "CRITICAL"))
    if cfg.require_tick_clean:
        checks.append(make_check("tick_clean", "FAIL" if md.get("tick_dirty") else "PASS",
                                 "CRITICAL"))
    vs = str(md.get("venue_status", "ready")).lower()
    checks.append(make_check("venue_not_degraded",
                             "FAIL" if vs in ("degraded", "disconnected", "reconnecting", "failed")
                             else "PASS", "ERROR", observed=vs))
    ms = str(md.get("market_status", "open")).lower()
    if cfg.require_market_open_at_submit:
        checks.append(make_check("market_open_at_submit",
                                 "PASS" if ms in _OPEN else "FAIL", "CRITICAL", observed=ms))
    if md.get("resolved"):
        checks.append(make_check("market_not_resolved", "FAIL", "CRITICAL",
                                 "market resolved at submit"))

    return MarketDataAuditResult(
        status=aggregate_status(checks), checks=checks,
        bbo_age_ms=bbo, orderbook_age_ms=ob, spread=spread,
        depth_at_limit=_d(md.get("depth_at_limit")),
        sequence_gap_detected=bool(md.get("sequence_gap")), tick_dirty=bool(md.get("tick_dirty")),
        venue_status=md.get("venue_status"), market_status=md.get("market_status"))
