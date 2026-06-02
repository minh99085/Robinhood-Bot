"""DisabledLiveBroker (Phase 8) — the default broker for every venue.

Every execution method raises LiveExecutionDisabled. No network calls. This is
the "lock on the door": guarded-live ships with execution hard-disabled.
"""

from __future__ import annotations

from .broker_interfaces import LiveBrokerInterface


class DisabledLiveBroker(LiveBrokerInterface):
    def __init__(self, venue: str = "unknown"):
        self.venue = venue

    def preflight_check(self) -> dict:
        return {"venue": self.venue, "status": "disabled", "live_execution": False,
                "reason": "live execution disabled (Phase 8 design/dry-run only)"}

    def validate_order(self, order: dict) -> dict:
        return {"venue": self.venue, "status": "disabled", "live_execution": False,
                "accepted": False}

    def reconcile(self) -> dict:
        return {"venue": self.venue, "status": "disabled", "noop": True}

    def health(self) -> dict:
        return {"venue": self.venue, "status": "disabled", "live_execution": False}
