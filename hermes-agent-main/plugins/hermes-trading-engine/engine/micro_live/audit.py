"""Micro-live audit log with a tamper-evident chain hash (Phase 9)."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from .secret_runtime import redact

_GENESIS = "0" * 16


def chain_hash(prev_hash: Optional[str], event: dict) -> str:
    blob = (prev_hash or _GENESIS) + json.dumps(event, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def write_audit(store, *, event_type: str, severity: str = "INFO", actor: Optional[str] = None,
                canary_plan_id: Optional[str] = None, live_order_attempt_id: Optional[str] = None,
                state: Optional[str] = None, message: str = "",
                payload: Optional[dict] = None) -> str:
    event = {"ts_ms": int(time.time() * 1000), "event_type": event_type, "severity": severity,
             "actor": redact(actor) if actor else None, "canary_plan_id": canary_plan_id,
             "live_order_attempt_id": live_order_attempt_id, "state": state,
             "message": redact(str(message))[:500], "payload_json": payload or {}}
    prev = None
    if store is not None:
        try:
            prev = store.get_last_micro_live_audit_hash()
        except Exception:  # noqa: BLE001
            prev = None
    h = chain_hash(prev, event)
    event["audit_chain_hash"] = h
    if store is not None:
        try:
            store.add_micro_live_audit_event(event)
        except Exception:  # noqa: BLE001 — audit failure must not crash
            pass
    return h
