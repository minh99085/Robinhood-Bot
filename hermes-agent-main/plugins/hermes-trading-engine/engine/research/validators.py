"""Strict validation + secret redaction for Grok research output.

Grok output must match GrokProbabilityOutput exactly. Any field implying order
execution or sizing is stripped (extra='ignore') and flagged. Invalid output
never becomes a tradeable estimate.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .schemas import GrokProbabilityOutput

# Keys that would indicate Grok trying to execute or size a trade — forbidden.
FORBIDDEN_EXECUTION_KEYS = frozenset({
    "order_size", "size", "quantity", "qty", "order_qty", "notional", "stake",
    "place_order", "submit_order", "submit", "cancel_order", "cancel", "execute",
    "leverage", "position_size", "amount_usd", "dollar_amount",
})

_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_XAI_KEY_RE = re.compile(r"\bxai-[A-Za-z0-9]{8,}\b")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")


def redact(text: str) -> str:
    """Remove API keys / bearer tokens from a string. Never log raw secrets."""
    if not text:
        return text
    out = str(text)
    for env in ("XAI_API_KEY", "GROK_API_KEY"):
        val = os.getenv(env)
        if val:
            out = out.replace(val, "[REDACTED]")
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _XAI_KEY_RE.sub("[REDACTED]", out)
    out = _SK_KEY_RE.sub("[REDACTED]", out)
    return out


def forbidden_execution_keys(raw) -> list[str]:
    """Return any execution/sizing keys present at the top level of raw output."""
    if not isinstance(raw, dict):
        return []
    return sorted(k for k in raw.keys() if str(k).lower() in FORBIDDEN_EXECUTION_KEYS)


def strip_forbidden(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if str(k).lower() not in FORBIDDEN_EXECUTION_KEYS}


def validate_probability_output(raw) -> Optional[GrokProbabilityOutput]:
    """Parse raw output into GrokProbabilityOutput. Returns None if invalid
    (e.g. probability out of [0,1], wrong types, missing required fields)."""
    if not isinstance(raw, dict):
        return None
    try:
        return GrokProbabilityOutput.model_validate(strip_forbidden(raw))
    except Exception:  # noqa: BLE001 — any validation failure -> not tradeable
        return None


def evidence_sufficient(output: GrokProbabilityOutput, *, min_count: int,
                        min_score: float, evidence_score: float) -> bool:
    if not output.evidence or len(output.evidence) < min_count:
        return False
    return evidence_score >= min_score
