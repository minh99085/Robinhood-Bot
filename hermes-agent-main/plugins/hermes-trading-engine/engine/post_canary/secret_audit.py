"""SecretAudit (Phase 10). Scans analysis inputs / would-be report content for
secret-like patterns. Any unredacted secret is FAIL (CRITICAL). Everything is
redacted before being written to artifacts."""

from __future__ import annotations

import json
import re

from .schemas import SecretAuditResult, aggregate_status, make_check

try:
    from ..micro_live.secret_runtime import SECRET_ENV_VARS, redact
except Exception:  # noqa: BLE001
    SECRET_ENV_VARS = ()

    def redact(t):  # type: ignore
        return t

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_HEX_KEY_RE = re.compile(r"0x[0-9a-fA-F]{40,}")
_PASSPHRASE_RE = re.compile(r"(api[_-]?secret|passphrase|private[_-]?key)\s*[=:]\s*\S+", re.I)


def _gather_blobs(ctx: dict) -> list[str]:
    blobs = list(ctx.get("scan_blobs") or [])
    a = ctx.get("attempt") or {}
    dry = ctx.get("dry_run_intent") or {}
    for v in (dry.get("venue_payload_json"), dry.get("internal_order_request_json"),
              a.get("error_message_redacted")):
        if v:
            blobs.append(str(v))
    for sv in (ctx.get("secret_violations") or []):
        blobs.append(json.dumps(sv, default=str))
    return blobs


def run(ctx: dict, cfg) -> SecretAuditResult:
    import os
    checks = []
    if not cfg.secret_scan_enabled:
        checks.append(make_check("secret_scan", "NOT_APPLICABLE", "INFO", "scan disabled"))
        return SecretAuditResult(status="NOT_APPLICABLE", checks=checks)

    blobs = _gather_blobs(ctx)
    leaks = 0
    for b in blobs:
        if _PEM_RE.search(b) or _HEX_KEY_RE.search(b) or _PASSPHRASE_RE.search(b):
            leaks += 1
        for env in SECRET_ENV_VARS:
            val = os.getenv(env)
            if val and val in b:
                leaks += 1
    checks.append(make_check("no_secret_in_inputs", "FAIL" if leaks else "PASS", "CRITICAL",
                             reason=f"{leaks} secret-like value(s) found" if leaks else "",
                             observed=leaks))
    # forbidden network calls (from network guard events)
    if cfg.require_no_forbidden_network_calls:
        forbidden = [e for e in (ctx.get("network_guard_events") or [])
                     if e.get("forbidden") or e.get("status") == "forbidden"]
        checks.append(make_check("no_forbidden_network_call",
                                 "FAIL" if forbidden else "PASS", "CRITICAL",
                                 observed=len(forbidden)))
    return SecretAuditResult(status=aggregate_status(checks), checks=checks,
                             secret_leak_count=leaks, redaction_count=len(blobs),
                             violations=[c.check_name for c in checks if c.status == "FAIL"])
