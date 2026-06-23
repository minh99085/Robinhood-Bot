#!/usr/bin/env python3
"""Build the COMPLETE human-readable BTC-pulse performance report from the bot's JSON artifacts.

Reads ``btc_pulse_light_report.json`` (+ status + ledger) from the data dir and writes a full
``report.md`` covering every performance dimension so an external reviewer (ChatGPT / Grok) can
inspect the bot end-to-end. PAPER ONLY; report-only. Usage:

    python scripts/build_full_report.py [DATA_DIR] [OUT_MD]

Defaults: DATA_DIR=$HTE_DATA_DIR or /data, OUT_MD=<DATA_DIR>/report.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else (os.getenv("HTE_DATA_DIR") or "/data"))
    out_md = Path(sys.argv[2]) if len(sys.argv) > 2 else (data_dir / "report.md")
    # importable whether run from the plugin root or with the package on sys.path
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    from engine.pulse.reporting import build_full_report_md
    light = _load(data_dir / "btc_pulse_light_report.json")
    status = _load(data_dir / "btc_pulse_status.json")
    ledger = _load(data_dir / "btc_pulse_ledger.json")
    md = build_full_report_md(light, status, ledger)
    out_md.write_text(md, encoding="utf-8")
    print("wrote %s (%d bytes)" % (out_md, len(md)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
