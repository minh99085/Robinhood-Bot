"""Guarded-live audit log (Phase 8). Fail-closed best-effort persistence."""

from __future__ import annotations

import time
from typing import Optional

from .secret_policy import redact


def write_audit(store, *, event_type: str, severity: str = "INFO",
                actor: Optional[str] = None, state: Optional[str] = None,
                config_hash: Optional[str] = None, payload: Optional[dict] = None) -> None:
    if store is None:
        return
    try:
        store.add_guarded_live_audit_event({
            "ts_ms": int(time.time() * 1000), "event_type": event_type, "severity": severity,
            "actor": redact(actor) if actor else None, "state": state,
            "config_hash": config_hash, "payload_json": payload or {}})
    except Exception:  # noqa: BLE001 — audit failures must not crash
        pass
