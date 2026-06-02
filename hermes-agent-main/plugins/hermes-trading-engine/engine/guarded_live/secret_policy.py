"""SecretPolicy (Phase 8). Detects forbidden execution secrets/env, redacts
secret-looking strings, and NEVER reads or prints secret values."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from .schemas import SecretPolicyViolation

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                     re.DOTALL)
_XAI_RE = re.compile(r"\bxai-[A-Za-z0-9]{8,}\b")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_HEX64_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")  # eth private-key-ish

# Env that may legitimately hold secrets (redacted, not forbidden).
_KNOWN_SECRET_ENV = ("XAI_API_KEY", "GROK_API_KEY", "KALSHI_ACCESS_KEY_ID",
                     "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_PASSWORD")


def _hint(value: str) -> str:
    """A non-reversible hint so reports can reference a secret without leaking it."""
    h = hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:8]
    return f"[REDACTED:{h}]"


def redact(text: str) -> str:
    if not text:
        return text
    out = str(text)
    for env in _KNOWN_SECRET_ENV:
        v = os.getenv(env)
        if v:
            out = out.replace(v, "[REDACTED]")
    out = _PEM_RE.sub("[REDACTED_PRIVATE_KEY]", out)
    out = _XAI_RE.sub("[REDACTED]", out)
    out = _SK_RE.sub("[REDACTED]", out)
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _HEX64_RE.sub("[REDACTED_HEX]", out)
    return out


class SecretPolicy:
    def __init__(self, config):
        self.cfg = config

    def detect_forbidden_env(self) -> list[SecretPolicyViolation]:
        out = []
        for pattern in self.cfg.forbidden_env_patterns:
            val = os.getenv(pattern)
            if val not in (None, "", "0", "false", "False"):
                out.append(SecretPolicyViolation(
                    severity="CRITICAL", location=f"env:{pattern}",
                    violation_type="forbidden_env_var", redacted_value=_hint(val),
                    reason=f"forbidden env var {pattern} is set in guarded-live mode"))
        return out

    def scan_payload(self, obj, location: str = "payload") -> list[SecretPolicyViolation]:
        text = obj if isinstance(obj, str) else str(obj)
        out = []
        for rx, vtype in ((_PEM_RE, "private_key_pem"), (_HEX64_RE, "hex_private_key"),
                          (_XAI_RE, "api_key"), (_SK_RE, "api_key")):
            m = rx.search(text)
            if m:
                out.append(SecretPolicyViolation(
                    severity="ERROR", location=location, violation_type=vtype,
                    redacted_value="[REDACTED]",
                    reason=f"secret-looking {vtype} found in {location}"))
        return out

    def check(self) -> tuple[bool, list[SecretPolicyViolation]]:
        violations = self.detect_forbidden_env()
        return (len(violations) == 0), violations
