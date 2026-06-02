"""Guarded-live errors (Phase 8).

LiveExecutionDisabled is raised by every real execution method. It exists so the
"door" is explicitly locked: there is no code path that submits, cancels, or
replaces a real order in this phase.
"""

from __future__ import annotations


class GuardedLiveError(Exception):
    """Base class for guarded-live errors."""


class LiveExecutionDisabled(GuardedLiveError):
    """Raised whenever any real order submit/cancel/replace is attempted.

    Phase 8 installs the locks but never opens the door — real execution is
    impossible. This is raised by DisabledLiveBroker and by the base
    LiveBrokerInterface execution methods.
    """

    def __init__(self, method: str = "execution", detail: str = ""):
        msg = (f"live execution is DISABLED ({method}). Phase 8 is design/dry-run "
               f"only; no real order submission/cancellation exists.")
        if detail:
            msg += f" {detail}"
        super().__init__(msg)
        self.method = method


class GuardedLiveStateError(GuardedLiveError):
    """Invalid / forbidden state transition (e.g. attempting a live state)."""


class ApprovalError(GuardedLiveError):
    """Approval workflow violation."""


class ArmingError(GuardedLiveError):
    """Arming-token workflow violation."""


class ConformanceFailure(GuardedLiveError):
    """A conformance check failed (e.g. a network/order/signing trap fired)."""


class SecretPolicyViolationError(GuardedLiveError):
    """A forbidden secret/env pattern was detected."""
