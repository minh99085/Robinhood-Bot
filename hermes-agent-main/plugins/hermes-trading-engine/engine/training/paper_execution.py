"""PaperExecutionPolicy — the single source of truth for paper-fill realism.

Pass-3 quant scope — *Execution Realism* + *Compliance/Security*: a paper trade
may only count as REAL executable edge if it could plausibly fill from the LIVE
Polymarket book. This centralizes the realism classification used by BOTH the
directional paper-execution path and the Bregman/ABCAS bundle path, so no
strategy invents its own rules.

A candidate is classified into exactly one outcome:

* ``EXECUTABLE`` — realistic, may count toward exploit/Bregman/readiness PnL.
* ``SHADOW``     — interesting but not live-executable; logged + scored, never
  counts as realized paper PnL.
* ``REJECT``     — fails a hard gate (closed/resolved/offline-stub); not opened.

The decision carries the conservative after-cost economics (tick-up + slippage +
fee + half-spread drag) so optimistic-only edge is exposed. PAPER ONLY — this
module never sizes for live, signs, or submits an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# -- outcome modes -----------------------------------------------------------
EXECUTABLE = "executable"
SHADOW = "shadow"
REJECT = "reject"

# -- execution_realism_status vocabulary (stamped on every paper leg/position) -
STATUS_REALISTIC = "realistic_executable"
STATUS_SHADOW_REFERENCE = "shadow_only_reference_price"
STATUS_SHADOW_STALE = "shadow_only_stale_book"
STATUS_SHADOW_MISSING_ASK = "shadow_only_missing_ask"
STATUS_SHADOW_THIN_DEPTH = "shadow_only_thin_depth"
STATUS_SHADOW_WIDE_SPREAD = "shadow_only_wide_spread"
STATUS_SHADOW_AMBIGUOUS = "shadow_only_ambiguous_settlement"
STATUS_REJECTED = "rejected"

# -- fill sources ------------------------------------------------------------
SRC_LIVE_CLOB = "live_clob"
SRC_REFERENCE = "reference_price"
SRC_OFFLINE_STUB = "offline_stub"
SRC_CACHED_BOOK = "cached_book"


@dataclass
class PaperExecutionContext:
    """Everything the policy needs to judge a single executable leg/trade."""
    fill_source: str = SRC_LIVE_CLOB
    ask: Optional[float] = None
    bid: Optional[float] = None
    spread: Optional[float] = None
    depth_usd: float = 0.0
    book_age_sec: Optional[float] = None
    fresh_book: bool = True
    ambiguity_score: float = 0.0
    resolved: bool = False
    accepting_orders: bool = True
    notional_usd: float = 0.0
    tick_size: float = 0.0
    gross_edge: Optional[float] = None     # directional only; None for bundle legs
    is_bregman_leg: bool = False


@dataclass
class PaperExecutionDecision:
    mode: str                              # EXECUTABLE | SHADOW | REJECT
    reason: str
    execution_realism_status: str
    fill_price: Optional[float] = None
    max_size: float = 0.0
    depth_at_price: float = 0.0
    spread: float = 0.0
    book_age_sec: Optional[float] = None
    slippage_estimate: float = 0.0
    fee_estimate: float = 0.0
    tick_rounding_drag: float = 0.0
    half_spread_drag: float = 0.0
    after_cost_edge: Optional[float] = None
    after_cost_roi: Optional[float] = None
    fill_source: str = SRC_LIVE_CLOB
    book_source: str = SRC_LIVE_CLOB
    price_source: str = SRC_LIVE_CLOB
    was_reference_price_fill: bool = False
    was_fallback_fill: bool = False
    was_offline_stub_fill: bool = False
    fill_quality: float = 0.0
    would_be_executable_if: str = ""
    failure_modes: list = field(default_factory=list)

    @property
    def allow_executable_trade(self) -> bool:
        return self.mode == EXECUTABLE

    @property
    def allow_shadow_only(self) -> bool:
        return self.mode == SHADOW

    @property
    def reject(self) -> bool:
        return self.mode == REJECT

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["allow_executable_trade"] = self.allow_executable_trade
        d["allow_shadow_only"] = self.allow_shadow_only
        d["rejected"] = self.reject
        return d


class PaperExecutionPolicy:
    """Centralized realism gate. Reuses the existing TrainingConfig thresholds
    (``max_spread``, ``min_depth_at_price``, ``max_ambiguity_score``,
    ``reject_on_stale_book``, ``allow_pm_reference_price_fills`` ...) plus the
    Pass-3 strict flags. Deterministic + pure (no I/O, never trades)."""

    def __init__(self, cfg, *, bregman: bool = False):
        self.cfg = cfg
        self.bregman = bool(bregman)
        g = lambda n, d: float(getattr(cfg, n, d))  # noqa: E731
        b = lambda n, d: bool(getattr(cfg, n, d))   # noqa: E731
        if bregman:
            self.max_spread = g("max_spread", 0.08)
            self.min_depth_usd = g("min_depth_at_price", 25.0)
            self.max_ambiguity = g("max_ambiguity_score", 0.45)
            self.max_book_age_sec = g("bregman_max_book_age_sec", 20.0)
            self.allow_reference = b("bregman_allow_reference_fills", False)
        else:
            self.max_spread = g("max_spread", 0.08)
            self.min_depth_usd = g("min_depth_at_price", 25.0)
            self.max_ambiguity = g("max_ambiguity_score", 0.45)
            self.max_book_age_sec = g("max_book_age_sec", 20.0)
            self.allow_reference = b("allow_pm_reference_price_fills", False)
        self.reject_on_stale = b("reject_on_stale_book", True)
        self.require_ask = b("require_executable_ask", True)
        self.reject_missing_ask = b("reject_missing_ask", True)
        self.reject_offline_stub = b("reject_offline_stub_fills", True)
        self.slippage_bps = g("slippage_bps", 25.0)
        self.fee_bps = g("taker_fee_bps", 0.0)

    # -- after-cost economics (conservative; rounds against us) --------------
    def _after_cost(self, ctx: PaperExecutionContext) -> dict:
        from engine.execution.slippage import drag_breakdown
        ask = float(ctx.ask or 0.0)
        b = drag_breakdown(ask, ctx.bid, ctx.tick_size,
                           slippage_bps=self.slippage_bps, fee_bps=self.fee_bps)
        exec_price = float(b["exec_price"])
        drag = (float(b["tick_rounding"]) + float(b["slippage"])
                + float(b["fee"]) + float(b["half_spread"]))
        after_cost_edge = None
        after_cost_roi = None
        if ctx.gross_edge is not None:
            after_cost_edge = round(float(ctx.gross_edge) - drag, 6)
            after_cost_roi = round(after_cost_edge / exec_price, 6) if exec_price > 0 else 0.0
        return {"exec_price": exec_price, "drag": b, "total_drag": round(drag, 6),
                "after_cost_edge": after_cost_edge, "after_cost_roi": after_cost_roi}

    def _decision(self, ctx, mode, reason, status, *, ac, would_if="") -> PaperExecutionDecision:
        src = ctx.fill_source
        return PaperExecutionDecision(
            mode=mode, reason=reason, execution_realism_status=status,
            fill_price=(round(ac["exec_price"], 6) if mode == EXECUTABLE else None),
            max_size=(float(ctx.depth_usd) if mode == EXECUTABLE else 0.0),
            depth_at_price=float(ctx.depth_usd or 0.0),
            spread=float(ctx.spread or 0.0),
            book_age_sec=ctx.book_age_sec,
            slippage_estimate=float(ac["drag"]["slippage"]),
            fee_estimate=float(ac["drag"]["fee"]),
            tick_rounding_drag=float(ac["drag"]["tick_rounding"]),
            half_spread_drag=float(ac["drag"]["half_spread"]),
            after_cost_edge=ac["after_cost_edge"], after_cost_roi=ac["after_cost_roi"],
            fill_source=src, book_source=src, price_source=src,
            was_reference_price_fill=(src == SRC_REFERENCE),
            was_fallback_fill=(src in (SRC_REFERENCE, SRC_OFFLINE_STUB)),
            was_offline_stub_fill=(src == SRC_OFFLINE_STUB),
            fill_quality=(1.0 if mode == EXECUTABLE else 0.0),
            would_be_executable_if=would_if, failure_modes=[reason] if reason else [])

    def evaluate(self, ctx: PaperExecutionContext) -> PaperExecutionDecision:
        ac = self._after_cost(ctx)

        # --- hard rejects (never open, not even shadow-tradeable) ---
        if ctx.resolved or not ctx.accepting_orders:
            return self._decision(ctx, REJECT, "market_closed_or_resolved",
                                  STATUS_REJECTED, ac=ac)
        if ctx.fill_source == SRC_OFFLINE_STUB and self.reject_offline_stub:
            return self._decision(ctx, REJECT, "offline_stub_fill_disallowed",
                                  STATUS_REJECTED, ac=ac,
                                  would_if="a live CLOB book replaces the offline stub")

        # --- realism downgrades to SHADOW (loggable, never counts as PnL) ---
        if ctx.fill_source == SRC_REFERENCE and not self.allow_reference:
            return self._decision(ctx, SHADOW, "reference_fill_disallowed",
                                  STATUS_SHADOW_REFERENCE, ac=ac,
                                  would_if="a real best-ask exists on the live book")
        if ctx.ask is None or float(ctx.ask) <= 0.0:
            if self.require_ask or self.reject_missing_ask:
                return self._decision(ctx, SHADOW, "missing_executable_ask",
                                      STATUS_SHADOW_MISSING_ASK, ac=ac,
                                      would_if="a real best-ask is quoted")
        stale = ((not ctx.fresh_book)
                 or (ctx.book_age_sec is not None
                     and float(ctx.book_age_sec) > self.max_book_age_sec))
        if stale and self.reject_on_stale:
            return self._decision(ctx, SHADOW, "stale_book", STATUS_SHADOW_STALE, ac=ac,
                                  would_if=f"book age <= {self.max_book_age_sec:g}s")
        if float(ctx.depth_usd or 0.0) < self.min_depth_usd:
            return self._decision(ctx, SHADOW, "thin_depth", STATUS_SHADOW_THIN_DEPTH, ac=ac,
                                  would_if=f"depth >= ${self.min_depth_usd:g}")
        if ctx.spread is not None and float(ctx.spread) > self.max_spread:
            return self._decision(ctx, SHADOW, "wide_spread", STATUS_SHADOW_WIDE_SPREAD, ac=ac,
                                  would_if=f"spread <= {self.max_spread:g}")
        if float(ctx.ambiguity_score or 0.0) > self.max_ambiguity:
            return self._decision(ctx, SHADOW, "ambiguous_settlement",
                                  STATUS_SHADOW_AMBIGUOUS, ac=ac,
                                  would_if=f"ambiguity <= {self.max_ambiguity:g}")
        # non-positive after-cost edge (directional only; bundles handled by certifier)
        if ac["after_cost_edge"] is not None and ac["after_cost_edge"] <= 0.0:
            return self._decision(ctx, REJECT, "negative_after_cost", STATUS_REJECTED, ac=ac,
                                  would_if="gross edge exceeds spread+slippage+fee+tick drag")

        return self._decision(ctx, EXECUTABLE, "", STATUS_REALISTIC, ac=ac)


# -- Bregman per-leg reject-reason mapping (all-or-nothing bundle) -----------
# Maps the centralized realism reason onto the explicit bregman_leg_* reasons
# required by Pass-3 so a failing leg rejects the WHOLE bundle.
BREGMAN_LEG_REASON = {
    "missing_executable_ask": "bregman_leg_missing_ask",
    "stale_book": "bregman_leg_stale_book",
    "wide_spread": "bregman_leg_wide_spread",
    "thin_depth": "bregman_leg_thin_depth",
    "ambiguous_settlement": "bregman_leg_ambiguous",
    "negative_after_cost": "bregman_negative_after_cost",
    "reference_fill_disallowed": "bregman_reference_fill_disallowed",
    "offline_stub_fill_disallowed": "bregman_reference_fill_disallowed",
    "market_closed_or_resolved": "bregman_leg_stale_book",
}


def bregman_leg_reason(realism_reason: str) -> str:
    """Translate a PaperExecutionPolicy reason to the Bregman bundle reason."""
    return BREGMAN_LEG_REASON.get(realism_reason, "bregman_incomplete_executable_set")
