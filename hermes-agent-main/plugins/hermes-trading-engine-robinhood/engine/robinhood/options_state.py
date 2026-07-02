"""Per-symbol cooldown + chain snapshot cache on disk."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "options_state.json"


def _cache_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "options_chain_cache.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(data_dir: str | Path) -> dict[str, Any]:
    path = _state_path(data_dir)
    if not path.exists():
        return {"symbol_last_action": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"symbol_last_action": {}}


def record_symbol_action(
    data_dir: str | Path,
    symbol: str,
    action: str,
    *,
    instrument_id: str | None = None,
) -> None:
    state = load_state(data_dir)
    actions: dict[str, Any] = dict(state.get("symbol_last_action") or {})
    actions[symbol.upper()] = {
        "ts": time.time(),
        "action": action,
        "instrument_id": instrument_id,
    }
    _atomic_write(_state_path(data_dir), {"symbol_last_action": actions})


def symbol_in_cooldown(data_dir: str | Path, symbol: str, cooldown_s: float) -> bool:
    if cooldown_s <= 0:
        return False
    state = load_state(data_dir)
    row = (state.get("symbol_last_action") or {}).get(symbol.upper())
    if not row:
        return False
    ts = float(row.get("ts") or 0)
    return (time.time() - ts) < cooldown_s


def save_chain_snapshot(data_dir: str | Path, symbol: str, snapshot: dict[str, Any]) -> None:
    path = _cache_path(data_dir)
    cache: dict[str, Any] = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    symbols = dict(cache.get("symbols") or {})
    symbols[symbol.upper()] = {**snapshot, "cached_ts": time.time()}
    _atomic_write(path, {"symbols": symbols})


def load_chain_snapshot(data_dir: str | Path, symbol: str) -> dict[str, Any] | None:
    path = _cache_path(data_dir)
    if not path.exists():
        return None
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return (cache.get("symbols") or {}).get(symbol.upper())
