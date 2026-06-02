"""Two-person manual approval workflow (Phase 8).

Internal audit workflow (NOT cryptographic signing). Approvals can only reach
APPROVED_DRY_RUN_ONLY — never live. Grok/automation cannot approve. Approvals
expire, dedupe by approver, and are invalidated by config changes.
"""

from __future__ import annotations

import time
from typing import Optional

from .config import GuardedLiveConfig
from .schemas import ApprovalBatch, ManualApproval

AUTOMATED_ACTORS = frozenset({
    "grok", "research_engine", "strategy", "bot", "auto", "policy", "automation",
    "system", "agent"})
CONFIRMATION_REQUIRED_SUBSTR = "dry-run only"


class ApprovalWorkflow:
    def __init__(self, store=None, config: Optional[GuardedLiveConfig] = None):
        self.store = store
        self.cfg = config or GuardedLiveConfig()

    def create_batch(self, *, readiness_report_id: str, config_hash: str,
                     now_ms: Optional[int] = None) -> ApprovalBatch:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        batch = ApprovalBatch(
            readiness_report_id=readiness_report_id, config_hash=config_hash,
            required_approvals=self.cfg.required_approvals, created_ts_ms=now,
            expires_ts_ms=now + self.cfg.approval_expiry_minutes * 60_000)
        if self.store is not None:
            self.store.upsert_approval_batch(batch.record())
        return batch

    def approve(self, batch: ApprovalBatch, *, approver_id: str, role: str,
                confirmation_text: str, readiness_report_id: str, config_hash: str,
                approval_reason: str = "", now_ms: Optional[int] = None) -> tuple[bool, object]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if (approver_id or "").strip().lower() in AUTOMATED_ACTORS:
            return False, "automated_actor_forbidden"
        if self.cfg.require_typed_confirmation and \
                CONFIRMATION_REQUIRED_SUBSTR not in (confirmation_text or "").lower():
            return False, "missing_typed_confirmation"
        if config_hash != batch.config_hash:
            batch.status = "INVALIDATED"
            self._save_batch(batch)
            return False, "config_hash_mismatch_invalidated"
        if readiness_report_id != batch.readiness_report_id:
            return False, "readiness_report_mismatch"
        if now > batch.expires_ts_ms:
            batch.status = "EXPIRED"
            self._save_batch(batch)
            return False, "approval_batch_expired"
        if batch.status not in ("PENDING", "APPROVED_DRY_RUN_ONLY"):
            return False, f"batch_{batch.status.lower()}"

        existing = self._active_approvers(batch.approval_batch_id)
        appr = ManualApproval(
            approval_batch_id=batch.approval_batch_id, ts_ms=now, approver_id=approver_id,
            role=role, readiness_report_id=readiness_report_id, config_hash=config_hash,
            risk_limits_hash=self.cfg.risk_limits_hash(), approval_reason=approval_reason,
            confirmation_text=confirmation_text,
            expires_ts_ms=now + self.cfg.approval_expiry_minutes * 60_000, status="ACTIVE")
        if approver_id in existing:
            # duplicate approver counts once — record but do not increment
            if self.store is not None:
                self.store.add_manual_approval(appr.record())
            return True, appr
        if self.store is not None:
            self.store.add_manual_approval(appr.record())
        existing.add(approver_id)
        batch.valid_approvals = len(existing)
        if batch.valid_approvals >= batch.required_approvals:
            batch.status = "APPROVED_DRY_RUN_ONLY"  # never "live"
        self._save_batch(batch)
        return True, appr

    def revoke(self, batch: ApprovalBatch) -> None:
        batch.status = "REVOKED"
        self._save_batch(batch)

    def _active_approvers(self, batch_id: str) -> set:
        if self.store is None:
            return set()
        rows = self.store.get_manual_approvals(batch_id)
        return {r["approver_id"] for r in rows if r.get("status") == "ACTIVE"}

    def _save_batch(self, batch: ApprovalBatch) -> None:
        if self.store is not None:
            self.store.upsert_approval_batch(batch.record())
