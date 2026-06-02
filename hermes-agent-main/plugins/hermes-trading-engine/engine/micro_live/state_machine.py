"""MicroLiveStateMachine (Phase 9). Explicit, audited transitions. No transition
may be triggered by Grok or a strategy loop. Only one live order may be SUBMITTING
or active at a time. Any hard failure goes to PAUSED/KILL_SWITCHED/FAILED."""

from __future__ import annotations

from typing import Optional

from .audit import write_audit
from .errors import MicroLiveError

STATES = ["DISABLED", "LOCKED", "PRECHECK_FAILED", "PRECHECK_PASSED", "AWAITING_APPROVAL",
          "APPROVED", "ARMED", "CANARY_READY", "SUBMITTING", "SUBMITTED", "ACKNOWLEDGED",
          "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCEL_REQUESTED", "CANCELLED",
          "RECONCILING", "PAUSED", "KILL_SWITCHED", "EXPIRED", "FAILED", "STOPPED"]

TERMINAL = {"FILLED", "REJECTED", "CANCELLED", "EXPIRED", "FAILED", "STOPPED", "KILL_SWITCHED"}

# Actors that may NEVER trigger a transition.
FORBIDDEN_ACTORS = {"grok", "research", "strategy", "strategy_loop", "auto",
                    "engine_tick", "dashboard"}

_ALLOWED = {
    "DISABLED": {"LOCKED", "PRECHECK_FAILED", "PRECHECK_PASSED", "STOPPED"},
    "LOCKED": {"DISABLED", "PRECHECK_FAILED", "STOPPED"},
    "PRECHECK_FAILED": {"DISABLED", "PRECHECK_PASSED", "STOPPED"},
    "PRECHECK_PASSED": {"AWAITING_APPROVAL", "PRECHECK_FAILED", "PAUSED", "STOPPED"},
    "AWAITING_APPROVAL": {"APPROVED", "PRECHECK_FAILED", "EXPIRED", "STOPPED"},
    "APPROVED": {"ARMED", "EXPIRED", "PAUSED", "STOPPED"},
    "ARMED": {"CANARY_READY", "EXPIRED", "PAUSED", "KILL_SWITCHED", "STOPPED"},
    "CANARY_READY": {"SUBMITTING", "EXPIRED", "PAUSED", "KILL_SWITCHED", "STOPPED"},
    "SUBMITTING": {"SUBMITTED", "REJECTED", "FAILED", "PAUSED", "KILL_SWITCHED"},
    "SUBMITTED": {"ACKNOWLEDGED", "RECONCILING", "PARTIALLY_FILLED", "FILLED", "REJECTED",
                  "CANCEL_REQUESTED", "PAUSED", "FAILED"},
    "ACKNOWLEDGED": {"RECONCILING", "PARTIALLY_FILLED", "FILLED", "CANCEL_REQUESTED",
                     "CANCELLED", "PAUSED", "FAILED"},
    "RECONCILING": {"FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED", "PAUSED", "FAILED"},
    "PARTIALLY_FILLED": {"RECONCILING", "PAUSED", "CANCEL_REQUESTED", "STOPPED", "FAILED"},
    "CANCEL_REQUESTED": {"CANCELLED", "PAUSED", "FAILED"},
    "PAUSED": {"RECONCILING", "STOPPED", "KILL_SWITCHED", "FAILED"},
    "FILLED": {"STOPPED"},
    "REJECTED": {"STOPPED"},
    "CANCELLED": {"STOPPED"},
    "EXPIRED": {"STOPPED"},
    "KILL_SWITCHED": {"STOPPED"},
    "FAILED": {"STOPPED"},
    "STOPPED": set(),
}


class MicroLiveStateMachine:
    def __init__(self, store=None, state: str = "DISABLED"):
        self.store = store
        self.state = state

    def can_transition(self, to_state: str) -> bool:
        return to_state in _ALLOWED.get(self.state, set())

    def transition(self, to_state: str, *, actor: str = "cli", reason: str = "",
                   canary_plan_id: Optional[str] = None,
                   live_order_attempt_id: Optional[str] = None) -> str:
        if str(actor).lower() in FORBIDDEN_ACTORS:
            raise MicroLiveError(f"actor '{actor}' may not trigger micro-live transitions")
        if to_state not in STATES:
            raise MicroLiveError(f"unknown state {to_state}")
        if not self.can_transition(to_state):
            raise MicroLiveError(f"illegal transition {self.state} -> {to_state}")
        prev, self.state = self.state, to_state
        write_audit(self.store, event_type="state_transition",
                    severity="WARN" if to_state in ("KILL_SWITCHED", "FAILED", "PAUSED") else "INFO",
                    actor=actor, canary_plan_id=canary_plan_id,
                    live_order_attempt_id=live_order_attempt_id, state=to_state,
                    message=f"{prev}->{to_state} {reason}".strip())
        return self.state
