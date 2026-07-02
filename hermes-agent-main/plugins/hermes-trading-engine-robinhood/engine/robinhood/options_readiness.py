"""Live-trading readiness checks (Phase 6)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.constants import OPTIONS_TOOLS
from engine.robinhood.mcp_catalog import load_catalog
from engine.robinhood.oauth_storage import FileTokenStorage
from engine.robinhood.options_ledger import load_ledger


@dataclass
class ReadinessCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class ReadinessReport:
    ready: bool
    checks: list[ReadinessCheck] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "blockers": self.blockers,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


def _active_bias_count(config: RobinhoodConfig) -> int:
    return sum(1 for sym in config.options_watchlist if config.bias_for(sym) != "none")


def evaluate_readiness(
    config: RobinhoodConfig,
    *,
    status: dict[str, Any] | None = None,
    options_status: dict[str, Any] | None = None,
    min_paper_scans: int = 1,
    paper_scan_max_age_s: float = 86400.0,
) -> ReadinessReport:
    """Static + file-based readiness (no network). Pass MCP status blobs when available."""
    checks: list[ReadinessCheck] = []
    blockers: list[str] = []

    storage = FileTokenStorage(config.data_dir)
    has_tokens = storage.has_tokens()
    checks.append(
        ReadinessCheck("oauth_tokens", has_tokens, "tokens on disk" if has_tokens else "missing")
    )
    if not has_tokens:
        blockers.append("oauth_tokens")

    if config.live_trading_enabled:
        checks.append(
            ReadinessCheck(
                "live_flag",
                False,
                "RH_LIVE_TRADING_ENABLED=1 already set — verify intentional",
            )
        )
    else:
        checks.append(ReadinessCheck("live_flag", True, "live trading off (safe default)"))

    bias_n = _active_bias_count(config)
    bias_ok = bias_n > 0
    checks.append(
        ReadinessCheck(
            "manual_bias",
            bias_ok,
            f"{bias_n} symbols with call/put bias" if bias_ok else "set RH_OPTIONS_BIAS or per-symbol",
        )
    )
    if not bias_ok:
        blockers.append("manual_bias")

    catalog = load_catalog(config.data_dir)
    if catalog:
        names = {t.get("name") for t in catalog.get("tools") or []}
        missing = sorted(OPTIONS_TOOLS - {n for n in names if n})
        tools_ok = not missing
        checks.append(
            ReadinessCheck(
                "options_mcp_tools",
                tools_ok,
                "all options tools in catalog" if tools_ok else f"missing: {', '.join(missing)}",
            )
        )
        if not tools_ok:
            blockers.append("options_mcp_tools")
    else:
        checks.append(
            ReadinessCheck(
                "options_mcp_tools",
                False,
                "no mcp_tool_catalog.json — connect MCP first",
            )
        )
        blockers.append("options_mcp_tools")

    connected = bool(status and status.get("connected"))
    checks.append(
        ReadinessCheck(
            "mcp_connected",
            connected,
            "MCP session up" if connected else "agent not connected",
        )
    )
    if not connected:
        blockers.append("mcp_connected")

    opts_ok = bool(options_status and options_status.get("available"))
    checks.append(
        ReadinessCheck(
            "options_loop_ran",
            opts_ok,
            "options tick completed" if opts_ok else "wait for first options scan",
        )
    )
    if not opts_ok:
        blockers.append("options_loop_ran")

    ledger = load_ledger(config.data_dir)
    now = time.time()
    scans = [
        e
        for e in ledger.get("events") or []
        if e.get("type") == "scan_complete" and (now - float(e.get("ts") or 0)) <= paper_scan_max_age_s
    ]
    paper_ok = len(scans) >= min_paper_scans
    checks.append(
        ReadinessCheck(
            "paper_soak",
            paper_ok,
            f"{len(scans)} scan(s) in last {int(paper_scan_max_age_s)}s"
            if paper_ok
            else f"need >={min_paper_scans} scan_complete events",
        )
    )
    if not paper_ok:
        blockers.append("paper_soak")

    can_enable_live = all(c.passed for c in checks if c.name != "live_flag") and not config.live_trading_enabled

    return ReadinessReport(ready=can_enable_live, checks=checks, blockers=blockers)


def write_readiness_report(data_dir: str | Path, report: ReadinessReport) -> Path:
    path = Path(data_dir) / "options_readiness.json"
    payload = report.to_dict()
    payload["ts"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
