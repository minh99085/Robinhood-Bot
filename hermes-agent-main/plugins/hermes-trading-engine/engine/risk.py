"""Deterministic RiskEngine — the single mandatory gate for every order.

No code path may open even a *simulated* order without an approved
:class:`~engine.schemas.RiskDecision` from this engine. The engine is pure and
deterministic: given the same :class:`~engine.schemas.TradeProposal` and
:class:`RiskContext` (and the same kill-switch file state) it always returns the
same verdict. It performs no network I/O and consults no LLM — Grok may propose,
but only this engine approves.

Quant scope — *Risk Management & Portfolio Optimization* + *Compliance*: the
mandatory choke point. Every flagship Bregman-arbitrage hedge leg is routed
through this gate (each leg pre-checked before any leg is placed), so a
certified "risk-free" arbitrage still cannot exceed paper exposure/notional
caps or bypass the kill switch. New checks only ever make the gate stricter.

Limits are config-driven via ``HTE_RISK_*`` environment variables (with safe
defaults). A kill-switch file (``HTE_KILL_SWITCH_FILE``, default
``<data_dir>/KILL_SWITCH``) blocks *all* orders the instant it exists.
"""

from __future__ import annotations

# The deterministic RiskEngine gate is MANDATORY for every simulated order and
# has no bypass; campaign-safe startup validation asserts this stays true.
def risk_engine_required() -> bool:
    """True — RiskEngine evaluation is mandatory (no bypass)."""
    return True

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .schemas import RiskDecision, TradeProposal


class RiskCode:
    OK = "OK"
    KILL_SWITCH = "KILL_SWITCH"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    MAX_OPEN_ORDERS = "MAX_OPEN_ORDERS"
    OVERSIZE_ORDER = "OVERSIZE_ORDER"
    MARKET_EXPOSURE = "MARKET_EXPOSURE"
    TOTAL_EXPOSURE = "TOTAL_EXPOSURE"
    DUPLICATE_EXPOSURE = "DUPLICATE_EXPOSURE"
    MAX_SPREAD = "MAX_SPREAD"
    LOW_EDGE = "LOW_EDGE"
    STALE_DATA = "STALE_DATA"
    AMBIGUOUS = "AMBIGUOUS"
    DAILY_LOSS = "DAILY_LOSS"
    # Phase 2: live market-data freshness (Polymarket CLOB). Exact strings are
    # part of the contract surfaced on the dashboard / API.
    STALE_MARKET_DATA = "stale_market_data"
    MISSING_BBO = "missing_bbo"
    EXCESSIVE_SPREAD = "excessive_spread"
    RESOLVED_MARKET = "resolved_market"
    TICK_SIZE_CHANGED = "tick_size_changed_requires_refresh"
    MARKET_DATA_DEGRADED = "market_data_degraded"
    # Phase 5: research/probability gates. Only applied when a proposal carries
    # a required research snapshot. Additive — never relaxes earlier checks.
    RESEARCH_MISSING = "research_missing"
    RESEARCH_INVALID = "research_invalid_estimate"
    RESEARCH_MODE_NOT_ALLOWED = "research_mode_not_allowed"
    RESEARCH_NO_TRADE = "research_no_trade"
    RESEARCH_STALE = "research_estimate_stale"
    RESEARCH_LOW_EVIDENCE = "research_low_evidence"
    RESEARCH_INSUFFICIENT_SOURCES = "research_insufficient_sources"
    RESEARCH_HIGH_AMBIGUITY = "research_high_ambiguity"
    RESEARCH_PROBABILITY_CONFLICT = "research_probability_conflict"
    # Phase 6: venue-neutral market-state gates (Polymarket + Kalshi). Only
    # applied when a proposal carries a required venue snapshot. Additive.
    VENUE_DISABLED = "venue_disabled"
    VENUE_DEGRADED = "venue_degraded"
    MARKET_METADATA_MISSING = "market_metadata_missing"
    MARKET_NOT_TRADABLE = "market_not_tradable"
    MARKET_CLOSED = "market_closed"
    MARKET_SETTLED = "market_settled"
    ORDERBOOK_MISSING = "orderbook_missing"
    BBO_MISSING = "bbo_missing"
    STALE_ORDERBOOK = "stale_orderbook"
    SEQUENCE_GAP_REQUIRES_SNAPSHOT = "sequence_gap_requires_snapshot"
    INVALID_ORDERBOOK_STATE = "invalid_orderbook_state"
    INVALID_PRICE_LEVEL = "invalid_price_level"
    RESOLUTION_RULES_MISSING = "resolution_rules_missing"
    SETTLEMENT_AMBIGUITY_HIGH = "settlement_ambiguity_high"
    UNSUPPORTED_VENUE_MAPPING = "unsupported_venue_mapping"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class RiskLimits:
    """Config-driven risk limits. Fractions are of current equity."""

    # Per-order notional caps. The fractional cap always applies; the absolute
    # cap only applies when > 0 (0 = disabled).
    max_order_notional_frac: float = 0.10
    max_order_notional_abs: float = 0.0
    # Aggregate exposure caps (fraction of equity).
    max_market_exposure_frac: float = 0.30
    max_total_exposure_frac: float = 0.60
    # Concurrency.
    max_open_orders: int = 50
    # Loss control.
    max_daily_loss_frac: float = 0.10
    # Market-quality gates.
    max_spread: float = 0.10            # fractional spread ceiling
    min_edge_after_costs: float = 0.0   # require non-negative edge by default
    max_data_age_s: float = 60.0        # reject stale market data
    max_ambiguity: float = 1.0          # 1.0 = accept all; lower to gate
    # Duplicate same-market-side exposure unless a proposal explicitly opts in.
    allow_duplicate_default: bool = False
    # Phase 5: research-estimate gates (only used when a research snapshot is
    # marked required on the RiskContext).
    research_min_evidence: float = 0.35
    research_min_sources: int = 2
    research_max_ambiguity: float = 0.35
    research_conflict_delta: float = 0.30
    research_conflict_confidence: float = 0.40
    # Phase 6: venue gates.
    venue_require_resolution_rules: bool = True
    venue_max_ambiguity: float = 0.35
    # Kill switch.
    kill_switch_file: Optional[Path] = None

    @classmethod
    def from_env(cls, data_dir: Optional[Path] = None) -> "RiskLimits":
        ks_raw = os.getenv("HTE_KILL_SWITCH_FILE", "").strip()
        if ks_raw:
            ks: Optional[Path] = Path(ks_raw)
        elif data_dir is not None:
            ks = Path(data_dir) / "KILL_SWITCH"
        else:
            ks = None
        return cls(
            max_order_notional_frac=_env_float("HTE_RISK_MAX_ORDER_NOTIONAL_FRAC", 0.10),
            max_order_notional_abs=_env_float("HTE_RISK_MAX_ORDER_NOTIONAL_USD", 0.0),
            max_market_exposure_frac=_env_float("HTE_RISK_MAX_MARKET_EXPOSURE_FRAC", 0.30),
            max_total_exposure_frac=_env_float("HTE_RISK_MAX_TOTAL_EXPOSURE_FRAC", 0.60),
            max_open_orders=_env_int("HTE_RISK_MAX_OPEN_ORDERS", 50),
            max_daily_loss_frac=_env_float("HTE_RISK_MAX_DAILY_LOSS_FRAC", 0.10),
            max_spread=_env_float("HTE_RISK_MAX_SPREAD", 0.10),
            min_edge_after_costs=_env_float("HTE_RISK_MIN_EDGE_AFTER_COSTS", 0.0),
            max_data_age_s=_env_float("HTE_RISK_MAX_DATA_AGE_S", 60.0),
            max_ambiguity=_env_float("HTE_RISK_MAX_AMBIGUITY", 1.0),
            allow_duplicate_default=os.getenv("HTE_RISK_ALLOW_DUPLICATE", "0")
            in ("1", "true", "True", "yes", "on"),
            research_min_evidence=_env_float("RESEARCH_MIN_EVIDENCE_SCORE", 0.35),
            research_min_sources=_env_int("RESEARCH_MIN_SOURCE_COUNT", 2),
            research_max_ambiguity=_env_float("RESEARCH_MAX_AMBIGUITY_SCORE", 0.35),
            research_conflict_delta=_env_float("RESEARCH_PROB_CONFLICT_DELTA", 0.30),
            research_conflict_confidence=_env_float("RESEARCH_PROB_CONFLICT_CONFIDENCE", 0.40),
            venue_require_resolution_rules=os.getenv("VENUE_REQUIRE_RESOLUTION_RULES", "1")
            not in ("0", "false", "False", ""),
            venue_max_ambiguity=_env_float("VENUE_MAX_SETTLEMENT_AMBIGUITY", 0.35),
            kill_switch_file=ks,
        )

    def as_dict(self) -> dict:
        return {
            "max_order_notional_frac": self.max_order_notional_frac,
            "max_order_notional_usd": self.max_order_notional_abs,
            "max_market_exposure_frac": self.max_market_exposure_frac,
            "max_total_exposure_frac": self.max_total_exposure_frac,
            "max_open_orders": self.max_open_orders,
            "max_daily_loss_frac": self.max_daily_loss_frac,
            "max_spread": self.max_spread,
            "min_edge_after_costs": self.min_edge_after_costs,
            "max_data_age_s": self.max_data_age_s,
            "max_ambiguity": self.max_ambiguity,
            "research_min_evidence": self.research_min_evidence,
            "research_min_sources": self.research_min_sources,
            "research_max_ambiguity": self.research_max_ambiguity,
            "kill_switch_file": str(self.kill_switch_file) if self.kill_switch_file else None,
        }


@dataclass
class MarketDataSnapshot:
    """Live market-data freshness for the asset a proposal depends on.

    Populated by the engine from the read-only CLOB feed. When ``required`` is
    False (e.g. CLOB disabled or market not tracked) the RiskEngine skips these
    checks entirely, preserving Phase 1 behavior exactly.
    """

    required: bool = False
    status: str = "connected"
    bbo_present: bool = True
    stale: bool = False
    resolved: bool = False
    tick_size_dirty: bool = False
    unreliable: bool = False
    spread: Optional[float] = None  # fractional spread from live book, if known


@dataclass
class ResearchSnapshot:
    """Research-estimate provenance for the proposal the engine is judging.

    When ``required`` is False (the default, e.g. RESEARCH_USE_IN_STRATEGY=0 or a
    non-research strategy) the RiskEngine skips every research check, preserving
    Phase 1-4 behavior exactly.
    """

    required: bool = False
    present: bool = True
    invalid: bool = False           # came from a failed/invalid research run
    mode_allowed: bool = True       # estimate produced in an allowed mode
    stale: bool = False             # stale_after_ts_ms passed (incl. replay clock)
    no_trade_reason: Optional[str] = None
    evidence_score: Optional[float] = None
    source_count: Optional[int] = None
    ambiguity_score: Optional[float] = None
    p_ensemble: Optional[float] = None
    p_market: Optional[float] = None
    confidence: Optional[float] = None


@dataclass
class VenueSnapshot:
    """Venue-neutral market state for the proposal's market (Phase 6).

    When ``required`` is False (no venue routing) the RiskEngine skips every venue
    check, preserving Phase 1-5 behavior exactly.
    """

    required: bool = False
    venue: str = "polymarket"
    enabled: bool = True
    degraded: bool = False
    metadata_present: bool = True
    tradable: bool = True
    closed: bool = False
    settled: bool = False
    orderbook_present: bool = True
    bbo_present: bool = True
    stale: bool = False
    seq_gap: bool = False
    needs_snapshot: bool = False
    invalid_book: bool = False
    invalid_price_level: bool = False
    resolution_rules_present: bool = True
    ambiguity_score: Optional[float] = None
    supported_mapping: bool = True


@dataclass
class RiskContext:
    """Live portfolio numbers the engine needs to judge a proposal."""

    equity: float = 0.0
    total_exposure: float = 0.0          # USD across all open positions
    market_exposure: float = 0.0         # USD open in the proposal's market
    has_open_same_market_side: bool = False
    open_orders: int = 0
    day_pnl: float = 0.0
    market_data: Optional[MarketDataSnapshot] = None
    research: Optional[ResearchSnapshot] = None
    venue: Optional[VenueSnapshot] = None


class RiskEngine:
    """Pure, deterministic pre-trade risk checker."""

    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()

    # ------------------------------------------------------------------ #
    def kill_switch_active(self) -> bool:
        ks = self.limits.kill_switch_file
        try:
            return bool(ks and Path(ks).exists())
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    def evaluate(self, proposal: TradeProposal, ctx: RiskContext) -> RiskDecision:
        lim = self.limits
        reasons: list[str] = []
        code = RiskCode.OK

        def fail(c: str, msg: str) -> None:
            nonlocal code
            if code == RiskCode.OK:
                code = c  # first failure sets the primary code
            reasons.append(msg)

        # 0) Kill switch overrides everything.
        if self.kill_switch_active():
            fail(RiskCode.KILL_SWITCH, f"kill switch active ({lim.kill_switch_file})")
            return self._decision(proposal, False, code, reasons)

        # 1) Structural validity.
        if proposal.notional <= 0:
            fail(RiskCode.INVALID_PROPOSAL, f"non-positive notional {proposal.notional}")

        equity = max(0.0, ctx.equity)

        # 2) Concurrency.
        if ctx.open_orders >= lim.max_open_orders:
            fail(RiskCode.MAX_OPEN_ORDERS,
                 f"open orders {ctx.open_orders} >= max {lim.max_open_orders}")

        # 3) Per-order notional caps.
        frac_cap = lim.max_order_notional_frac * equity
        if equity > 0 and proposal.notional > frac_cap:
            fail(RiskCode.OVERSIZE_ORDER,
                 f"notional {proposal.notional:.2f} > {lim.max_order_notional_frac:.0%} "
                 f"of equity ({frac_cap:.2f})")
        if lim.max_order_notional_abs > 0 and proposal.notional > lim.max_order_notional_abs:
            fail(RiskCode.OVERSIZE_ORDER,
                 f"notional {proposal.notional:.2f} > abs cap {lim.max_order_notional_abs:.2f}")

        # 4) Aggregate exposure caps.
        if equity > 0:
            total_cap = lim.max_total_exposure_frac * equity
            if ctx.total_exposure + proposal.notional > total_cap:
                fail(RiskCode.TOTAL_EXPOSURE,
                     f"total exposure {ctx.total_exposure + proposal.notional:.2f} > "
                     f"{lim.max_total_exposure_frac:.0%} of equity ({total_cap:.2f})")
            market_cap = lim.max_market_exposure_frac * equity
            if ctx.market_exposure + proposal.notional > market_cap:
                fail(RiskCode.MARKET_EXPOSURE,
                     f"{proposal.market} exposure {ctx.market_exposure + proposal.notional:.2f} > "
                     f"{lim.max_market_exposure_frac:.0%} of equity ({market_cap:.2f})")

        # 5) No duplicate market-side exposure unless explicitly allowed.
        allow_dup = proposal.allow_duplicate or lim.allow_duplicate_default
        if ctx.has_open_same_market_side and not allow_dup:
            fail(RiskCode.DUPLICATE_EXPOSURE,
                 f"already holding {proposal.side} {proposal.market}:{proposal.symbol}")

        # 6) Market-quality gates.
        if proposal.spread > lim.max_spread:
            fail(RiskCode.MAX_SPREAD,
                 f"spread {proposal.spread:.4f} > max {lim.max_spread:.4f}")
        if proposal.edge_after_costs < lim.min_edge_after_costs:
            fail(RiskCode.LOW_EDGE,
                 f"edge {proposal.edge_after_costs:.4f} < min {lim.min_edge_after_costs:.4f}")
        if proposal.data_age_s > lim.max_data_age_s:
            fail(RiskCode.STALE_DATA,
                 f"data age {proposal.data_age_s:.1f}s > max {lim.max_data_age_s:.1f}s")
        if proposal.ambiguity_score > lim.max_ambiguity:
            fail(RiskCode.AMBIGUOUS,
                 f"ambiguity {proposal.ambiguity_score:.2f} > max {lim.max_ambiguity:.2f}")

        # 7) Daily loss circuit.
        if equity > 0 and ctx.day_pnl <= -abs(lim.max_daily_loss_frac) * equity:
            fail(RiskCode.DAILY_LOSS,
                 f"day P&L {ctx.day_pnl:.2f} <= -{lim.max_daily_loss_frac:.0%} of equity")

        # 8) Live market-data freshness (Phase 2). Only applies when the
        #    proposal is tied to a tracked CLOB market (md.required). These are
        #    ADDITIVE — they never relax any Phase 1 check above.
        md = ctx.market_data
        if md is not None and md.required:
            if md.status in ("disconnected", "connecting", "reconnecting", "degraded"):
                fail(RiskCode.MARKET_DATA_DEGRADED, f"market data status={md.status}")
            if md.resolved:
                fail(RiskCode.RESOLVED_MARKET, "market resolved")
            if md.tick_size_dirty:
                fail(RiskCode.TICK_SIZE_CHANGED, "tick size changed; awaiting book refresh")
            if not md.bbo_present:
                fail(RiskCode.MISSING_BBO, "no BBO for required asset")
            if md.stale:
                fail(RiskCode.STALE_MARKET_DATA, "order book stale beyond max age")
            if md.unreliable:
                fail(RiskCode.MARKET_DATA_DEGRADED, "order book state unreliable (no base snapshot)")
            if md.spread is not None and md.spread > lim.max_spread:
                fail(RiskCode.EXCESSIVE_SPREAD,
                     f"live spread {md.spread:.4f} > max {lim.max_spread:.4f}")

        # 9) Research-estimate gates (Phase 5). Only applies when a research
        #    snapshot is required for this proposal. ADDITIVE — never relaxes
        #    any check above. Grok-derived estimates can only BLOCK, never
        #    approve, and never set size.
        rs = ctx.research
        if rs is not None and rs.required:
            if not rs.present or rs.p_ensemble is None:
                fail(RiskCode.RESEARCH_MISSING, "no research estimate for proposal")
            if rs.invalid:
                fail(RiskCode.RESEARCH_INVALID, "estimate from failed/invalid research run")
            if not rs.mode_allowed:
                fail(RiskCode.RESEARCH_MODE_NOT_ALLOWED, "estimate produced in disallowed mode")
            if rs.no_trade_reason:
                fail(RiskCode.RESEARCH_NO_TRADE, f"research no-trade: {rs.no_trade_reason}")
            if rs.stale:
                fail(RiskCode.RESEARCH_STALE, "research estimate stale")
            if rs.evidence_score is not None and rs.evidence_score < lim.research_min_evidence:
                fail(RiskCode.RESEARCH_LOW_EVIDENCE,
                     f"evidence {rs.evidence_score:.2f} < min {lim.research_min_evidence:.2f}")
            if rs.source_count is not None and rs.source_count < lim.research_min_sources:
                fail(RiskCode.RESEARCH_INSUFFICIENT_SOURCES,
                     f"sources {rs.source_count} < min {lim.research_min_sources}")
            if rs.ambiguity_score is not None and rs.ambiguity_score > lim.research_max_ambiguity:
                fail(RiskCode.RESEARCH_HIGH_AMBIGUITY,
                     f"ambiguity {rs.ambiguity_score:.2f} > max {lim.research_max_ambiguity:.2f}")
            if (rs.p_ensemble is not None and rs.p_market is not None
                    and abs(rs.p_ensemble - rs.p_market) > lim.research_conflict_delta
                    and (rs.confidence or 0.0) < lim.research_conflict_confidence):
                fail(RiskCode.RESEARCH_PROBABILITY_CONFLICT,
                     f"|p_ens-p_mkt| {abs(rs.p_ensemble - rs.p_market):.2f} > "
                     f"{lim.research_conflict_delta:.2f} with low confidence")

        # 10) Venue-neutral market-state gates (Phase 6). Only when a venue
        #     snapshot is required for this proposal. ADDITIVE — blocking-only.
        vs = ctx.venue
        if vs is not None and vs.required:
            if not vs.enabled:
                fail(RiskCode.VENUE_DISABLED, f"venue {vs.venue} disabled")
            if vs.degraded:
                fail(RiskCode.VENUE_DEGRADED, f"venue {vs.venue} market data degraded")
            if not vs.supported_mapping:
                fail(RiskCode.UNSUPPORTED_VENUE_MAPPING, "unsupported venue/outcome mapping")
            if not vs.metadata_present:
                fail(RiskCode.MARKET_METADATA_MISSING, "market metadata missing")
            if vs.settled:
                fail(RiskCode.MARKET_SETTLED, "market settled/determined/resolved")
            if vs.closed:
                fail(RiskCode.MARKET_CLOSED, "market closed")
            if not vs.tradable:
                fail(RiskCode.MARKET_NOT_TRADABLE, "market status not tradable")
            if vs.needs_snapshot or vs.seq_gap:
                fail(RiskCode.SEQUENCE_GAP_REQUIRES_SNAPSHOT, "sequence gap; awaiting snapshot")
            if vs.invalid_book:
                fail(RiskCode.INVALID_ORDERBOOK_STATE, "crossed/invalid normalized book")
            if not vs.orderbook_present:
                fail(RiskCode.ORDERBOOK_MISSING, "no normalized orderbook")
            if not vs.bbo_present:
                fail(RiskCode.BBO_MISSING, "no BBO for market")
            if vs.stale:
                fail(RiskCode.STALE_ORDERBOOK, "orderbook stale beyond max age")
            if vs.invalid_price_level:
                fail(RiskCode.INVALID_PRICE_LEVEL, "price not on a valid tick/level")
            if lim.venue_require_resolution_rules and not vs.resolution_rules_present:
                fail(RiskCode.RESOLUTION_RULES_MISSING, "resolution rules missing")
            if vs.ambiguity_score is not None and vs.ambiguity_score > lim.venue_max_ambiguity:
                fail(RiskCode.SETTLEMENT_AMBIGUITY_HIGH,
                     f"settlement ambiguity {vs.ambiguity_score:.2f} > max {lim.venue_max_ambiguity:.2f}")

        approved = code == RiskCode.OK
        return self._decision(proposal, approved, code, reasons)

    # ------------------------------------------------------------------ #
    def _decision(self, proposal: TradeProposal, approved: bool,
                  code: str, reasons: list[str]) -> RiskDecision:
        return RiskDecision(
            proposal_id=proposal.proposal_id,
            approved=approved,
            code=code if not approved else RiskCode.OK,
            reasons=reasons,
            adjusted_notional=proposal.notional if approved else None,
            limits_snapshot=self.limits.as_dict(),
        )


# --------------------------------------------------------------------------- #
# Market-dependency-graph exposure netting (ADDITIVE, read-only).
#
# This does NOT alter the mandatory RiskEngine gate above — it is a portfolio-
# level helper that nets correlated/same-event exposures over a
# :class:`engine.training.dependency_graph.MarketDependencyGraph` so the trainer
# can avoid concentrating risk in one correlated cluster (Risk Management &
# Portfolio Optimization). Every individual order still passes the RiskEngine.
# --------------------------------------------------------------------------- #
def bregman_trade_allowed(opp) -> bool:
    """Risk-layer gate: a Bregman opportunity may be SIZED + sent to the
    RiskEngine only when it is fully CERTIFIED with a strictly-positive certified
    profit lower bound (and, when a certificate is attached, a proven risk-free
    full hedge). A non-certified candidate may only be LOGGED — never approved or
    up-sized. Quant scope — *Bregman arbitrage priority* + *Compliance/Security*."""
    if opp is None:
        return False
    certified = bool(getattr(opp, "certified", False))
    positive = float(getattr(opp, "profit_lower_bound", 0.0) or 0.0) > 0.0
    cert = getattr(opp, "certificate", None)
    if cert is not None and not bool(getattr(cert, "risk_free", False)):
        return False
    return certified and positive


def risk_gate_violations(positions: list, *, max_market_exposure: float,
                         max_total_exposure: float, max_order_notional: float,
                         open_positions: Optional[list] = None) -> int:
    """Post-hoc count of HARD risk-cap violations across a position book.

    Live-readiness input (Risk Management & Compliance): with the mandatory
    TrainingRiskGate/RiskEngine in front of every order this MUST be 0 — a
    non-zero count means a cap was breached and the strategy is NOT live-ready.
    Per-order notional > cap is checked over EVERY order in ``positions``;
    per-market (group) + total exposure caps are checked over the CONCURRENT
    ``open_positions`` (cumulative would overcount closed trades). Read-only."""
    tol = 1e-6
    violations = 0
    for p in positions or []:
        if float(getattr(p, "cost", 0.0) or 0.0) > float(max_order_notional) + tol:
            violations += 1
    concurrent = open_positions if open_positions is not None else (positions or [])
    by_group: dict = {}
    total = 0.0
    for p in concurrent:
        cost = float(getattr(p, "cost", 0.0) or 0.0)
        by_group[getattr(p, "group_key", "") or ""] = \
            by_group.get(getattr(p, "group_key", "") or "", 0.0) + cost
        total += cost
    violations += sum(1 for v in by_group.values() if v > float(max_market_exposure) + tol)
    if total > float(max_total_exposure) + tol:
        violations += 1
    return int(violations)


def cluster_exposure_report(graph, positions: list, *,
                            max_cluster_exposure_usd: float = 50.0) -> dict:
    """Per-correlated-cluster gross/net exposure + the clusters breaching the cap.

    ``graph`` is a MarketDependencyGraph; ``positions`` are
    ``{market_id, notional, side}`` dicts. Same-event exhaustive hedges net down;
    overexposure is flagged on gross. Defensive: missing graph/positions -> empty.
    PAPER-aware, read-only — never sizes/places an order."""
    if graph is None or not positions:
        return {"clusters": {}, "overexposed": [],
                "max_cluster_exposure_usd": max_cluster_exposure_usd}
    try:
        from engine.training.dependency_graph import ClusterExposureNetter
        netter = ClusterExposureNetter(graph, max_cluster_exposure_usd=max_cluster_exposure_usd)
        return {"clusters": netter.cluster_exposures(positions),
                "overexposed": netter.overexposed(positions),
                "max_cluster_exposure_usd": max_cluster_exposure_usd}
    except Exception:  # noqa: BLE001 — netting must never break risk evaluation
        return {"clusters": {}, "overexposed": [],
                "max_cluster_exposure_usd": max_cluster_exposure_usd}


def capital_allocation_constraint_check(*, notional: float, constraints,
                                        market_exposure: float = 0.0,
                                        event_exposure: float = 0.0,
                                        cluster_exposure: float = 0.0,
                                        strategy_exposure: float = 0.0,
                                        open_capital_lock: float = 0.0,
                                        day_pnl: float = 0.0) -> tuple:
    """Additive capital-allocation exposure check (market / event / correlated
    cluster / strategy / daily loss / open capital lock). This NEVER relaxes the
    mandatory RiskEngine gate — it only ever ADDS a tighter portfolio constraint
    on top, for the adaptive capital allocator. Read-only; returns ``(ok, reason)``."""
    from engine.training.capital_allocator import check_portfolio_constraints
    return check_portfolio_constraints(
        notional=notional, constraints=constraints, market_exposure=market_exposure,
        event_exposure=event_exposure, cluster_exposure=cluster_exposure,
        strategy_exposure=strategy_exposure, open_capital_lock=open_capital_lock,
        day_pnl=day_pnl)


def canary_caps_check(*, notional: float, caps=None, **exposures) -> tuple:
    """Additive micro-live CANARY capital-cap check (tiny notional, orders/day,
    daily loss, open/event/strategy exposure, Bregman bundle capital lock). This
    only ever TIGHTENS the live-execution control surface and is inert unless the
    (default-disabled) canary framework is engaged. Returns ``(ok, reason)``."""
    from engine.micro_live.canary import CanaryCaps
    caps = caps or CanaryCaps()
    return caps.check(notional=notional, **exposures)
