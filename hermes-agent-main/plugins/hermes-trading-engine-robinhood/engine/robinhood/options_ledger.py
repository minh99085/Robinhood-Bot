"""Persist options scan / intent / order history for operator review."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def ledger_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "options_ledger.json"


def status_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "options_status.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_ledger(data_dir: str | Path) -> dict[str, Any]:
    path = ledger_path(data_dir)
    if not path.exists():
        return {"events": [], "last_ts": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"events": [], "last_ts": None}


def append_event(data_dir: str | Path, event: dict[str, Any], *, max_events: int = 500) -> None:
    ledger = load_ledger(data_dir)
    events: list[dict[str, Any]] = list(ledger.get("events") or [])
    event = dict(event)
    event.setdefault("ts", time.time())
    events.append(event)
    if len(events) > max_events:
        events = events[-max_events:]
    _atomic_write(
        ledger_path(data_dir),
        {"events": events, "last_ts": event["ts"]},
    )


def write_status(data_dir: str | Path, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["ts"] = time.time()
    _atomic_write(status_path(data_dir), payload)
