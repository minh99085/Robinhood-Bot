#!/usr/bin/env python3
"""Artifact-write smoke test (PAPER ONLY).

Proves the resolved metrics / reports / training-data directories are REAL and
WRITABLE: resolves the absolute dirs (honoring POLYMARKET_*_DIR env), creates
them, writes one test metric + report + event file, verifies each exists and is
non-empty, then cleans up the test files. Exit 0 = writes work; non-zero = blocked.

    python scripts/validate_artifact_writes.py [--data-dir /data]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.training.artifact_dirs import (resolve_artifact_dirs, ensure_dirs,  # noqa: E402
                                           startup_report)

_TEST_PREFIX = "_artifact_write_smoketest"


def run(data_dir: str) -> dict:
    art = resolve_artifact_dirs(data_dir)
    ensure_dirs(art)
    checks: list = []
    created: list = []

    def _check(name: str, p: Path, body: str) -> None:
        ok = False
        non_empty = False
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            created.append(p)
            ok = p.exists()
            non_empty = ok and p.stat().st_size > 0
        except Exception as exc:  # noqa: BLE001
            checks.append({"target": name, "path": str(p), "ok": False,
                           "error": f"{type(exc).__name__}: {exc}"})
            return
        checks.append({"target": name, "path": str(p), "ok": bool(ok),
                       "non_empty": bool(non_empty), "size": p.stat().st_size if ok else -1})

    _check("metric", Path(art["metrics_dir"]) / f"{_TEST_PREFIX}.json",
           json.dumps({"smoketest": True, "ts": "now"}))
    _check("report", Path(art["reports_dir"]) / f"{_TEST_PREFIX}.md",
           "# artifact write smoke test\nok\n")
    _check("event", Path(art["training_data_dir"]) / f"{_TEST_PREFIX}.jsonl",
           json.dumps({"event_type": "smoketest"}) + "\n")

    # cleanup
    cleaned = []
    for p in created:
        try:
            p.unlink()
            cleaned.append(str(p))
        except Exception:  # noqa: BLE001
            pass
    ok = all(c.get("ok") and c.get("non_empty") for c in checks)
    return {"ok": ok, "dirs": {k: str(v) for k, v in art.items()},
            "checks": checks, "cleaned": cleaned, "startup_report": startup_report(art)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate the trainer artifact dirs are writable.")
    ap.add_argument("--data-dir", default=os.environ.get("HTE_DATA_DIR", "/data"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = run(args.data_dir)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(res["startup_report"])
        for c in res["checks"]:
            status = "OK" if (c.get("ok") and c.get("non_empty")) else "FAIL"
            print(f"  [{status}] {c['target']}: {c['path']} "
                  f"size={c.get('size')} {c.get('error', '')}".rstrip())
        print(f"\nARTIFACT WRITES OK: {res['ok']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
