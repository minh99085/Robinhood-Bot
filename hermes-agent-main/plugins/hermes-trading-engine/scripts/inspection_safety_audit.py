"""Live-execution safety audit for the bot inspection report.

Inspection/reporting ONLY. This module reads `.env`, `.env.example`,
docker-compose.yml, the runtime training status, and API snapshots and decides
whether any *forbidden live/production execution* flag is enabled. It never
changes a flag and never trades.

Classification contributions:

* ``CRITICAL`` — a forbidden live/prod execution flag is enabled, or live
  credential material (private key / api secret) is present.
* ``WARN`` — a protective flag is disabled, or a paper-simulation flag that
  shares live-style naming (e.g. ``HTE_AUTOTRADE``) is enabled. Paper autotrade
  is NOT a live-execution failure.
* ``OK`` — none of the above.
"""

from __future__ import annotations

import re
from typing import Any

_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_FALSY = {"0", "false", "no", "off", "disabled", ""}

# Flags that MUST stay disabled. If any is truthy → CRITICAL_SAFETY_FAIL.
FORBIDDEN_LIVE_FLAGS = (
    "MICRO_LIVE_ENABLED",
    "MICRO_LIVE_ALLOW_PRODUCTION",
    "POLYMARKET_MICRO_LIVE_ENABLED",
    "POLYMARKET_MICRO_LIVE_ALLOW_PROD",
    "KALSHI_MICRO_LIVE_ENABLED",
    "GUARDED_LIVE_ENABLED",
    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
    "PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE",
    "PRODUCTION_REVIEW_ALLOW_DASHBOARD_SUBMIT",
    "PRODUCTION_REVIEW_ALLOW_API_SUBMIT",
    "PRODUCTION_REVIEW_ALLOW_SIZE_INCREASE",
    "BTC_PULSE_LIVE_ENABLED",
    "BTC_AUTOTRADE_ENABLED",
)

# Protective flags that SHOULD stay enabled (truthy). Disabled → WARN, unless the
# matching live gate is also enabled, in which case it escalates to CRITICAL.
PROTECTIVE_FLAGS = (
    "GUARDED_LIVE_DRY_RUN_ONLY",
    "GUARDED_LIVE_BLOCK_SIGNING",
    "GUARDED_LIVE_FORBID_ORDER_ENDPOINTS",
)

# Presence of a non-empty value here means live-credential material is loaded →
# CRITICAL (paper training must not carry signing keys / api secrets).
FORBIDDEN_SECRET_PRESENCE = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_SECRET",
    "KALSHI_TRADING_PRIVATE_KEY_PEM",
    "KALSHI_TRADING_PRIVATE_KEY_PATH",
    "KALSHI_PRIVATE_KEY_PEM",
    "KALSHI_PRIVATE_KEY_PATH",
)

# Paper-simulation flags that share live-style naming. Enabled → WARN only,
# unless live mode is detected.
WARN_PAPER_FLAGS = (
    "HTE_AUTOTRADE",
    "HTE_BTC_PULSE_PAPER_ENABLED",
)


# Modes that are explicitly NON-live (paper/observation/replay/shadow/training).
NON_LIVE_MODES = {
    "paper", "paper_train", "paper_trading", "observe_only", "observe",
    "disabled", "replay", "backtest", "shadow", "shadow_live", "design_only", "",
}
# Tokens that denote real live execution.
LIVE_MODE_TOKENS = ("live", "production", "prod", "real_money", "real-money", "realmoney")


def is_live_mode(mode: Any) -> bool:
    """True only if ``mode`` denotes real LIVE execution.

    Paper/observation/replay/shadow/training modes (incl. ``paper_train``) are
    NOT live. Unknown modes without a live token are treated as non-live to avoid
    false positives. ``shadow_live`` is records-only (no orders) and stays safe.
    """
    m = str(mode or "").strip().lower()
    if "paper" in m or m in NON_LIVE_MODES:
        return False
    if m == "shadow_live":  # shadow == no order submission
        return False
    return any(tok in m for tok in LIVE_MODE_TOKENS)


def is_truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _present(value: Any) -> bool:
    """Non-empty, non-placeholder value present."""
    if value is None:
        return False
    s = str(value).strip().strip('"').strip("'")
    # Treat docker-compose ``${VAR:-}`` interpolation defaults that resolve empty
    # as "not present".
    if s == "" or s in {"${GROK_API_KEY:-}", "${XAI_API_KEY:-}"}:
        return False
    if s.startswith("${") and s.endswith("}") and ":-}" in s:
        # ${VAR:-default}; only "present" if a non-empty default is given.
        default = s[s.find(":-") + 2:-1]
        return default.strip() != ""
    return True


def parse_env_assignments(text: str) -> dict:
    """Parse ``KEY=VALUE`` lines from a `.env`-style body into a dict.

    Ignores comments/blanks; strips surrounding quotes. Last assignment wins.
    """
    out: dict[str, str] = {}
    if not text:
        return out
    kv = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = kv.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        # Drop trailing inline comment for unquoted values.
        elif "#" in val:
            val = val.split("#", 1)[0].rstrip()
        out[key] = val
    return out


def parse_compose_environment(text: str) -> dict:
    """Best-effort extraction of ``KEY: "VALUE"`` pairs from a docker-compose
    ``environment:`` block. Tolerant of formatting; last write wins."""
    out: dict[str, str] = {}
    if not text:
        return out
    kv = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*:\s*(.*)$')
    for line in text.splitlines():
        m = kv.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    return out


def audit(
    env: dict | None = None,
    compose_env: dict | None = None,
    status: dict | None = None,
    api: dict | None = None,
) -> dict:
    """Run the live-execution safety audit.

    Returns a dict with: ``status`` (OK|WARN|CRITICAL), ``critical`` (bool),
    ``live_detected`` (bool), ``findings`` (list), and the raw evaluated flags.
    Inputs are merged with precedence: explicit env > compose_env (only adds
    keys env lacks).
    """
    env = dict(env or {})
    compose_env = dict(compose_env or {})
    status = status or {}
    api = api or {}

    merged: dict[str, str] = {}
    for k, v in compose_env.items():
        merged[k] = v
    for k, v in env.items():
        merged[k] = v  # real .env overrides compose defaults

    findings: list[dict] = []
    critical = False
    warn = False

    # Runtime live detection from status / api.
    live_detected = bool(
        (status.get("safety", {}) or {}).get("live_detected")
        or ((api.get("state", {}) or {}).get("live_detected"))
    )
    # Mode safety: paper / observation / replay / shadow / training modes are all
    # NON-live (e.g. "paper", "paper_train", "observe_only", "disabled"). Only a
    # mode that clearly denotes real live execution is a CRITICAL finding — do NOT
    # flag "paper_train" (the paper training mode) as live.
    mode = str((status.get("mode") or merged.get("HTE_MODE") or "paper")).lower()
    mode_is_live = is_live_mode(mode)
    if mode_is_live:
        live_detected = True
        findings.append({
            "flag": "HTE_MODE", "value": mode, "severity": "CRITICAL",
            "reason": "engine mode denotes LIVE execution (not paper).",
        })
        critical = True

    forbidden_flags_state = {}
    for flag in FORBIDDEN_LIVE_FLAGS:
        val = merged.get(flag)
        enabled = is_truthy(val)
        forbidden_flags_state[flag] = {"value": val, "enabled": enabled}
        if enabled:
            critical = True
            findings.append({
                "flag": flag, "value": val, "severity": "CRITICAL",
                "reason": "forbidden live/production execution flag is ENABLED.",
            })

    guarded_enabled = forbidden_flags_state.get("GUARDED_LIVE_ENABLED", {}).get("enabled", False)
    protective_state = {}
    for flag in PROTECTIVE_FLAGS:
        val = merged.get(flag)
        # Protective flags default to ON when unset in this project.
        present = val is not None
        enabled = is_truthy(val) if present else True
        protective_state[flag] = {"value": val, "enabled": enabled, "present": present}
        if present and not enabled:
            if guarded_enabled:
                critical = True
                findings.append({
                    "flag": flag, "value": val, "severity": "CRITICAL",
                    "reason": "protective flag disabled while GUARDED_LIVE_ENABLED is on.",
                })
            else:
                warn = True
                findings.append({
                    "flag": flag, "value": val, "severity": "WARN",
                    "reason": "protective flag explicitly disabled (guarded-live currently off).",
                })

    secret_presence_state = {}
    for flag in FORBIDDEN_SECRET_PRESENCE:
        val = merged.get(flag)
        present = _present(val)
        secret_presence_state[flag] = {"present": present}
        if present:
            critical = True
            findings.append({
                "flag": flag, "value": "<present>", "severity": "CRITICAL",
                "reason": "live credential material is present in paper config.",
            })

    paper_flag_state = {}
    for flag in WARN_PAPER_FLAGS:
        val = merged.get(flag)
        enabled = is_truthy(val)
        paper_flag_state[flag] = {"value": val, "enabled": enabled}
        if enabled and not live_detected:
            warn = True
            findings.append({
                "flag": flag, "value": val, "severity": "INFO",
                "reason": "paper-simulation autotrade flag enabled (PAPER only — not a live failure).",
            })
        elif enabled and live_detected:
            critical = True
            findings.append({
                "flag": flag, "value": val, "severity": "CRITICAL",
                "reason": "autotrade flag enabled while live mode detected.",
            })

    status_str = "CRITICAL" if critical else ("WARN" if warn else "OK")
    return {
        "status": status_str,
        "critical": critical,
        "warn": warn,
        "live_detected": live_detected,
        "engine_mode": mode,
        "findings": findings,
        "forbidden_live_flags": forbidden_flags_state,
        "protective_flags": protective_state,
        "secret_presence": secret_presence_state,
        "paper_simulation_flags": paper_flag_state,
        "summary": {
            "forbidden_enabled": [f for f, s in forbidden_flags_state.items() if s["enabled"]],
            "credentials_present": [f for f, s in secret_presence_state.items() if s["present"]],
            "protective_disabled": [f for f, s in protective_state.items()
                                    if s["present"] and not s["enabled"]],
        },
    }
