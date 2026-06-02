"""Production-review audit events (Phase 11)."""

from __future__ import annotations

import time
from typing import Optional


def write_audit(store, *, event_type: str, severity: str = "INFO", actor: Optional[str] = None,
                review_id: Optional[str] = None, message: str = "",
                payload: Optional[dict] = None) -> None:
    if store is None:
        return
    try:
        store.add_production_review_audit_event({
            "ts_ms": int(time.time() * 1000), "review_id": review_id, "event_type": event_type,
            "severity": severity, "actor": actor, "message": str(message)[:500],
            "payload_json": payload or {}})
    except Exception:  # noqa: BLE001
        pass
