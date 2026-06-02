"""Micro-live execution package (Phase 9).

The smallest possible real-execution surface: one tiny FOK canary order at a
time, demo-by-default, CLI-only, hard-capped, and blocked unless EVERY lock and
gate is open. Disabled by default. No strategy loop, dashboard button, or Grok
path may submit, cancel, approve, arm, or size anything.
"""

from __future__ import annotations

from .config import (EMERGENCY_CANCEL_CONFIRMATION, REQUIRED_ACK_PHRASE,
                     SUBMIT_CONFIRMATION, MicroLiveConfig)
from .errors import (ForbiddenEndpointError, MicroLiveDisabled, MicroLiveError,
                     MicroLiveLockError, NotImplementedLiveSigning)
from .locks import all_pass, check_locks, failed_locks

__all__ = [
    "MicroLiveConfig", "REQUIRED_ACK_PHRASE", "SUBMIT_CONFIRMATION",
    "EMERGENCY_CANCEL_CONFIRMATION", "MicroLiveError", "MicroLiveDisabled",
    "MicroLiveLockError", "ForbiddenEndpointError", "NotImplementedLiveSigning",
    "check_locks", "all_pass", "failed_locks",
]
