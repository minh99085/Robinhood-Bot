"""Persist Robinhood MCP tool catalog (names + input schemas) for operator inspection."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def catalog_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "mcp_tool_catalog.json"


def save_catalog(
    data_dir: str | Path,
    *,
    tools: list[dict[str, Any]],
    mcp_url: str,
) -> Path:
    path = catalog_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.time(),
        "mcp_url": mcp_url,
        "tool_count": len(tools),
        "tools": tools,
        "options_tools": sorted(
            t["name"] for t in tools if "option" in t.get("name", "").lower()
        ),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_catalog(data_dir: str | Path) -> dict[str, Any] | None:
    path = catalog_path(data_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
