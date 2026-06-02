#!/usr/bin/env python3
"""Export a production-review dossier (Phase 11). Read-only; copies the redacted
artifact bundle and writes a dossier index."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from _production_review_common import default_db


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export production-review dossier (read-only)")
    ap.add_argument("--review-id", default=None)
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--out", default="production_review_dossier")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.storage import Store
    store = Store(Path(args.db or default_db()))
    runs = store.get_production_review_runs(200)
    if args.review_id:
        runs = [r for r in runs if r.get("review_id") == args.review_id]
    run = runs[0] if runs else None
    if not run:
        print("no production-review run found")
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    src = run.get("report_path")
    copied = []
    if src and Path(src).parent.exists():
        for f in Path(src).parent.glob("*"):
            if f.is_file():
                shutil.copy2(f, out / f.name)
                copied.append(f.name)
    # always (re)write the index
    (out / "production_dossier_index.md").write_text(
        "# Production Dossier Index\n\n"
        f"- review_id: {run.get('review_id')}\n"
        f"- recommendation: {run.get('recommendation')}\n"
        f"- status: {run.get('status')}\n\n"
        f"Files: {', '.join(sorted(copied)) or 'n/a'}\n\n"
        "_Production execution remains UNIMPLEMENTED in Phase 11. No size increase or "
        "autonomous live trading is approved._\n")
    print(f"exported dossier for {run.get('review_id')} to {out}/ "
          f"({len(copied)} files + index)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
