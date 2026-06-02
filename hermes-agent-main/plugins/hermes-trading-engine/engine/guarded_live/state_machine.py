"""GuardedLiveStateMachine (Phase 8).

Fail-closed. There is NO live/real-money/production/auto-live state, and no
transition can enable real execution. Attempting to enter a forbidden (live)
state raises GuardedLiveStateError.
"""

from __future__ import annotations

import time
from typing import Optional

from .audit import write_audit
from .errors import GuardedLiveStateError

STATES = (
    "DISABLED", "DESIGN_ONLY", "PRECHECK_FAILED", "PRECHECK_PASSED", "AWAITING_APPROVAL",
    "APPROVED_DRY_RUN_ONLY", "ARMED_DRY_RUN_ONLY", "DRY_RUN_ACTIVE", "PAUSED",
    "KILL_SWITCHED", "EXPIRED", "STOPPED", "FAILED")

# Names that must NEVER exist as states (guard against accidental live enablement).
FORBIDDEN_LIVE_STATES = frozenset({
    "LIVE_ACTIVE", "REAL_MONEY_ACTIVE", "PRODUCTION_EXECUTION", "AUTO_LIVE",
    "READY_FOR_AUTO_LIVE", "LIVE", "REAL_MONEY"})

_ALLOWED = {
    "DISABLED": {"DESIGN_ONLY", "KILL_SWITCHED", "FAILED"},
    "DESIGN_ONLY": {"PRECHECK_PASSED", "PRECHECK_FAILED", "KILL_SWITCHED", "FAILED", "STOPPED"},
    "PRECHECK_FAILED": {"DESIGN_ONLY", "PRECHECK_PASSED", "KILL_SWITCHED", "FAILED", "STOPPED"},
    "PRECHECK_PASSED": {"AWAITING_APPROVAL", "PRECHECK_FAILED", "KILL_SWITCHED", "EXPIRED",
                        "FAILED", "STOPPED"},
    "AWAITING_APPROVAL": {"APPROVED_DRY_RUN_ONLY", "PRECHECK_FAILED", "KILL_SWITCHED", "EXPIRED",
                          "FAILED", "STOPPED"},
    "APPROVED_DRY_RUN_ONLY": {"ARMED_DRY_RUN_ONLY", "KILL_SWITCHED", "EXPIRED", "FAILED",
                              "STOPPED"},
    "ARMED_DRY_RUN_ONLY": {"DRY_RUN_ACTIVE", "KILL_SWITCHED", "EXPIRED", "FAILED", "STOPPED",
                           "PAUSED"},
    "DRY_RUN_ACTIVE": {"PAUSED", "STOPPED", "KILL_SWITCHED", "EXPIRED", "FAILED"},
    "PAUSED": {"DRY_RUN_ACTIVE", "STOPPED", "KILL_SWITCHED", "EXPIRED", "FAILED"},
    "KILL_SWITCHED": {"STOPPED", "DISABLED"},
    "EXPIRED": {"DESIGN_ONLY", "STOPPED", "DISABLED"},
    "STOPPED": {"DISABLED", "DESIGN_ONLY"},
    "FAILED": {"DISABLED", "STOPPED"},
}


def is_forbidden_live_state(name: str) -> bool:
    return str(name).upper() in FORBIDDEN_LIVE_STATES


class GuardedLiveStateMachine:
    def __init__(self, store=None, config=None, initial: Optional[str] = None):
        self.store = store
        self.config = config
        self.state = initial or self._load_state() or "DISABLED"

    def _load_state(self) -> Optional[str]:
        if self.store is None:
            return None
        try:
            rows = self.store.get_guarded_live_state(1)
            return rows[0]["state"] if rows else None
        except Exception:  # noqa: BLE001
            return None

    def can_transition(self, to: str) -> bool:
        return to in _ALLOWED.get(self.state, set())

    def transition(self, to: str, reason: str = "", actor: Optional[str] = None) -> bool:
        if is_forbidden_live_state(to):
            # Hard rule: no transition may enable real execution.
            write_audit(self.store, event_type="forbidden_live_transition_blocked",
                        severity="CRITICAL", actor=actor, state=self.state,
                        config_hash=self.config.config_hash() if self.config else None,
                        payload={"attempted": to, "reason": reason})
            raise GuardedLiveStateError(
                f"forbidden live state {to!r}: real execution can never be enabled")
        if to not in STATES:
            raise GuardedLiveStateError(f"unknown state {to!r}")
        # kill switch + expiry are allowed from any state
        if to not in ("KILL_SWITCHED", "EXPIRED", "FAILED") and not self.can_transition(to):
            raise GuardedLiveStateError(f"illegal transition {self.state} -> {to}")
        prev = self.state
        self.state = to
        if self.store is not None:
            try:
                self.store.add_guarded_live_state({
                    "ts_ms": int(time.time() * 1000), "state": to, "previous_state": prev,
                    "reason": reason,
                    "config_hash": self.config.config_hash() if self.config else None,
                    "payload_json": {"actor": actor}})
            except Exception:  # noqa: BLE001
                pass
        write_audit(self.store, event_type="state_transition", actor=actor, state=to,
                    config_hash=self.config.config_hash() if self.config else None,
                    payload={"from": prev, "to": to, "reason": reason})
        return True
