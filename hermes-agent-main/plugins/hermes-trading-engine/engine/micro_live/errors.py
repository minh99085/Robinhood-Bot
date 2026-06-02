"""Micro-live errors (Phase 9). Favor explicit refusal over clever fallback."""

from __future__ import annotations


class MicroLiveError(Exception):
    """Base micro-live error."""


class MicroLiveDisabled(MicroLiveError):
    """Raised when a real execution method is reached without all locks open.

    Phase 9 keeps live execution DISABLED by default. This is raised by the live
    broker base methods and whenever any lock/gate fails before a network call.
    """

    def __init__(self, method: str = "execution", reason: str = ""):
        msg = f"micro-live execution blocked ({method})"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)
        self.method = method
        self.reason = reason


class MicroLiveLockError(MicroLiveError):
    """One or more locks/gates failed."""


class ForbiddenEndpointError(MicroLiveError):
    """A forbidden network endpoint (deposit/withdraw/bridge/amend/batch/prod) was
    attempted."""


class NotImplementedLiveSigning(MicroLiveError):
    """A venue live signer is intentionally not implemented (fails safe)."""


class ReconciliationError(MicroLiveError):
    """Reconciliation failed or returned an ambiguous/unknown status."""


class IdempotencyError(MicroLiveError):
    """A duplicate submit was prevented by the idempotency guard."""
