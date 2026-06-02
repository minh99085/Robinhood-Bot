"""Artifact writers (Phase 11). All content redacted before writing."""

from __future__ import annotations

import csv
import json
from pathlib import Path

try:
    from ..micro_live.secret_runtime import redact, redact_dict
except Exception:  # noqa: BLE001
    def redact(t):  # type: ignore
        return t

    def redact_dict(d):  # type: ignore
        return d


def write_json(path: Path, obj) -> None:
    safe = redact_dict(obj) if isinstance(obj, dict) else obj
    path.write_text(json.dumps(safe, indent=2, default=str))


def write_csv(path: Path, header: list, rows: list) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([redact(str(x)) if x is not None else "" for x in r])
