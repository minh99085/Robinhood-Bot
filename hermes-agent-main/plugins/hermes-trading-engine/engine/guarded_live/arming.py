"""ArmingTokenManager (Phase 8).

Short-lived DRY-RUN-ONLY arming tokens. The plain token is shown once; only its
hash is stored. A token cannot enable real execution, expires quickly, is bound
to (config_hash, approval batch, readiness report), and is invalidated by the
kill switch / config change / conformance failure. Verification is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from .config import GuardedLiveConfig
from .errors import ArmingError
from .schemas import ArmingTokenRecord


def _hash(plain: str) -> str:
    return hashlib.sha256((plain or "").encode("utf-8")).hexdigest()


class ArmingTokenManager:
    def __init__(self, store=None, config: Optional[GuardedLiveConfig] = None):
        self.store = store
        self.cfg = config or GuardedLiveConfig()

    def issue(self, batch, *, now_ms: Optional[int] = None) -> tuple[str, ArmingTokenRecord]:
        if getattr(batch, "status", None) != "APPROVED_DRY_RUN_ONLY":
            raise ArmingError("approval batch is not APPROVED_DRY_RUN_ONLY")
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        plain = secrets.token_urlsafe(32)
        rec = ArmingTokenRecord(
            token_hash=_hash(plain), approval_batch_id=batch.approval_batch_id,
            readiness_report_id=batch.readiness_report_id, config_hash=batch.config_hash,
            mode="dry_run_only",  # fixed: never live
            created_ts_ms=now, expires_ts_ms=now + self.cfg.arming_expiry_minutes * 60_000,
            status="ACTIVE")
        if self.store is not None:
            self.store.add_arming_token(rec.record())
        return plain, rec

    def verify(self, plain: str, *, config_hash: Optional[str] = None,
               conformance_ok: bool = True, now_ms: Optional[int] = None) -> tuple[bool, str]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if self.cfg.kill_switch_active():
            return False, "kill_switch_active"
        if not conformance_ok:
            return False, "conformance_failed"
        h = _hash(plain)
        rec = self.store.get_arming_token_by_hash(h) if self.store is not None else None
        if rec is None:
            return False, "unknown_token"
        # constant-time compare against the stored hash
        if not hmac.compare_digest(h, rec.get("token_hash", "")):
            return False, "token_mismatch"
        if rec.get("status") in ("REVOKED", "INVALIDATED"):
            return False, rec["status"].lower()
        if now > int(rec.get("expires_ts_ms") or 0):
            self.store.update_arming_token(rec["arming_token_id"], {"status": "EXPIRED"})
            return False, "expired"
        if config_hash is not None and config_hash != rec.get("config_hash"):
            return False, "config_hash_mismatch"
        if rec.get("mode") != "dry_run_only":
            return False, "mode_not_dry_run_only"
        self.store.update_arming_token(rec["arming_token_id"],
                                       {"status": "USED", "used_ts_ms": now})
        return True, "ok"

    def revoke(self, arming_token_id: str) -> None:
        if self.store is not None:
            self.store.update_arming_token(arming_token_id, {"status": "REVOKED",
                                                             "revoked_ts_ms": int(time.time() * 1000)})
