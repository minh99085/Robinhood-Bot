#!/usr/bin/env python3
"""Generate a guarded-live design report (Phase 8). Always states no live orders
and that real execution remains disabled."""

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
    ap = argparse.ArgumentParser(description="Generate a guarded-live design report")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--conformance-run-id", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.guarded_live import ConformanceHarness, GuardedLiveConfig, write_report
    from engine.guarded_live.state_machine import GuardedLiveStateMachine
    from engine.storage import Store

    store = Store(Path(args.db or _default_db()))
    cfg = GuardedLiveConfig.from_env()
    conf = None
    if args.conformance_run_id:
        conf = store.get_conformance_run(args.conformance_run_id)
    else:
        conf = ConformanceHarness(store=store, config=cfg).run().record()
    prechecks = store.get_guarded_live_prechecks(1)
    precheck = prechecks[0] if prechecks else None
    state = (store.get_guarded_live_state(1) or [{"state": "DESIGN_ONLY"}])[0]["state"]
    blockers = ["shadow readiness report", "conformance pass", "manual approvals",
                "secret-policy pass", "dry-run mapper validation", "risk-envelope validation",
                "kill-switch test", "reconciliation clean", "manual_review_of_guarded_live_design"]
    out = write_report(store, cfg, state=state, precheck=precheck, conformance=conf,
                       blockers=blockers)
    print(f"report: {out}")
    print(f"state: {state}")
    print("No live orders were submitted. Real execution remains DISABLED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
