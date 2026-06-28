"""Cross-window dependency arbitrage (LCMM layer).

Layer 1: deterministic linear constraints (nested 5m inside 15m implication).
Layer 2 (later): Bregman/Frank-Wolfe — gated, not required here.

PAPER ONLY — scanner always on; execution gated by ``execute_enabled``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.pulse.execution_gate import vwap_fill


def realized_dependency_profit_usd(trade: dict) -> float:
    """VWAP- and ROI-bounded paper profit for nested implication (not raw mid-gap × shares)."""
    shares = float(trade.get("shares") or 0)
    entry = float(trade.get("entry_vwap") or 0)
    cost = float(trade.get("cost_usd") or 0)
    mag = float(trade.get("violation_magnitude") or 0)
    implied = float(trade.get("implied_bound") or (entry + mag))
    cap_frac = float(trade.get("capture_frac") or 0.5)
    expected = float(trade.get("expected_profit_usd") or 0)
    if shares <= 0 or mag <= 0:
        return 0.0
    vwap_edge = max(0.0, implied - entry)
    per_share = min(mag, vwap_edge) * cap_frac
    raw = shares * per_share
    roi_cap = cost * mag * cap_frac
    capped = min(raw, roi_cap)
    if expected > 0:
        capped = min(capped, expected)
    return round(max(0.0, capped), 6)


@dataclass
class DependencyViolation:
    """A detected LCMM constraint violation (may or may not be executable)."""
    constraint_type: str
    parent_window_key: str
    child_window_keys: list
    description: str
    parent_up_mid: Optional[float] = None
    child_up_mids: list = field(default_factory=list)
    implied_bound: Optional[float] = None
    violation_magnitude: float = 0.0
    actionable: bool = False
    reason: str = "log_only"

    def to_dict(self) -> dict:
        return {"constraint_type": self.constraint_type,
                "parent_window_key": self.parent_window_key,
                "child_window_keys": list(self.child_window_keys),
                "description": self.description,
                "parent_up_mid": self.parent_up_mid,
                "child_up_mids": list(self.child_up_mids),
                "implied_bound": self.implied_bound,
                "violation_magnitude": round(self.violation_magnitude, 6),
                "actionable": self.actionable, "reason": self.reason}


def _up_mid(window) -> Optional[float]:
    book = getattr(window, "up_book", None)
    if book is None:
        return None
    return getattr(book, "mid", None)


def group_nested_windows(windows: list) -> list:
    """Group 5m windows whose open_ts falls inside a 15m parent's [open, close)."""
    parents = [w for w in windows if int(getattr(w, "window_seconds", 0) or 0) >= 900]
    children = [w for w in windows if int(getattr(w, "window_seconds", 0) or 0) < 900]
    groups = []
    for p in parents:
        nested = [c for c in children
                  if float(p.open_ts) <= float(c.open_ts) < float(p.close_ts)]
        if nested:
            groups.append((p, sorted(nested, key=lambda x: x.open_ts)))
    return groups


def validate_violation(v: DependencyViolation) -> tuple[bool, str]:
    """Deterministic validator — LLM proposals must pass this before any trade."""
    if v.constraint_type != "nested_implication":
        return False, "unsupported_constraint"
    if v.violation_magnitude <= 0:
        return False, "no_magnitude"
    if not v.parent_window_key or not v.child_window_keys:
        return False, "missing_window_keys"
    if v.parent_up_mid is None or not v.child_up_mids:
        return False, "missing_prices"
    if float(v.child_up_mids[0]) <= float(v.parent_up_mid):
        return False, "implication_not_violated"
    return True, "ok"


def enrich_vwap_actionable(
    violation: DependencyViolation,
    parent,
    child,
    *,
    max_usd: float = 50.0,
    epsilon: float = 0.02,
    capture_frac: float = 0.5,
) -> DependencyViolation:
    """Mark violation actionable when VWAP-executable parent-UP buy clears epsilon."""
    ok, val_reason = validate_violation(violation)
    if not ok:
        violation.actionable = False
        violation.reason = val_reason
        return violation
    trade, fail_reason = try_execute_nested_implication(
        parent, child, violation, max_usd=max_usd, epsilon=epsilon,
        capture_frac=capture_frac, return_reason=True)
    if trade is None or float(trade.get("expected_profit_usd") or 0.0) <= 0:
        violation.actionable = False
        violation.reason = fail_reason or "vwap_not_executable"
        return violation
    violation.actionable = True
    violation.reason = "vwap_executable"
    return violation


def scan_nested_implication(
    parent,
    children: list,
    *,
    epsilon: float = 0.02,
    max_usd: float = 50.0,
    vwap_enrich: bool = True,
) -> list:
    """LCMM: P(up over 15m) >= max P(up over constituent 5m windows) on mids."""
    out = []
    p_mid = _up_mid(parent)
    if p_mid is None:
        return out
    for c in children:
        c_mid = _up_mid(c)
        if c_mid is None:
            continue
        mag = float(c_mid) - float(p_mid)
        if mag > float(epsilon):
            v = DependencyViolation(
                constraint_type="nested_implication",
                parent_window_key=str(parent.event_id),
                child_window_keys=[str(c.event_id)],
                description=("15m up-mid below nested 5m up-mid: "
                             "P(up_15m) cannot be < P(up_5m) for overlapping window"),
                parent_up_mid=round(float(p_mid), 6),
                child_up_mids=[round(float(c_mid), 6)],
                implied_bound=round(float(c_mid), 6),
                violation_magnitude=round(mag, 6),
                actionable=False,
                reason="detected",
            )
            if vwap_enrich:
                v = enrich_vwap_actionable(
                    v, parent, c, max_usd=max_usd, epsilon=epsilon)
            out.append(v)
    return out


def scan_windows(
    windows: list,
    *,
    epsilon: float = 0.02,
    max_usd: float = 50.0,
    vwap_enrich: bool = True,
) -> list:
    """Run all LCMM dependency scans with optional VWAP executability enrichment."""
    violations = []
    for parent, children in group_nested_windows(windows):
        violations.extend(scan_nested_implication(
            parent, children, epsilon=epsilon, max_usd=max_usd,
            vwap_enrich=vwap_enrich))
    return violations


def try_execute_nested_implication(
    parent,
    child,
    violation: DependencyViolation,
    *,
    max_usd: float = 50.0,
    epsilon: float = 0.02,
    capture_frac: float = 0.5,
    bregman_diag: Optional[dict] = None,
    bregman_authority: bool = False,
    return_reason: bool = False,
) -> Optional[dict]:
    """Paper BUY parent UP when nested implication violated (parent UP underpriced vs child).

    Conservative paper model: expected edge = violation_magnitude * shares * capture_frac,
    booked at parent window close. Deterministic validator must pass first.
    """
    fail = "vwap_not_executable"

    def _ret(trade: Optional[dict], reason: str = "ok"):
        if return_reason:
            return trade, (reason if trade is None else "ok")
        return trade

    ok, reason = validate_violation(violation)
    if not ok:
        return _ret(None, reason)
    book = getattr(parent, "up_book", None)
    if book is None or not getattr(book, "asks", None):
        return _ret(None, "missing_parent_book")
    trade_usd = float(max_usd)
    if bregman_authority and bregman_diag:
        from engine.pulse.bregman_projection import modified_kelly_arb_size_usd
        edge = float(
            bregman_diag.get("max_theoretical_profit_per_share")
            or violation.violation_magnitude)
        depth = float(getattr(book, "ask_depth_usd", 0) or max_usd)
        trade_usd = modified_kelly_arb_size_usd(
            edge_per_share=edge,
            fill_probability=0.85,
            max_usd=max_usd,
            depth_cap_usd=max(depth * 0.5, 1.0),
        )
        if trade_usd <= 0:
            return _ret(None, "bregman_kelly_zero")
        if not bregman_diag.get("actionable_projection"):
            return _ret(None, "bregman_not_actionable")
    vwap, spent, shares, full = vwap_fill(book.asks, trade_usd)
    if vwap is None:
        return _ret(None, "vwap_fill_failed")
    if not full:
        return _ret(None, "partial_fill")
    if shares <= 0:
        return _ret(None, "zero_shares")
    if violation.violation_magnitude < float(epsilon):
        return _ret(None, "below_epsilon")
    expected = round(shares * violation.violation_magnitude * float(capture_frac), 6)
    if expected <= 0:
        return _ret(None, "zero_expected_profit")
    entry_mode = "dependency_bregman" if bregman_authority else "lcmm_nested"
    implied_bound = float(violation.implied_bound or violation.child_up_mids[0])
    trade = {
        "constraint_type": violation.constraint_type,
        "parent_window_key": str(parent.event_id),
        "child_window_key": str(child.event_id),
        "side": "buy_parent_up",
        "entry_mode": entry_mode,
        "shares": round(shares, 4),
        "cost_usd": round(spent, 4),
        "entry_vwap": round(vwap, 6),
        "expected_profit_usd": expected,
        "theoretical_profit_usd": expected,
        "capture_frac": float(capture_frac),
        "implied_bound": round(implied_bound, 6),
        "close_ts": float(parent.close_ts),
        "violation_magnitude": violation.violation_magnitude,
        "reason": entry_mode,
        "bregman_projection_distance": (bregman_diag or {}).get("projection_distance"),
    }
    trade["booked_profit_usd"] = realized_dependency_profit_usd(trade)
    return _ret(trade, "ok")


class DependencyArbLedger:
    """Separate ledger for dependency-arb (never blended with dutch-book or directional)."""

    def __init__(self, *, execute_enabled: bool = False):
        self.execute_enabled = bool(execute_enabled)
        self.scans = 0
        self.violations_detected = 0
        self.actionable_detected = 0
        self.executed = 0
        self.settled = 0
        self.realized_profit_usd = 0.0
        self.last_violations: list = []
        self.positions: dict = {}
        self.rejected_invalid = 0
        self.rejected_by_reason: dict = {}
        self.mid_only_violations = 0

    def record_scan(self, violations: list) -> None:
        self.scans += 1
        self.last_violations = [v.to_dict() if hasattr(v, "to_dict") else dict(v)
                              for v in (violations or [])]
        self.violations_detected += len(self.last_violations)
        self.actionable_detected += sum(
            1 for v in (violations or []) if bool(getattr(v, "actionable", False)))
        for v in (violations or []):
            actionable = bool(getattr(v, "actionable", False))
            reason = str(getattr(v, "reason", "") or "unknown")
            if actionable:
                continue
            if reason == "detected":
                self.mid_only_violations += 1
                reason = "mid_only_pending_vwap"
            self.rejected_by_reason[reason] = (
                int(self.rejected_by_reason.get(reason, 0) or 0) + 1)

    def has_open(self, parent_key: str) -> bool:
        return parent_key in self.positions

    def book(self, trade: dict, *, now: float) -> bool:
        if not self.execute_enabled or not trade:
            return False
        pk = str(trade.get("parent_window_key") or "")
        if not pk or pk in self.positions:
            return False
        self.positions[pk] = {**trade, "status": "open", "entry_ts": float(now)}
        self.executed += 1
        return True

    def settle_due(self, now: float) -> int:
        n = 0
        for pk, p in list(self.positions.items()):
            if p.get("status") == "open" and now >= float(p.get("close_ts") or 0):
                p["status"] = "settled"
                profit = realized_dependency_profit_usd(p)
                p["realized_profit_usd"] = profit
                p.setdefault("theoretical_profit_usd", p.get("expected_profit_usd"))
                self.realized_profit_usd = round(self.realized_profit_usd + profit, 6)
                self.settled += 1
                n += 1
        return n

    def report(self) -> dict:
        mode = "paper_execute" if self.execute_enabled else "log_only"
        return {"strategy": "dependency_arbitrage", "paper_only": True,
                "enabled": self.execute_enabled, "mode": mode,
                "scans": self.scans, "violations_detected": self.violations_detected,
                "actionable_detected": int(getattr(self, "actionable_detected", 0) or 0),
                "rejected_invalid": self.rejected_invalid,
                "rejected_by_reason": dict(self.rejected_by_reason),
                "mid_only_violations": int(getattr(self, "mid_only_violations", 0) or 0),
                "executed": self.executed, "settled": self.settled,
                "open": sum(1 for p in self.positions.values() if p.get("status") == "open"),
                "realized_profit_usd": round(self.realized_profit_usd, 4),
                "last_violations": self.last_violations[-20:],
                "segregated_from_directional": True,
                "note": ("LCMM nested-window scanner + optional paper execution on validated "
                         "nested_implication violations.")}

    def to_state(self) -> dict:
        return {"execute_enabled": self.execute_enabled, "scans": self.scans,
                "violations_detected": self.violations_detected,
                "rejected_invalid": self.rejected_invalid,
                "rejected_by_reason": dict(self.rejected_by_reason),
                "mid_only_violations": int(getattr(self, "mid_only_violations", 0) or 0),
                "executed": self.executed, "settled": self.settled,
                "realized_profit_usd": self.realized_profit_usd,
                "last_violations": self.last_violations,
                "positions": {k: dict(v) for k, v in self.positions.items()}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        # execute_enabled is set from PulseConfig after load — do not restore from disk.
        self.scans = int(data.get("scans", 0) or 0)
        self.violations_detected = int(data.get("violations_detected", 0) or 0)
        self.rejected_invalid = int(data.get("rejected_invalid", 0) or 0)
        self.rejected_by_reason = dict(data.get("rejected_by_reason") or {})
        self.mid_only_violations = int(data.get("mid_only_violations", 0) or 0)
        self.executed = int(data.get("executed", 0) or 0)
        self.settled = int(data.get("settled", 0) or 0)
        self.realized_profit_usd = float(data.get("realized_profit_usd", 0.0) or 0.0)
        self.last_violations = list(data.get("last_violations") or [])
        self.positions = {k: dict(v) for k, v in (data.get("positions") or {}).items()}