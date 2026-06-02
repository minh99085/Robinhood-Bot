#!/usr/bin/env python3
"""Export post-canary analysis dataset to CSVs (Phase 10). Read-only."""

from __future__ import annotations

import argparse
import csv
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


def _dump(path: Path, rows: list[dict]) -> int:
    cols = sorted({k for r in rows for k in r.keys()}) if rows else []
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export post-canary dataset (read-only)")
    ap.add_argument("--out", default="post_canary_dataset")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    store = Store(Path(args.db or _default_db()))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    analyses = store.get_post_canary_analyses(100000)
    n_an = _dump(out / "analyses.csv", analyses)
    checks = []
    recon = []
    eq = []
    mk = []
    for a in analyses:
        aid = a.get("analysis_id")
        checks.extend(store.get_post_canary_audit_checks(aid))
        r = store.get_post_canary_rows("post_canary_reconciliation_audits", 100000)
        recon = [x for x in r]
        eq = store.get_post_canary_rows("post_canary_execution_quality", 100000)
        mk.extend(store.get_post_canary_markout(aid))
    _dump(out / "audit_checks.csv", checks)
    _dump(out / "reconciliation.csv", recon)
    _dump(out / "execution_quality.csv", eq)
    _dump(out / "markout.csv", mk)
    _dump(out / "eligibility.csv", store.get_post_canary_eligibility(100000))
    print(f"exported {n_an} analyses to {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
