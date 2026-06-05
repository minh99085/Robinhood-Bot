"""Cost/depth-aware arbitrage certification (PAPER ONLY, pure, deterministic).

Given a group of outcomes with an *exactly-one-true* relationship (complement /
MECE / range, or a cross-market pair reduced to such), the canonical coherence
arbitrage is to BUY one share of each leg: in every feasible world state exactly
one share pays $1, so the worst-case payoff is $1 per "set". The set is profitable
iff ``1 - sum(ask) - fees > 0``.

This module certifies that with a **worst-case (min over feasible atoms)** check
of a constructed, depth-bounded portfolio — a sound certificate equivalent to the
LP that maximizes the guaranteed (worst-case) after-fee profit subject to depth.
A non-certified group is never tradeable ("no certified proof means no trade").

Soundness: the certificate's ``after_fee_profit_per_set`` is the *minimum* profit
over ALL enumerated feasible states, so a positive value guarantees nonnegative
(indeed positive) payoff in every admissible resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .constraint_graph import Constraint, ConstraintGraph, Outcome

logger = logging.getLogger("hte.arbitrage.certificate")


class CertificateStatus:
    """Executable-certification states for a Bregman arbitrage (PAPER ONLY).

    Bregman may only TRADE when the status is ``EXECUTABLE_AFTER_COST_CERTIFIED``.
    A theoretically-proven arb that cannot be executed atomically (multi-leg on a
    non-atomic venue) is ``CERTIFIED_THEORETICAL_NOT_EXECUTABLE`` and is NOT
    tradable. All other states are explicit rejections with a reason.
    """

    EXECUTABLE_AFTER_COST_CERTIFIED = "EXECUTABLE_AFTER_COST_CERTIFIED"
    CERTIFIED_THEORETICAL_NOT_EXECUTABLE = "CERTIFIED_THEORETICAL_NOT_EXECUTABLE"
    REJECTED_NO_WORST_CASE_PROFIT = "REJECTED_NO_WORST_CASE_PROFIT"
    REJECTED_INSUFFICIENT_DEPTH = "REJECTED_INSUFFICIENT_DEPTH"
    REJECTED_AFTER_COST_NONPOSITIVE = "REJECTED_AFTER_COST_NONPOSITIVE"
    REJECTED_STALE_BOOK = "REJECTED_STALE_BOOK"
    REJECTED_MISSING_OUTCOME = "REJECTED_MISSING_OUTCOME"


@dataclass
class FeeModel:
    """Conservative taker fee model (paper). Fees only ever *reduce* certified
    profit, so the certificate stays sound."""

    taker_fee_bps: float = 0.0     # bps on traded notional (sum of buy prices)
    per_share_fee: float = 0.0     # flat fee per share bought

    def set_fee(self, buy_prices: Sequence[float]) -> float:
        notional = sum(float(p) for p in buy_prices)
        return notional * (self.taker_fee_bps / 10_000.0) + self.per_share_fee * len(buy_prices)


@dataclass
class Certificate:
    """A deterministic worst-case arbitrage certificate."""

    certified: bool
    relation: str
    outcome_ids: list[str]
    worst_case_payoff_per_set: float = 0.0
    cost_per_set: float = 0.0
    fee_per_set: float = 0.0
    after_fee_profit_per_set: float = 0.0
    size: float = 0.0                       # certifiable set count (depth-bounded)
    total_after_fee_profit: float = 0.0
    portfolio: dict = field(default_factory=dict)   # outcome_id -> shares to BUY
    atoms_checked: int = 0
    fill_feasible: bool = False
    executable_depth_ok: bool = False       # EVERY leg passes executable-depth
    min_leg_depth: float = 0.0              # shares available on the thinnest leg
    legs_depth: dict = field(default_factory=dict)  # outcome_id -> ask_depth
    deterministic: bool = True
    reason: str = ""
    # --- hard executable-certification (after-cost, depth, atomicity) ---
    status: str = ""                        # CertificateStatus
    min_profit_after_cost: float = 0.0      # worst-case profit per set after ALL costs
    spread_cost_per_set: float = 0.0
    slippage_cost_per_set: float = 0.0
    required_capital: float = 0.0           # cost_per_set * size
    leg_depth_ok: bool = False              # alias of executable_depth_ok (audit name)
    atomicity_risk: bool = False
    stale_book: bool = False
    fantasy_fill: bool = False              # profitable but undepthed -> would be fantasy
    rejection_reason: str = ""

    @property
    def executable(self) -> bool:
        """True ONLY when after-cost executability is certified (tradable)."""
        return self.status == CertificateStatus.EXECUTABLE_AFTER_COST_CERTIFIED

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["executable"] = self.executable
        return d

    def is_multi_leg(self) -> bool:
        """True when the arbitrage requires buying more than one leg."""
        return len(self.outcome_ids) > 1

    def audit_diagnostics(self) -> dict:
        """Per-certificate diagnostics for the Algorithmic Edge Audit (pure)."""
        return {
            "certified": bool(self.certified),
            "executable_depth_ok": bool(self.executable_depth_ok),
            "n_legs": len(self.outcome_ids),
            "worst_case_payoff_per_set": self.worst_case_payoff_per_set,
            "expected_min_profit": self.after_fee_profit_per_set if self.certified else 0.0,
            "cost_per_set": self.cost_per_set,
            "fee_per_set": self.fee_per_set,
            "min_leg_depth": self.min_leg_depth,
            "size": self.size,
            "rejection_reason": None if self.certified else self.reason,
        }


# Non-null required fields for the Bregman-diagnostics section of the canonical
# AlgorithmicEdgeAudit. Missing certified-arbitrage fields make the audit
# non-decision-grade (the edge engine's output cannot be verified).
BREGMAN_AUDIT_REQUIRED: tuple = (
    "constraint_groups_scanned", "candidate_arbitrages", "certified_arbitrages",
    "executable_depth_certified",
)


def missing_bregman_fields(section) -> list:
    """Return the required Bregman-diagnostics audit fields that are None/absent."""
    section = section or {}
    return [f"bregman.{k}" for k in BREGMAN_AUDIT_REQUIRED if section.get(k) is None]


def atomicity_risk(cert: "Certificate", *, venue_supports_atomic_multileg: bool = False) -> dict:
    """Assess whether a certified arb can be executed atomically risk-free (pure).

    A certificate proves a *worst-case* profit assuming ALL legs are acquired. On a
    venue without atomic multi-order fills (Polymarket CLOB v2), a multi-leg arb
    carries **leg-in risk**: you may fill some legs and not others. This helper
    reports that risk so the execution layer logs (but does not mark executable)
    any certified opportunity whose atomic execution cannot be guaranteed.
    """
    multi = cert.is_multi_leg()
    atomic_ok = bool(cert.certified) and (not multi or bool(venue_supports_atomic_multileg))
    return {
        "certified": bool(cert.certified),
        "multi_leg": multi,
        "n_legs": len(cert.outcome_ids),
        "venue_supports_atomic_multileg": bool(venue_supports_atomic_multileg),
        "atomic_risk_free_guaranteed": atomic_ok,
        "reason": ("atomic_ok" if atomic_ok else
                   ("not_certified" if not cert.certified else
                    "multi_leg_non_atomic_venue")),
    }


def _worst_case_payoff(portfolio: Mapping[str, float],
                       atoms: Sequence[Mapping[str, int]]) -> float:
    """Minimum gross payoff of a long portfolio over feasible world states."""
    worst = None
    for atom in atoms:
        payoff = sum(qty * float(atom.get(oid, 0)) for oid, qty in portfolio.items())
        worst = payoff if worst is None else min(worst, payoff)
    return float(worst if worst is not None else 0.0)


def certify_group(graph: ConstraintGraph, constraint: Constraint, *,
                  fee_model: Optional[FeeModel] = None, profit_floor: float = 0.005,
                  max_size: float = 1e9, min_leg_depth: float = 0.0,
                  spread_cost_per_set: float = 0.0, slippage_bps: float = 0.0,
                  stale: bool = False,
                  venue_supports_atomic_multileg: bool = False) -> Certificate:
    """Certify (or reject) a buy-set arbitrage for one constraint, classifying its
    **executable** status.

    Proves the worst-case payoff is nonnegative and the minimum profit is positive
    AFTER fees, spread, slippage, and depth limits, then classifies:

    * ``EXECUTABLE_AFTER_COST_CERTIFIED`` — tradable (single-leg, or atomic venue),
    * ``CERTIFIED_THEORETICAL_NOT_EXECUTABLE`` — proven but multi-leg on a
      non-atomic venue (leg-in / partial-fill / stale-book risk) → NOT tradable,
    * ``REJECTED_*`` — no worst-case profit, insufficient depth (fantasy fill),
      after-cost non-positive, stale book, or missing outcomes.

    ``certified`` / ``reason`` keep their legacy (theoretical proof) meaning;
    ``status`` / ``executable`` add the hard after-cost executability gate.
    Deterministic + sound (worst-case over feasible atoms).
    """
    fee_model = fee_model or FeeModel()
    ids = list(constraint.outcome_ids)
    outcomes: list[Outcome] = [graph.get(i) for i in ids]  # type: ignore[misc]
    if any(o is None for o in outcomes):
        return Certificate(False, constraint.type.value, ids, reason="missing_outcome",
                           status=CertificateStatus.REJECTED_MISSING_OUTCOME,
                           rejection_reason="missing_outcome")

    atoms = graph.feasible_atoms(constraint)
    if not atoms:
        return Certificate(False, constraint.type.value, ids,
                           reason="no_enumerable_atoms",
                           status=CertificateStatus.REJECTED_NO_WORST_CASE_PROFIT,
                           rejection_reason="no_enumerable_atoms")

    buy_prices = [o.buy_price() for o in outcomes]
    portfolio = {o.id: 1.0 for o in outcomes}        # buy one share of each leg
    worst_payoff = _worst_case_payoff(portfolio, atoms)
    cost = sum(buy_prices)
    fee = fee_model.set_fee(buy_prices)
    profit_per_set = worst_payoff - cost - fee

    # Per-leg executable depth: every leg must clear min_leg_depth (and be > 0)
    # before any size is allowed. The certifiable set count is the THINNEST leg.
    legs_depth = {o.id: float(o.ask_depth) for o in outcomes}
    depth = min(legs_depth.values(), default=0.0)
    executable_depth_ok = depth > 0 and depth >= float(min_leg_depth)
    size = max(0.0, min(depth, float(max_size))) if executable_depth_ok else 0.0
    worst_case_proof = profit_per_set > profit_floor
    certified = worst_case_proof and executable_depth_ok and size > 0
    fill_feasible = size > 0 and executable_depth_ok

    if certified:
        reason = "certified"
    elif not worst_case_proof:
        reason = "no_positive_worst_case_profit"
    elif depth <= 0:
        reason = "no_depth"
    elif not executable_depth_ok:
        reason = "insufficient_executable_depth"
    else:
        reason = "no_depth"

    # --- hard after-cost executability classification ---
    slippage_cost = (float(slippage_bps) / 10_000.0) * cost
    after_cost_per_set = profit_per_set - float(spread_cost_per_set) - slippage_cost
    atomicity_risk = (len(ids) > 1) and (not venue_supports_atomic_multileg)
    fantasy_fill = bool(worst_case_proof and not executable_depth_ok)

    if not worst_case_proof:
        status = CertificateStatus.REJECTED_NO_WORST_CASE_PROFIT
    elif not executable_depth_ok:
        status = CertificateStatus.REJECTED_INSUFFICIENT_DEPTH
    elif stale:
        status = CertificateStatus.REJECTED_STALE_BOOK
    elif after_cost_per_set <= profit_floor:
        status = CertificateStatus.REJECTED_AFTER_COST_NONPOSITIVE
    elif atomicity_risk:
        status = CertificateStatus.CERTIFIED_THEORETICAL_NOT_EXECUTABLE
    else:
        status = CertificateStatus.EXECUTABLE_AFTER_COST_CERTIFIED
    rejection_reason = "" if status in (
        CertificateStatus.EXECUTABLE_AFTER_COST_CERTIFIED,
        CertificateStatus.CERTIFIED_THEORETICAL_NOT_EXECUTABLE) else status

    cert = Certificate(
        certified=bool(certified), relation=constraint.type.value, outcome_ids=ids,
        worst_case_payoff_per_set=round(worst_payoff, 6),
        cost_per_set=round(cost, 6), fee_per_set=round(fee, 6),
        after_fee_profit_per_set=round(profit_per_set, 6),
        size=round(size, 6),
        total_after_fee_profit=round(profit_per_set * size, 6) if certified else 0.0,
        portfolio=portfolio, atoms_checked=len(atoms), fill_feasible=fill_feasible,
        executable_depth_ok=bool(executable_depth_ok),
        min_leg_depth=round(depth, 6), legs_depth=legs_depth,
        reason=reason, status=status,
        min_profit_after_cost=round(after_cost_per_set, 6),
        spread_cost_per_set=round(float(spread_cost_per_set), 6),
        slippage_cost_per_set=round(slippage_cost, 6),
        required_capital=round(cost * size, 6),
        leg_depth_ok=bool(executable_depth_ok), atomicity_risk=bool(atomicity_risk),
        stale_book=bool(stale), fantasy_fill=fantasy_fill,
        rejection_reason=rejection_reason)
    if cert.certified:
        logger.info("bregman certificate: %s legs=%s profit/set=%.4f size=%.2f total=%.4f",
                    cert.relation, ids, cert.after_fee_profit_per_set, cert.size,
                    cert.total_after_fee_profit)
    else:
        logger.debug("bregman not certified: %s legs=%s reason=%s profit/set=%.4f",
                     cert.relation, ids, cert.reason, cert.after_fee_profit_per_set)
    return cert


def certified_trade_size(cert: Optional[Certificate], *, equity: float = 0.0,
                         max_frac: float = 0.50) -> float:
    """Set count a certified arbitrage may trade — **0 unless fully certified**.

    A Bregman arb may size LARGER (depth-bounded set count, optionally capped by
    ``max_frac`` of equity worth of cost) ONLY when the certificate proves a
    positive worst-case after-fee profit AND every leg passes executable depth.
    Any non-certified / non-executable / fantasy certificate sizes to 0. This is
    the risk-side enforcement of "no certified proof means no trade". Pure.
    """
    if cert is None or not cert.certified or not cert.executable_depth_ok:
        return 0.0
    size = max(0.0, float(cert.size))
    eq = max(0.0, float(equity or 0.0))
    cost_per_set = float(cert.cost_per_set or 0.0)
    if eq > 0 and cost_per_set > 0 and max_frac > 0:
        equity_bounded = (max_frac * eq) / cost_per_set
        size = min(size, equity_bounded)
    return round(max(0.0, size), 6)
