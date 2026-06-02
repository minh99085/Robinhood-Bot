#!/usr/bin/env python3
"""Issue a DRY-RUN-ONLY arming token (Phase 8). This can NEVER enable live
execution. The plain token is shown once."""

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
    ap = argparse.ArgumentParser(description="Issue a DRY-RUN-ONLY arming token (never live)")
    ap.add_argument("--approval-batch-id", required=True)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.guarded_live import ArmingTokenManager, GuardedLiveConfig
    from engine.guarded_live.errors import ArmingError
    from engine.guarded_live.schemas import ApprovalBatch
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    row = store.get_approval_batch(args.approval_batch_id)
    if row is None:
        print("unknown approval batch")
        return 2
    batch = ApprovalBatch(**{k: row[k] for k in (
        "approval_batch_id", "readiness_report_id", "config_hash", "required_approvals",
        "valid_approvals", "status", "created_ts_ms", "expires_ts_ms") if k in row})
    try:
        plain, rec = ArmingTokenManager(store, GuardedLiveConfig.from_env()).issue(batch)
    except ArmingError as e:
        print(f"cannot arm: {e}")
        return 1
    print("DRY-RUN ONLY arming token (shown ONCE; cannot enable live execution):")
    print(f"  arming_token_id: {rec.arming_token_id}")
    print(f"  arming_token:    {plain}")
    print(f"  mode:            {rec.mode}")
    print(f"  expires_ts_ms:   {rec.expires_ts_ms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
