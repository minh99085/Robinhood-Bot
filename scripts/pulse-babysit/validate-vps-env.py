#!/usr/bin/env python3
"""Validate VPS .env has all secrets + loop-arch keys (run on VPS or via ssh)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ENV_PATH = Path("/opt/Grok-Bot-2/hermes-agent-main/plugins/hermes-trading-engine/.env")

REQUIRED_NONEMPTY = (
    "XAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TRADINGVIEW_WEBHOOK_SECRET",
)

REQUIRED_VALUES = {
    "PULSE_GROK_DECIDER_MODE": "follow",
    "PULSE_GROK_DECIDER_EXPLORE_RATE": "0",
    "PULSE_GROK_DECIDER_MIN_CONFIDENCE": "0.62",
    "PULSE_VERIFIER_ENABLED": "1",
    "PULSE_TRADINGVIEW_SIGNAL_GATE": "0",
    "PULSE_TV_MTF_REQUIRE_CONFIRM": "0",
    "PULSE_TV_DOWN_BIAS_GATE": "1",
    "PULSE_MAX_OPEN_LAG_S": "45",
}


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    env = _parse_env(ENV_PATH)
    issues: list[dict] = []

    if not env:
        issues.append({"code": "env_missing", "severity": "P0", "detail": str(ENV_PATH)})

    for key in REQUIRED_NONEMPTY:
        if not env.get(key):
            issues.append({
                "code": "secret_missing",
                "severity": "P0",
                "detail": key,
                "hint": f"set {key} in {ENV_PATH} and recreate hermes-training",
            })

    for key, want in REQUIRED_VALUES.items():
        got = env.get(key)
        if got != want:
            issues.append({
                "code": "config_drift",
                "severity": "P1" if key.startswith("PULSE_TV") else "P0",
                "detail": f"{key}={got!r} want={want!r}",
                "hint": "run scripts/apply-loop-arch-env.py on VPS",
            })

    healthy = len(issues) == 0
    out = {
        "healthy": healthy,
        "verdict": "healthy" if healthy else "blocked",
        "issues": issues,
        "env_path": str(ENV_PATH),
        "keys_present": sorted(env.keys()),
    }
    print(json.dumps(out, indent=2))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())