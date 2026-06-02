"""Shadow alerts (Phase 7). CRITICAL alerts pause NEW shadow orders; observations
may continue. Persisted best-effort; no external notification dependency.

Quant scope — *Live Trading & Monitoring* + *Compliance*: shadow monitoring is
read-only (no live orders). The risk/quality alerts here are unchanged by the
replay/aggressive upgrade."""

from __future__ import annotations

import time
import uuid
from typing import Optional

SEVERITIES = ("INFO", "WARN", "ERROR", "CRITICAL")
ALERT_TYPES = (
    "venue_degraded", "stale_market_data", "sequence_gap", "tick_size_dirty",
    "research_budget_blocked", "research_validation_failed", "risk_rejection_spike",
    "paper_broker_rejection_spike", "reconciliation_failure", "scheduler_lag",
    "storage_failure", "readiness_gate_failed", "kill_switch_triggered",
    "secret_redaction_test_failed", "live_endpoint_call_attempted",
)


class AlertManager:
    def __init__(self, store=None, session_id: Optional[str] = None):
        self.store = store
        self.session_id = session_id
        self.alerts: list[dict] = []
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def new_orders_allowed(self) -> bool:
        return not self._paused

    def emit(self, severity: str, alert_type: str, message: str,
             payload: Optional[dict] = None) -> dict:
        rec = {
            "alert_id": "al-" + uuid.uuid4().hex[:16], "shadow_session_id": self.session_id,
            "ts_ms": int(time.time() * 1000), "severity": severity, "alert_type": alert_type,
            "message": str(message)[:500], "payload_json": payload or {}, "acknowledged": 0,
        }
        self.alerts.append(rec)
        if severity == "CRITICAL":
            self._paused = True  # pause NEW shadow orders; observations continue
        if self.store is not None:
            try:
                self.store.add_shadow_alert(rec)
            except Exception:  # noqa: BLE001
                pass
        return rec

    def resume(self) -> None:
        self._paused = False
