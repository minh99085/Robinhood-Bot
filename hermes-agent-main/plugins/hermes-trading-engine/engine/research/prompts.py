"""Prompt construction + stable prompt hashing for the research engine.

The system prompt makes Grok a research analyst that estimates probability and
flags ambiguity — never a trader. We never store the raw prompt unless
RESEARCH_STORE_PROMPTS=1, and even then secrets are redacted.
"""

from __future__ import annotations

import hashlib
import json

from .validators import redact

SYSTEM_PROMPT = (
    "You are a research analyst estimating the probability that a prediction-market "
    "outcome resolves YES. You are NOT a trader.\n"
    "Rules:\n"
    "- You must NOT recommend an order size, notional, or leverage.\n"
    "- You must NOT submit, cancel, or replace orders.\n"
    "- You must distinguish evidence from speculation.\n"
    "- You must identify resolution ambiguity (unclear source, vague threshold, "
    "subjective judgment, oracle/dispute risk, missing deadline).\n"
    "- If evidence is missing or weak, say so and set no_trade_recommendation=true.\n"
    "- Output STRICT JSON matching the provided schema only. No prose outside JSON."
)


def build_user_prompt(market_ctx: dict, cached_evidence: list[dict] | None = None) -> str:
    ctx = {
        "venue": market_ctx.get("venue"),
        "market_id": market_ctx.get("market_id"),
        "asset_id": market_ctx.get("asset_id"),
        "outcome": market_ctx.get("outcome", "YES"),
        "question": market_ctx.get("question"),
        "resolution_source": market_ctx.get("resolution_source"),
        "close_ts_ms": market_ctx.get("close_ts_ms"),
        "current_market_probability": market_ctx.get("p_market_mid"),
        "best_bid": market_ctx.get("best_bid"),
        "best_ask": market_ctx.get("best_ask"),
        "cached_evidence_summary": [
            {"claim": e.get("claim"), "direction": e.get("direction"),
             "source_type": (e.get("payload_json") or {}).get("source_type")
             if isinstance(e.get("payload_json"), dict) else None}
            for e in (cached_evidence or [])[:10]
        ],
    }
    return (
        "Estimate the probability for the following market and return strict JSON.\n"
        + json.dumps(ctx, default=str, sort_keys=True)
        + "\nRequired fields: market_id, outcome, fair_probability (0..1), confidence (0..1), "
        "evidence (list with claim/source_type/direction/weight/credibility/relevance), "
        "key_assumptions, resolution_notes, ambiguity_score (0..1), no_trade_recommendation, "
        "no_trade_reason, do_not_trade_if, expected_update_triggers, source_coverage_score (0..1)."
    )


def build_messages(market_ctx: dict, cached_evidence: list[dict] | None = None) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(market_ctx, cached_evidence)},
    ]


def prompt_hash(messages: list[dict], config: dict | None = None) -> str:
    """Stable hash of the (redacted) prompt + config. Deterministic across runs."""
    canonical = {
        "messages": [{"role": m.get("role"), "content": redact(str(m.get("content", "")))}
                     for m in messages],
        "config": _canonical_config(config or {}),
    }
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical_config(config: dict) -> dict:
    # Never include secrets in the hash input.
    drop = {"api_key", "xai_api_key", "grok_api_key", "authorization"}
    return {k: v for k, v in sorted(config.items()) if str(k).lower() not in drop}
