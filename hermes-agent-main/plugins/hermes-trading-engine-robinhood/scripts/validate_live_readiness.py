#!/usr/bin/env python3
"""Validate Robinhood options live-trading readiness (Phase 6 checklist)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.options_readiness import evaluate_readiness


def main() -> int:
    cfg = RobinhoodConfig.from_env()
    data = Path(cfg.data_dir)

    status = None
    st_path = data / "robinhood_status.json"
    if st_path.exists():
        try:
            status = json.loads(st_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    opts = None
    op_path = data / "options_status.json"
    if op_path.exists():
        try:
            opts = json.loads(op_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    report = evaluate_readiness(
        cfg,
        status=status,
        options_status=opts,
        min_paper_scans=cfg.options_min_paper_scans,
    )
    print(json.dumps(report.to_dict(), indent=2))
    if report.ready:
        print("\nREADY: safe to consider RH_LIVE_TRADING_ENABLED=1 after operator review.")
        return 0
    print("\nNOT READY — blockers:", ", ".join(report.blockers) or "(see checks)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
