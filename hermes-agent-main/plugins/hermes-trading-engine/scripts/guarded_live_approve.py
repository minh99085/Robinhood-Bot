#!/usr/bin/env python3
"""Record a manual guarded-live approval (Phase 8). Human-only; dry-run-only;
never enables live execution. Requires a typed confirmation string."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _default_db() -> str:
    try:
        from engine.config import settings
        return str(settings.db_path)
    except Exception:  # noqa: BLE001
        return os.getenv("HTE_DB_PATH", "trading.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record a manual guarded-live approval (dry-run only)")
    ap.add_argument("--approval-batch-id", required=True)
    ap.add_argument("--approver-id", required=True)
    ap.add_argument("--role", required=True)
    ap.add_argument("--confirm", required=True,
                    help='typed confirmation, must contain "DRY-RUN ONLY"')
    ap.add_argument("--reason", default="")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.guarded_live import ApprovalWorkflow, GuardedLiveConfig
    from engine.guarded_live.schemas import ApprovalBatch
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    cfg = GuardedLiveConfig.from_env()
    row = store.get_approval_batch(args.approval_batch_id)
    if row is None:
        print("unknown approval batch")
        return 2
    batch = ApprovalBatch(**{k: row[k] for k in (
        "approval_batch_id", "readiness_report_id", "config_hash", "required_approvals",
        "valid_approvals", "status", "created_ts_ms", "expires_ts_ms") if k in row})
    ok, res = ApprovalWorkflow(store, cfg).approve(
        batch, approver_id=args.approver_id, role=args.role, confirmation_text=args.confirm,
        readiness_report_id=batch.readiness_report_id, config_hash=cfg.config_hash(),
        approval_reason=args.reason)
    print(f"accepted={ok} detail={res if isinstance(res, str) else 'approved'} "
          f"batch_status={batch.status}")
    print("DRY-RUN ONLY — this does not enable live order submission.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
