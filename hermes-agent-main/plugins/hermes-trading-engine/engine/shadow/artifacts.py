"""Artifact writers for shadow sessions (Phase 7)."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    return len(rows)
