"""Profitability truth governor (PAPER ONLY — verdicts + memory, never trades).

Shifts the bot from high trade COUNT to high expected NET profitability: it stops
trading markets where edge does not survive costs, timing, or settlement risk.

Quant scope (documented end-to-end):

* **Data Acquisition & Ingestion / Preprocessing & Feature Engineering** — the
  graylist/blacklist memory ingests per-market outcome quality (after-cost
  expectancy, fill quality, spread drag, settlement ambiguity, label quality).
* **Statistical & Probabilistic Modeling** — :class:`EdgeDecayModel` models how
  fast a gross edge decays by strategy, market type, liquidity, spread,
  time-to-resolution, and execution delay (with an explicit edge half-life).
* **Signal Generation & Strategy Development (Bregman priority)** — Bregman edge
  decays slower than directional; the after-cost score ranks markets by net edge.
* **Risk Management & Portfolio Optimization** — :func:`after_cost_edge` nets the
  gross edge against fees, spread, slippage, fill failure, adverse selection,
  label ambiguity, and timing decay; the timing decision gates trade/wait/skip.
* **Backtesting & Simulation / Strategy Optimization & Robustness Testing** —
  :func:`profitability_truth_report` decomposes realized edge into every cost
  bucket so a backtest's "profit" can be audited.
* **CLOB v2 Execution** — execution delay + fill quality feed the decay + memory.
* **Live Trading & Monitoring / Compliance/Security** — a market is only
  ``live_ready`` when it is clean (not graylisted/blacklisted) AND its after-cost
  edge survives; aggressive paper may still explore a graylisted market with a
  tiny LABELED exploration size, but live-readiness blocks it. Never trades.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

STATE_CLEAN = "clean"
STATE_GRAYLIST = "graylist"
STATE_BLACKLIST = "blacklist"

# cost-component keys produced by EdgeEngine that map onto truth-report buckets.
_FEE_KEYS = ("fee",)
_SPREAD_KEYS = ("spread",)
_SLIPPAGE_KEYS = ("slippage",)
_AMBIGUITY_KEYS = ("ambiguity",)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _sum(d: dict, keys) -> float:
    return sum(float(d.get(k, 0.0) or 0.0) for k in keys)


# --------------------------------------------------------------------------- #
# after-cost edge + profitability score
# --------------------------------------------------------------------------- #
def after_cost_edge(gross_edge: float, cost_components: Optional[dict] = None, *,
                    fill_failure: float = 0.0, adverse_selection: float = 0.0,
                    timing_decay: float = 0.0) -> dict:
    """Net a gross edge against every real-world cost bucket.

    ``cost_components`` is the EdgeEngine cost dict (fee / spread / slippage /
    ambiguity / stale / evidence / calibration / liquidity). The explicit
    ``fill_failure`` / ``adverse_selection`` / ``timing_decay`` terms are the
    CLOB-v2 + timing costs the pre-trade edge math does not see. Returns the
    decomposition + ``net_edge = gross − total_cost``."""
    c = cost_components or {}
    fees = _sum(c, _FEE_KEYS)
    spread = _sum(c, _SPREAD_KEYS)
    slippage = _sum(c, _SLIPPAGE_KEYS)
    label_ambiguity = _sum(c, _AMBIGUITY_KEYS)
    # any other cost keys (stale/evidence/calibration/liquidity) roll into "other"
    other = sum(float(v or 0.0) for k, v in c.items()
                if k not in (*_FEE_KEYS, *_SPREAD_KEYS, *_SLIPPAGE_KEYS, *_AMBIGUITY_KEYS))
    ff = max(0.0, float(fill_failure))
    adv = max(0.0, float(adverse_selection))
    td = max(0.0, float(timing_decay))
    total = fees + spread + slippage + label_ambiguity + other + ff + adv + td
    return {
        "gross": round(float(gross_edge), 8),
        "fees": round(fees, 8), "spread": round(spread, 8), "slippage": round(slippage, 8),
        "fill_failure": round(ff, 8), "adverse_selection": round(adv, 8),
        "label_ambiguity": round(label_ambiguity, 8), "timing_decay": round(td, 8),
        "other_cost": round(other, 8), "total_cost": round(total, 8),
        "net_edge": round(float(gross_edge) - total, 8),
    }


def profitability_score(net_edge: float, *, scale: float = 0.02) -> float:
    """Logistic after-cost profitability score in ``[0, 1]`` (0.5 at net=0).

    A positive net edge scores above 0.5 and saturates toward 1; a negative net
    edge scores below 0.5. ``scale`` is the net-edge unit (~2% default)."""
    z = float(net_edge) / max(1e-9, float(scale))
    return round(1.0 / (1.0 + math.exp(-z)), 6)


def after_cost_profitability_score(*, gross_edge: float,
                                   cost_components: Optional[dict] = None,
                                   fill_failure: float = 0.0,
                                   adverse_selection: float = 0.0,
                                   timing_decay: float = 0.0, scale: float = 0.02) -> float:
    """Convenience: after-cost net edge -> profitability score in ``[0, 1]``."""
    ac = after_cost_edge(gross_edge, cost_components, fill_failure=fill_failure,
                         adverse_selection=adverse_selection, timing_decay=timing_decay)
    return profitability_score(ac["net_edge"], scale=scale)


# --------------------------------------------------------------------------- #
# edge decay model
# --------------------------------------------------------------------------- #
# Per-strategy base half-life (seconds): a certified Bregman hedge persists longer
# than a directional view that the market is racing to price in.
_STRATEGY_HALF_LIFE = {
    "bregman": 3600.0, "bregman_arbitrage": 3600.0,
    "statistical_mispricing": 1800.0, "statistical_edge": 1800.0,
    "directional": 900.0, "directional_edge": 900.0, "chainlink_edge": 1200.0,
}
_MARKET_TYPE_FACTOR = {"binary": 1.0, "range": 0.9, "categorical": 0.85,
                       "scalar": 0.8, "unknown": 0.7}


class EdgeDecayModel:
    """Models gross-edge decay (and an edge half-life) by regime.

    The half-life shrinks with wide spreads, thin liquidity, and short
    time-to-resolution; the decay factor is ``0.5 ** (execution_delay / half_life)``
    — a slower-decaying strategy/regime keeps more of its edge by the time the
    order actually fills. Deterministic + stdlib-only."""

    def __init__(self, *, min_half_life_s: float = 30.0):
        self.min_half_life_s = float(min_half_life_s)

    def edge_half_life(self, *, strategy: str, market_type: str = "binary",
                       liquidity_usd: float = 0.0, spread: float = 0.0,
                       time_to_resolution_s: Optional[float] = None,
                       **_ignore) -> float:
        base = _STRATEGY_HALF_LIFE.get(str(strategy).lower(), 900.0)
        base *= _MARKET_TYPE_FACTOR.get(str(market_type).lower(), 0.7)
        # wide spread -> faster decay (race to fade); tight -> slower
        spread_mult = _clamp01(1.0 - min(1.0, max(0.0, float(spread)) / 0.08)) * 0.9 + 0.1
        # thin liquidity -> faster decay
        liq = max(0.0, float(liquidity_usd or 0.0))
        liq_mult = (min(1.0, math.log1p(liq) / math.log1p(100_000.0)) * 0.9 + 0.1) if liq > 0 else 0.1
        # short time-to-resolution -> faster decay (book locks up near resolve)
        if time_to_resolution_s is None:
            ttr_mult = 1.0
        else:
            ttr_mult = _clamp01(math.log1p(max(0.0, float(time_to_resolution_s)))
                                / math.log1p(7 * 86400.0)) * 0.9 + 0.1
        hl = base * spread_mult * liq_mult * ttr_mult
        return round(max(self.min_half_life_s, hl), 6)

    def decay_factor(self, *, strategy: str, market_type: str = "binary",
                     liquidity_usd: float = 0.0, spread: float = 0.0,
                     time_to_resolution_s: Optional[float] = None,
                     execution_delay_s: float = 0.0) -> float:
        hl = self.edge_half_life(strategy=strategy, market_type=market_type,
                                 liquidity_usd=liquidity_usd, spread=spread,
                                 time_to_resolution_s=time_to_resolution_s)
        delay = max(0.0, float(execution_delay_s))
        return round(_clamp01(0.5 ** (delay / max(1e-9, hl))), 6)

    def decayed_edge(self, gross_edge: float, **kw) -> float:
        return round(float(gross_edge) * self.decay_factor(**kw), 8)

    def timing_decay_cost(self, gross_edge: float, **kw) -> float:
        """The edge lost to timing decay (gross − decayed); >= 0."""
        return round(max(0.0, float(gross_edge) - self.decayed_edge(gross_edge, **kw)), 8)


# --------------------------------------------------------------------------- #
# graylist / blacklist memory
# --------------------------------------------------------------------------- #
class MarketQualityMemory:
    """Per-market strike memory: repeated bad after-cost behaviour graylists then
    blacklists a market so the bot stops wasting capital on it. A strike is added
    for negative after-cost expectancy, ambiguous settlement, poor fill quality,
    excessive spread drag, or bad labels. Optional JSON persistence."""

    def __init__(self, *, graylist_threshold: int = 3, blacklist_threshold: int = 6,
                 min_fill_quality: float = 0.5, max_spread_drag: float = 0.02,
                 path: Optional[Path] = None):
        self.graylist_threshold = int(graylist_threshold)
        self.blacklist_threshold = int(blacklist_threshold)
        self.min_fill_quality = float(min_fill_quality)
        self.max_spread_drag = float(max_spread_drag)
        self.path = Path(path) if path else None
        self._strikes: dict = {}     # market_id -> int
        self._reasons: dict = {}     # market_id -> {reason: count}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self._strikes = {k: int(v) for k, v in d.get("strikes", {}).items()}
                self._reasons = dict(d.get("reasons", {}))
            except (ValueError, OSError):
                pass

    def persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"strikes": self._strikes, "reasons": self._reasons}, default=str),
                encoding="utf-8")
        except OSError:
            pass

    def record(self, market_id: str, *, after_cost_expectancy: float = 0.0,
               ambiguous: bool = False, fill_quality: float = 1.0,
               spread_drag: float = 0.0, bad_label: bool = False) -> str:
        reasons = []
        if float(after_cost_expectancy) < 0.0:
            reasons.append("negative_after_cost")
        if ambiguous:
            reasons.append("ambiguous_settlement")
        if float(fill_quality) < self.min_fill_quality:
            reasons.append("poor_fill_quality")
        if float(spread_drag) > self.max_spread_drag:
            reasons.append("excessive_spread_drag")
        if bad_label:
            reasons.append("bad_labels")
        if reasons:
            self._strikes[market_id] = self._strikes.get(market_id, 0) + len(reasons)
            rmap = self._reasons.setdefault(market_id, {})
            for r in reasons:
                rmap[r] = rmap.get(r, 0) + 1
        return self.state(market_id)

    def state(self, market_id: str) -> str:
        s = self._strikes.get(market_id, 0)
        if s >= self.blacklist_threshold:
            return STATE_BLACKLIST
        if s >= self.graylist_threshold:
            return STATE_GRAYLIST
        return STATE_CLEAN

    def is_graylisted(self, market_id: str) -> bool:
        return self.state(market_id) in (STATE_GRAYLIST, STATE_BLACKLIST)

    def is_blacklisted(self, market_id: str) -> bool:
        return self.state(market_id) == STATE_BLACKLIST

    def reasons(self, market_id: str) -> list:
        return sorted(self._reasons.get(market_id, {}).keys())

    def to_report(self) -> dict:
        graylisted = [m for m in self._strikes if self.state(m) == STATE_GRAYLIST]
        blacklisted = [m for m in self._strikes if self.state(m) == STATE_BLACKLIST]
        return {"tracked": len(self._strikes), "graylisted": graylisted,
                "blacklisted": blacklisted, "graylist_count": len(graylisted),
                "blacklist_count": len(blacklisted),
                "reasons": {m: self.reasons(m) for m in self._strikes}}


# --------------------------------------------------------------------------- #
# timing decision
# --------------------------------------------------------------------------- #
def timing_decision(*, net_edge: float, decay_factor: float, graylist_state: str,
                    aggressive: bool = False, min_net_edge: float = 0.0,
                    min_decay_factor: float = 0.5) -> str:
    """Decide trade_now / wait / tiny_exploration / skip.

    A blacklisted market is always skipped. A graylisted market is explored only
    in aggressive mode with a tiny labeled size (else skipped). A clean market
    with positive net edge trades now when the edge has not decayed; if it is
    decaying it waits; a non-positive net edge is skipped."""
    if graylist_state == STATE_BLACKLIST:
        return "skip"
    if graylist_state == STATE_GRAYLIST:
        return "tiny_exploration" if aggressive else "skip"
    if float(net_edge) <= float(min_net_edge):
        return "skip"
    if float(decay_factor) < float(min_decay_factor):
        return "wait"
    return "trade_now"


# --------------------------------------------------------------------------- #
# governor + truth report
# --------------------------------------------------------------------------- #
@dataclass
class GovernorVerdict:
    market_id: str
    strategy: str
    after_cost: dict
    profitability_score: float
    decay_factor: float
    edge_half_life: float
    decayed_edge: float
    graylist_state: str
    timing: str
    tradeable: bool
    live_ready: bool
    exploration_label: str = ""
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"market_id": self.market_id, "strategy": self.strategy,
                "after_cost": self.after_cost,
                "profitability_score": self.profitability_score,
                "decay_factor": self.decay_factor, "edge_half_life": self.edge_half_life,
                "decayed_edge": self.decayed_edge, "graylist_state": self.graylist_state,
                "timing": self.timing, "tradeable": self.tradeable,
                "live_ready": self.live_ready, "exploration_label": self.exploration_label,
                "reasons": list(self.reasons)}


class ProfitabilityGovernor:
    """Decides, per candidate, whether the edge survives costs + timing + market
    quality — and whether the market is live-ready. PAPER ONLY (verdicts only)."""

    def __init__(self, *, memory: Optional[MarketQualityMemory] = None,
                 decay: Optional[EdgeDecayModel] = None,
                 min_net_edge: float = 0.0, min_decay_factor: float = 0.5):
        self.memory = memory or MarketQualityMemory()
        self.decay = decay or EdgeDecayModel()
        self.min_net_edge = float(min_net_edge)
        self.min_decay_factor = float(min_decay_factor)

    def evaluate(self, *, market_id: str, strategy: str, gross_edge: float,
                 cost_components: Optional[dict] = None, liquidity_usd: float = 0.0,
                 spread: float = 0.0, market_type: str = "binary",
                 time_to_resolution_s: Optional[float] = None,
                 execution_delay_s: float = 1.0, fill_failure: float = 0.0,
                 adverse_selection: float = 0.0, aggressive: bool = False
                 ) -> GovernorVerdict:
        decay_kw = dict(strategy=strategy, market_type=market_type,
                        liquidity_usd=liquidity_usd, spread=spread,
                        time_to_resolution_s=time_to_resolution_s,
                        execution_delay_s=execution_delay_s)
        decay_f = self.decay.decay_factor(**decay_kw)
        half_life = self.decay.edge_half_life(**decay_kw)
        timing_cost = self.decay.timing_decay_cost(gross_edge, **decay_kw)
        ac = after_cost_edge(gross_edge, cost_components, fill_failure=fill_failure,
                             adverse_selection=adverse_selection, timing_decay=timing_cost)
        state = self.memory.state(market_id)
        timing = timing_decision(net_edge=ac["net_edge"], decay_factor=decay_f,
                                 graylist_state=state, aggressive=aggressive,
                                 min_net_edge=self.min_net_edge,
                                 min_decay_factor=self.min_decay_factor)
        reasons: list = []
        if ac["net_edge"] <= self.min_net_edge:
            reasons.append("net_edge_not_positive")
        if state != STATE_CLEAN:
            reasons.append(f"market_{state}")
        if decay_f < self.min_decay_factor:
            reasons.append("edge_decaying")
        # live-ready ONLY when clean + positive after-cost edge + edge survives delay
        live_ready = (state == STATE_CLEAN and ac["net_edge"] > self.min_net_edge
                      and self.decayed_edge_positive(ac, decay_f))
        tradeable = timing in ("trade_now", "tiny_exploration")
        return GovernorVerdict(
            market_id=market_id, strategy=strategy, after_cost=ac,
            profitability_score=profitability_score(ac["net_edge"]),
            decay_factor=decay_f, edge_half_life=half_life,
            decayed_edge=self.decay.decayed_edge(gross_edge, **decay_kw),
            graylist_state=state, timing=timing, tradeable=tradeable,
            live_ready=bool(live_ready),
            exploration_label=("graylisted_market" if timing == "tiny_exploration" else ""),
            reasons=reasons)

    @staticmethod
    def decayed_edge_positive(after_cost: dict, decay_factor: float) -> bool:
        # net edge must remain positive after the timing decay already netted in.
        return after_cost["net_edge"] > 0.0 and decay_factor > 0.0


def profitability_truth_report(trades: list) -> dict:
    """Aggregate a truth report separating gross edge from each cost bucket.

    Each trade dict carries ``gross_edge``, ``cost_components`` (EdgeEngine dict),
    and optional ``fill_failure`` / ``adverse_selection`` / ``timing_decay``.
    Returns the summed decomposition + ``net_edge``. Backtesting/robustness audit
    so a strategy's "profit" can be attributed to (or blamed on) each cost."""
    keys = ("gross", "fees", "spread", "slippage", "fill_failure",
            "adverse_selection", "label_ambiguity", "timing_decay", "other_cost",
            "total_cost", "net_edge")
    agg = {k: 0.0 for k in keys}
    n = 0
    for t in trades or []:
        ac = after_cost_edge(
            float(t.get("gross_edge", 0.0)), t.get("cost_components") or {},
            fill_failure=float(t.get("fill_failure", 0.0)),
            adverse_selection=float(t.get("adverse_selection", 0.0)),
            timing_decay=float(t.get("timing_decay", 0.0)))
        for k in keys:
            agg[k] += ac[k]
        n += 1
    out = {("gross_edge" if k == "gross" else k): round(v, 8) for k, v in agg.items()}
    out["n"] = n
    out["edge_survival"] = round(out["net_edge"] / out["gross_edge"], 6) \
        if out["gross_edge"] > 1e-12 else 0.0
    return out
