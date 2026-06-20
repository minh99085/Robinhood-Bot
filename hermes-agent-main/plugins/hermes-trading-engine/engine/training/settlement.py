"""Settlement Truth Engine — clean training labels for the Polymarket paper bot.

Aggressive paper trading produces many closed positions, but a position's
exit-mark P&L is NOT a clean training label: a market can be voided, resolve
ambiguously, settle late (stale), or partially invalidate. Training the
probability stack / calibrator / Bregman validator on those *dirty* labels
silently corrupts everything downstream.

This module is the single source of *settlement truth*. It maps a raw venue
resolution observation into one explicit label state with a confidence and a
settlement-source, and exposes the realized 0/1 outcome for a predicted side.
Only ``resolved_yes`` / ``resolved_no`` labels are *trainable*; every other state
is suppressed by the learner / calibrator / feedback loop.

Quant scope documented here (this engine is the join point for all of them):

* Data Acquisition & Ingestion — consumes venue resolution observations /
  replayed ``market_resolved`` events (offline, no network).
* Data Preprocessing & Feature Engineering — normalizes heterogeneous venue
  resolution payloads into one ``SettlementLabel`` shape.
* Statistical & Probabilistic Modeling — supplies the (predicted, realized)
  pairs the calibrator fits; dirty pairs are excluded so calibration is clean.
* Signal Generation — label confidence + ambiguity feed selection/risk gates.
* Bregman arbitrage validation — :class:`BregmanSettlementValidator` checks the
  certified profit lower bound against the *settled* multi-leg payout.
* Risk Management — void/ambiguous/stale never train; conservative by default.
* Backtesting & Simulation — replay calibration uses only clean labels.
* Strategy Optimization & Robustness Testing — label-quality metrics surface
  coverage / delay / ambiguous-rate / suppression for reports.
* CLOB v2 execution simulation — leg settle prices (0/1) close the paper book.
* Live Trading & Monitoring — label coverage / delay are monitored, never an
  execution path. PAPER ONLY: this module never sizes, approves, arms, or places
  an order.
* Compliance / Security / Operational Excellence — deterministic, offline, no
  secrets; every label records its settlement source for auditability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


class LabelState:
    """Explicit settlement-label states. Only TRAINABLE states may update the
    probability stack / calibrator / learner."""

    UNRESOLVED = "unresolved"
    RESOLVED_YES = "resolved_yes"
    RESOLVED_NO = "resolved_no"
    VOID = "void"
    AMBIGUOUS = "ambiguous"
    PARTIALLY_INVALID = "partially_invalid"
    STALE_RESOLUTION = "stale_resolution"

    ALL = (UNRESOLVED, RESOLVED_YES, RESOLVED_NO, VOID, AMBIGUOUS,
           PARTIALLY_INVALID, STALE_RESOLUTION)
    # Clean, usable-for-learning labels (the market settled to a known side).
    TRAINABLE = frozenset({RESOLVED_YES, RESOLVED_NO})
    # Terminal = a settlement decision was reached (anything but unresolved).
    TERMINAL = frozenset({RESOLVED_YES, RESOLVED_NO, VOID, AMBIGUOUS,
                          PARTIALLY_INVALID, STALE_RESOLUTION})
    # Dirty = terminal but NOT trainable (must be suppressed).
    DIRTY = frozenset({VOID, AMBIGUOUS, PARTIALLY_INVALID, STALE_RESOLUTION})


def is_trainable_state(state: Optional[str]) -> bool:
    """True only for ``resolved_yes`` / ``resolved_no``. ``None`` is treated as
    a legacy clean label (back-compat for callers that pass no settlement)."""
    if state is None:
        return True
    return state in LabelState.TRAINABLE


# Public settlement-source reliability weights (NOT secrets). A weaker source
# lowers confidence, which can demote an otherwise-clean label to ambiguous.
_SOURCE_WEIGHTS = {
    "uma": 1.0, "optimistic_oracle": 1.0, "official": 1.0, "chainlink": 0.98,
    "polymarket": 0.95, "clob": 0.95, "gamma": 0.9, "manual": 0.6,
    "paper_mark": 0.8, "heuristic": 0.6, "unknown": 0.5, "": 0.5,
}


def source_weight(source: Optional[str]) -> float:
    return _SOURCE_WEIGHTS.get((source or "unknown").lower(), 0.5)


def _norm_side(side: Optional[str]) -> str:
    s = (side or "").strip().upper()
    if s in ("YES", "UP", "BUY", "LONG", "TRUE", "1"):
        return "YES"
    if s in ("NO", "DOWN", "SELL", "SHORT", "FALSE", "0"):
        return "NO"
    return s


@dataclass
class SettlementLabel:
    """One market's settlement truth + provenance.

    ``state`` is the canonical :class:`LabelState`. ``winning_outcome`` is the
    side the market resolved to (``YES`` / ``NO`` / a token id). ``confidence``
    in [0,1] combines settlement-source reliability with (1 - ambiguity)."""

    market_id: str
    state: str = LabelState.UNRESOLVED
    confidence: float = 0.0
    source: str = "unknown"
    asset_id: Optional[str] = None
    winning_outcome: Optional[str] = None
    resolved_ts_ms: Optional[int] = None
    observed_ts_ms: Optional[int] = None
    close_ts_ms: Optional[int] = None
    delay_ms: Optional[int] = None
    ambiguity_score: float = 0.0
    reasons: list = field(default_factory=list)

    @property
    def trainable(self) -> bool:
        return self.state in LabelState.TRAINABLE

    def realized_for(self, side: Optional[str]) -> Optional[int]:
        """Realized 0/1 label for a *predicted* side. ``None`` when the market
        did not settle to a clean side (so it cannot become a training pair)."""
        if self.state not in LabelState.TRAINABLE:
            return None
        market_yes = self.state == LabelState.RESOLVED_YES
        s = _norm_side(side)
        if s == "YES":
            return 1 if market_yes else 0
        if s == "NO":
            return 0 if market_yes else 1
        # token-id / explicit-outcome match against the winning outcome
        if self.winning_outcome is not None and side is not None:
            return 1 if str(side) == str(self.winning_outcome) else 0
        return 1 if market_yes else 0

    def settle_price_for(self, side: Optional[str]) -> Optional[float]:
        """Binary settle price (1.0/0.0) for a side, or ``None`` if not clean."""
        r = self.realized_for(side)
        return None if r is None else float(r)

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id, "asset_id": self.asset_id,
            "state": self.state, "trainable": self.trainable,
            "confidence": round(float(self.confidence), 4), "source": self.source,
            "winning_outcome": self.winning_outcome,
            "resolved_ts_ms": self.resolved_ts_ms, "observed_ts_ms": self.observed_ts_ms,
            "close_ts_ms": self.close_ts_ms, "delay_ms": self.delay_ms,
            "ambiguity_score": round(float(self.ambiguity_score), 4),
            "reasons": list(self.reasons),
        }


class SettlementTruthEngine:
    """Deterministic classifier: raw resolution observation -> SettlementLabel.

    Conservative by construction (Risk Management): anything not provably clean
    becomes a non-trainable state. No network, no LLM, no side effects.
    """

    def __init__(self, *, ambiguity_threshold: float = 0.5,
                 min_confidence: float = 0.4,
                 max_resolution_delay_ms: int = 14 * 24 * 3600 * 1000):
        self.ambiguity_threshold = float(ambiguity_threshold)
        self.min_confidence = float(min_confidence)
        self.max_resolution_delay_ms = int(max_resolution_delay_ms)

    def classify(self, obs: dict, *, now_ms: Optional[int] = None) -> SettlementLabel:
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        market_id = str(obs.get("market_id") or obs.get("id") or "")
        asset_id = obs.get("asset_id")
        source = str(obs.get("settlement_source") or obs.get("source") or "unknown")
        amb = float(obs.get("ambiguity_score") or 0.0)
        resolved = bool(obs.get("resolved", obs.get("winning_outcome") is not None
                                 or obs.get("voided") or obs.get("invalid")))
        voided = bool(obs.get("voided") or obs.get("invalid"))
        partial = bool(obs.get("partial") or obs.get("partially_invalid"))
        stale_flag = bool(obs.get("stale"))
        winner = obs.get("winning_outcome")
        close_ts = obs.get("close_ts_ms")
        resolved_ts = obs.get("resolved_ts_ms")

        delay_ms = None
        if close_ts is not None and resolved_ts is not None:
            try:
                delay_ms = int(resolved_ts) - int(close_ts)
            except (TypeError, ValueError):
                delay_ms = None

        conf = round(source_weight(source) * max(0.0, 1.0 - amb), 4)
        reasons: list = []

        def _label(state: str, c: float) -> SettlementLabel:
            return SettlementLabel(
                market_id=market_id, asset_id=asset_id, state=state,
                confidence=round(max(0.0, min(1.0, c)), 4), source=source,
                winning_outcome=winner, resolved_ts_ms=resolved_ts,
                observed_ts_ms=now_ms, close_ts_ms=close_ts, delay_ms=delay_ms,
                ambiguity_score=round(amb, 4), reasons=reasons)

        if voided:
            reasons.append("voided_or_invalid")
            return _label(LabelState.VOID, conf)
        if not resolved or winner is None:
            reasons.append("no_resolution")
            return _label(LabelState.UNRESOLVED, 0.0)

        # Resolved with a winner — now screen for dirtiness (conservative order).
        stale = stale_flag or (delay_ms is not None and delay_ms > self.max_resolution_delay_ms)
        if stale:
            reasons.append("stale_resolution")
            return _label(LabelState.STALE_RESOLUTION, conf)
        if partial:
            reasons.append("partially_invalid")
            return _label(LabelState.PARTIALLY_INVALID, conf)
        if amb >= self.ambiguity_threshold:
            reasons.append("ambiguity_above_threshold")
            return _label(LabelState.AMBIGUOUS, conf)
        if conf < self.min_confidence:
            reasons.append("confidence_below_minimum")
            return _label(LabelState.AMBIGUOUS, conf)

        # Clean settlement.
        state = LabelState.RESOLVED_YES if _norm_side(str(winner)) == "YES" \
            else LabelState.RESOLVED_NO
        return _label(state, conf)


@dataclass
class GroupSettlementResult:
    """Outcome of validating a certified Bregman group against settlement truth."""

    group_id: str
    state: str                       # validated | unresolved | partially_invalid | violated
    valid: bool
    all_legs_clean: bool
    leg_states: dict
    realized_payout: Optional[float]
    expected_lower_bound: Optional[float]
    margin: Optional[float]
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id, "state": self.state, "valid": self.valid,
            "all_legs_clean": self.all_legs_clean, "leg_states": dict(self.leg_states),
            "realized_payout": self.realized_payout,
            "expected_lower_bound": self.expected_lower_bound, "margin": self.margin,
            "reasons": list(self.reasons),
        }


class BregmanSettlementValidator:
    """Validate a certified Bregman opportunity against *settled* leg outcomes.

    A hedged group's certified profit lower bound is only meaningful once every
    leg settles cleanly. We require ALL legs to carry a trainable label, then
    compute the realized payout (each binary leg settles to 0/1) and confirm it
    cleared the certified lower bound. Any unresolved leg => cannot validate;
    any dirty leg => ``partially_invalid`` (excluded from learning)."""

    def __init__(self, *, tolerance: float = 1e-6):
        self.tolerance = float(tolerance)

    def validate(self, *, group_id: str, legs: list, labels: dict,
                 certified_profit_lower_bound: float,
                 now_ms: Optional[int] = None) -> GroupSettlementResult:
        leg_states: dict = {}
        reasons: list = []
        any_unresolved = False
        any_dirty = False

        for leg in legs:
            mid = str(leg.get("market_id"))
            lab = labels.get(mid)
            if lab is None:
                leg_states[mid] = LabelState.UNRESOLVED
                any_unresolved = True
                reasons.append(f"leg {mid}: missing label")
                continue
            leg_states[mid] = lab.state
            if lab.state == LabelState.UNRESOLVED:
                any_unresolved = True
                reasons.append(f"leg {mid}: unresolved")
            elif lab.state in LabelState.DIRTY:
                any_dirty = True
                reasons.append(f"leg {mid}: {lab.state}")

        if any_unresolved:
            return GroupSettlementResult(
                group_id=group_id, state="unresolved", valid=False,
                all_legs_clean=False, leg_states=leg_states, realized_payout=None,
                expected_lower_bound=certified_profit_lower_bound, margin=None,
                reasons=reasons or ["group has unresolved legs"])
        if any_dirty:
            return GroupSettlementResult(
                group_id=group_id, state="partially_invalid", valid=False,
                all_legs_clean=False, leg_states=leg_states, realized_payout=None,
                expected_lower_bound=certified_profit_lower_bound, margin=None,
                reasons=reasons or ["group has dirty legs"])

        # All legs clean: realized payout = sum (settle_price - entry) * qty.
        payout = 0.0
        for leg in legs:
            mid = str(leg.get("market_id"))
            lab = labels[mid]
            side = leg.get("side") or leg.get("outcome")
            settle = lab.settle_price_for(side)
            if settle is None:  # defensive — should not happen when all clean
                return GroupSettlementResult(
                    group_id=group_id, state="partially_invalid", valid=False,
                    all_legs_clean=False, leg_states=leg_states, realized_payout=None,
                    expected_lower_bound=certified_profit_lower_bound, margin=None,
                    reasons=reasons + [f"leg {mid}: unresolvable side"])
            entry = float(leg.get("entry_price") or 0.0)
            qty = float(leg.get("qty") or 0.0)
            payout += (settle - entry) * qty

        payout = round(payout, 8)
        bound = float(certified_profit_lower_bound)
        margin = round(payout - bound, 8)
        valid = payout >= bound - self.tolerance
        return GroupSettlementResult(
            group_id=group_id, state="validated" if valid else "violated",
            valid=valid, all_legs_clean=True, leg_states=leg_states,
            realized_payout=payout, expected_lower_bound=bound, margin=margin,
            reasons=reasons or (["payout cleared certified lower bound"] if valid
                                else ["payout below certified lower bound"]))


DEFAULT_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _parse_json_list(v) -> list:
    """Gamma returns ``outcomes``/``outcomePrices`` as a JSON-encoded string or a list."""
    import json as _json
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            out = _json.loads(v)
            return out if isinstance(out, list) else []
        except (ValueError, TypeError):
            return []
    return []


def gamma_settlement_fetcher(*, base_url: Optional[str] = None, timeout_s: float = 4.0):
    """Build a READ-ONLY Polymarket ``/markets/{id}`` resolution fetcher (one GET per id) for
    the closed-loop settlement labeler. Returns a raw resolution OBSERVATION dict suitable for
    :meth:`SettlementTruthEngine.classify` — never a trade, never a write, never raises.

    Observation contract: always carries ``closed`` so the caller can treat a closed market
    as TERMINAL (resolve clean, or record dirty + drop) and an unclosed one as 'retry later'.
    A clean binary resolution (``outcomePrices`` ~ [1,0]/[0,1]) yields ``winning_outcome``
    YES/NO; a closed-but-not-clean market yields ``winning_outcome=None`` (classifier ->
    non-trainable) so an ambiguous/void settlement never trains the model."""
    import os
    url = base_url or os.getenv("SETTLEMENT_MARKETS_URL", DEFAULT_GAMMA_MARKETS_URL)
    _box: dict = {}

    def _client():
        c = _box.get("c")
        if c is None:
            import httpx
            c = httpx.Client(timeout=timeout_s,
                             headers={"User-Agent": "hermes-settlement/1.0"})
            _box["c"] = c
        return c

    def _fetch(market_id, condition_id=None) -> Optional[dict]:
        mid = str(market_id or condition_id or "").strip()
        if not mid:
            return None
        try:
            resp = _client().get(f"{url}/{mid}")
            if resp.status_code != 200:
                return None
            d = resp.json()
            if isinstance(d, list):
                d = d[0] if d else {}
        except Exception:  # noqa: BLE001 — read-only enrichment never breaks a tick
            return None
        if not isinstance(d, dict):
            return None
        closed = bool(d.get("closed")) or str(d.get("umaResolutionStatus") or "").lower() \
            in ("resolved", "settled")
        obs = {"market_id": mid, "settlement_source": "gamma", "closed": closed,
               "resolved": False}
        if not closed:
            return obs
        prices = []
        for x in _parse_json_list(d.get("outcomePrices")):
            try:
                prices.append(float(x))
            except (TypeError, ValueError):
                prices.append(None)
        outcomes = [str(x) for x in _parse_json_list(d.get("outcomes"))]
        if len(prices) >= 2 and None not in prices[:2]:
            hi, lo = max(prices[:2]), min(prices[:2])
            if hi >= 0.99 and lo <= 0.01:
                win_idx = prices.index(hi)
                lbl = outcomes[win_idx].strip().lower() if win_idx < len(outcomes) else ""
                winner = "YES" if (lbl in ("yes", "y", "true") or win_idx == 0) else "NO"
                obs.update({"resolved": True, "winning_outcome": winner})
                return obs
        # closed but not an unambiguous 0/1 -> mark resolved with no clean winner +
        # high ambiguity so the truth engine yields a non-trainable (ambiguous) label.
        obs.update({"resolved": True, "winning_outcome": None, "ambiguity_score": 0.6})
        return obs

    return _fetch


def default_settlement_fetcher():
    """Constructor default: ON only when ``CLOSED_LOOP_SETTLEMENT_FETCH_ENABLED`` (or the
    shared read-only CLOB hydration flag) is set, so offline/unit runs never hit the network."""
    import os

    def _on(name: str) -> bool:
        return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")
    if _on("CLOSED_LOOP_SETTLEMENT_FETCH_ENABLED") or _on("BREGMAN_CLOB_HYDRATION_ENABLED"):
        return gamma_settlement_fetcher()
    return None
