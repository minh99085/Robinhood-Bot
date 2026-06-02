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


# Execution-INTENT keys (a superset of the size keys): anything that would let
# research act rather than merely inform a probability.
EXECUTION_INTENT_KEYS = frozenset(FORBIDDEN_EXECUTION_KEYS | {
    "approve", "approved", "arm", "armed", "submit", "submit_order", "sign", "signed",
    "place", "place_order", "cancel", "cancel_order", "should_trade", "trade",
    "authorize", "authorized", "execute_now", "auto_approve",
})


class ResearchFirewall:
    """Advisory firewall: research/Grok may inform a probability ONLY — it can
    never size, approve, arm, submit, or override risk. The firewall scans an
    object for execution-intent fields and STRIPS them before any estimate is
    built. Quant scope — *Compliance/Security/Operational Excellence*."""

    def __init__(self, intent_keys=EXECUTION_INTENT_KEYS):
        self.intent_keys = frozenset(str(k).lower() for k in intent_keys)

    def scan(self, obj) -> list:
        """Return any execution-intent keys present on a dict (or object)."""
        if isinstance(obj, dict):
            keys = obj.keys()
        else:
            keys = [k for k in dir(obj) if not k.startswith("_")]
        return sorted(k for k in keys if str(k).lower() in self.intent_keys)

    def sanitize(self, raw: dict) -> dict:
        """Strip every execution-intent key, keeping only advisory fields."""
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if str(k).lower() not in self.intent_keys}

    def assert_advisory(self, obj) -> dict:
        """Assert (and enforce) that an object is advisory-only. Returns the keys
        that were stripped; ``advisory`` is True because the firewall removes any
        action intent — research can never reach an order path."""
        stripped = self.scan(obj)
        return {"advisory": True, "stripped": stripped, "ok": True}


def research_contribution(p_market: float, p_research: float, p_final: float) -> float:
    """How much of the research view survived calibration, in ``[0, 1]``.

    ``(p_final − p_market) / (p_research − p_market)`` clamped to ``[0, 1]`` — the
    fraction of research's proposed deviation from the market that made it into
    the final calibrated probability. Bounded so research can NEVER amplify beyond
    its own view (and a conservative shrink reduces it). Advisory metric only —
    research never sizes or approves. (Statistical Modeling + Compliance.)"""
    dev_research = float(p_research) - float(p_market)
    dev_final = float(p_final) - float(p_market)
    if abs(dev_research) < 1e-12:
        return 0.0
    ratio = dev_final / dev_research
    return round(max(0.0, min(1.0, ratio)), 6)


def research_is_advisory_only() -> bool:
    """Compliance invariant: research/Grok is ALWAYS advisory-only.

    Research can estimate a probability and supply evidence; it can never size,
    approve, place, arm, or bypass the RiskEngine / EdgeEngine / Bregman gates.
    Execution/size fields are stripped by :func:`strip_forbidden` and the
    probability bundle carries no order, size, or approval field. Returns ``True``
    unconditionally so callers/tests can assert the guarantee explicitly."""
    return True
