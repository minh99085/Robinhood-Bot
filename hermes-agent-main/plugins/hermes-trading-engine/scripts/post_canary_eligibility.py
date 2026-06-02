#!/usr/bin/env python3
"""Show canary eligibility (Phase 10). Size increase is ALWAYS NO."""

from __future__ import annotations

import argparse
import json
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
    ap = argparse.ArgumentParser(description="Post-canary eligibility (no scaling)")
    ap.add_argument("--venue", default="kalshi")
    ap.add_argument("--environment", default="demo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.post_canary import PostCanaryConfig, compute_eligibility
    from engine.storage import Store
    store = Store(Path(args.db or _default_db()))
    elig = compute_eligibility(store, PostCanaryConfig.from_env(), args.venue, args.environment)
    out = elig.model_dump()
    out["eligible_size_increase"] = False
    out["eligible_autonomous_live"] = False
    out["production_execution"] = "NOT_IMPLEMENTED"
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"venue={elig.venue} env={elig.environment}")
        print(f"  total={elig.total_canaries} clean={elig.clean_canaries} "
              f"failed={elig.failed_canaries} unresolved={elig.unresolved_canaries}")
        print(f"  repeat_demo_same_size={elig.eligible_repeat_demo_same_size} "
              f"production_design_review={elig.eligible_production_design_review}")
        print("  size_increase=NO  autonomous_live=NO  production_execution=NOT_IMPLEMENTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
