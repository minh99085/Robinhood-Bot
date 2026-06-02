#!/usr/bin/env python3
"""Stop the Polymarket PAPER training loop safely and generate a final report.

Writes a stop sentinel that the training loop honours, keeps all data, and emits
a report from the last persisted status. PAPER ONLY — nothing here can place,
cancel, or affect a real order (none exist).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.training.reports import write_reports  # noqa: E402


def _data_dir() -> Path:
    try:
        from engine.config import Settings
        return Path(Settings().data_dir)
    except Exception:  # noqa: BLE001
        import os
        return Path(os.getenv("HTE_DATA_DIR") or ".")


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stop the Polymarket PAPER training loop and report.")
    ap.add_argument("--data-dir", default=None, help="data dir (defaults to Settings().data_dir)")
    ap.add_argument("--no-report", action="store_true", help="skip writing a final report")
    args = ap.parse_args(argv)

    dd = Path(args.data_dir) if args.data_dir else _data_dir()
    dd.mkdir(parents=True, exist_ok=True)
    stop_path = dd / "polymarket_training.stop"
    stop_path.write_text("stop", encoding="utf-8")
    print(f"stop sentinel written: {stop_path}")

    status_path = dd / "polymarket_training.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        print(f"last status: tick={status.get('tick')} "
              f"equity={status.get('pnl', {}).get('equity')} "
              f"closed={status.get('pnl', {}).get('trades_closed')}")
        if not args.no_report:
            out = write_reports(status=status)
            print(f"final report: {out['run_dir']} · recommendation={out['recommendation']}")
    else:
        print("no persisted training status found (nothing running yet).")
    print("training loop will stop on its next tick. Data preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
