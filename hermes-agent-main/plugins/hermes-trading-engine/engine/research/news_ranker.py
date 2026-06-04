"""Source-quality scoring, ranking, dedup, sanitization, and the prompt-injection
firewall for market-news evidence.

Everything here is deterministic and stdlib-only so replay and live monitoring
agree exactly. News is advisory: scores only ever *reduce* confidence or flag
ambiguity/contradiction downstream — they can never approve or size a trade.
"""

from __future__ import annotations

import re

from .news_schemas import (
    NewsEvidenceItem,
    NewsPacket,
    _norm_text,
    normalized_claim,
)

# ---------------------------------------------------------------------------
# Source credibility priors by source_type. Conservative: unknown sources get a
# low prior so weak/unattributed news cannot drive confidence on its own.
# ---------------------------------------------------------------------------
SOURCE_CREDIBILITY = {
    "official": 0.95,        # resolution source / primary record / govt filing
    "regulator": 0.95,
    "exchange": 0.90,
    "primary": 0.90,
    "wire": 0.85,            # reuters/ap-style wire
    "major_news": 0.80,
    "data_provider": 0.80,
    "news": 0.65,
    "analyst": 0.55,
    "blog": 0.40,
    "social": 0.25,
    "forum": 0.20,
    "unknown": 0.30,
}

# Half-life (seconds) for recency decay. ~3 days: news older than that decays.
_RECENCY_HALF_LIFE_S = 3 * 24 * 3600

# ---------------------------------------------------------------------------
# Prompt-injection firewall. All news snippets are UNTRUSTED. We strip phrases
# that try to hijack the model, disclose secrets, escape the schema, or imply
# trade execution/sizing/risk-bypass. This is defense-in-depth: even if a phrase
# slips through, Grok's OUTPUT is still schema-validated and execution-key
# stripped by validators.ResearchFirewall.
# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignore (all|any|the)?\s*(previous|prior|above|earlier)\s+instructions?",
    r"disregard (all|any|the)?\s*(previous|prior|above|earlier)\s+(instructions?|rules?)",
    r"forget (all|everything|the)\s+(above|previous|prior)",
    r"you are now\b",
    r"new instructions?\s*:",
    r"system\s*prompt",
    r"\bsystem\s*:",
    r"\bassistant\s*:",
    r"\bdeveloper\s*:",
    r"override (the\s+)?(risk|safety|guard|system)",
    r"bypass (the\s+)?(risk|safety|guard|engine|edge|check)",
    r"disable (the\s+)?(risk|safety|guard|kill[- ]?switch)",
    r"enable (live|real[- ]?money|micro[- ]?live|guarded[- ]?live)",
    r"go live\b",
    r"(approve|submit|place|execute|cancel|replace)\s+(the\s+)?(order|trade|position)",
    r"set\s+no_trade_recommendation\s*=?\s*(false|0)",
    r"set\s+(order_size|size|notional|stake|leverage|position_size)",
    r"(buy|sell)\s+now\b",
    r"recommend(ed)?\s+(size|notional|stake|leverage)",
    r"(print|reveal|disclose|leak|show)\s+(the\s+)?[\w ]*?(api[_ ]?key|secret|token|wallet|private[_ ]?key)",
    r"private[_ ]?key",
    r"api[_ ]?key",
    r"seed phrase",
    r"\bmnemonic\b",
    r"\bxai-[A-Za-z0-9]{6,}\b",
    r"\bsk-[A-Za-z0-9]{6,}\b",
    r"```",                          # code fence (no executable content)
    r"</?\s*script\b",               # script tags
    r"<\s*/?\s*[a-z!][^>]*>",        # any HTML tag
    r"\{\{.*?\}\}",                  # template injection
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS),
                           re.IGNORECASE | re.DOTALL)

# Secondary single-token red flags used by ``contains_injection`` for tests.
_INJECTION_TOKENS = (
    "ignore previous", "disregard previous", "you are now", "system prompt",
    "override risk", "bypass risk", "approve the order", "place the order",
    "no_trade_recommendation=false", "order_size", "enable live", "go live",
    "private key", "api key", "<script", "```",
)


def contains_injection(text: str) -> bool:
    if not text:
        return False
    low = str(text).lower()
    if any(tok in low for tok in _INJECTION_TOKENS):
        return True
    return bool(_INJECTION_RE.search(str(text)))


def strip_injection(text: str) -> str:
    """Remove injection/HTML/code/secret-ish fragments. Returns plain text."""
    if not text:
        return ""
    out = _INJECTION_RE.sub(" ", str(text))
    out = re.sub(r"<[^>]*>", " ", out)          # any residual HTML
    out = out.replace("`", " ")
    out = re.sub(r"\s+", " ", out).strip()
    return out


def sanitize_snippet(text: str, max_chars: int = 500) -> str:
    """Strip injection/markup and hard-truncate. No full articles / no HTML /
    no script / no executable content ever reaches Grok."""
    clean = strip_injection(text)
    if max_chars and len(clean) > max_chars:
        clean = clean[: max_chars].rstrip() + "…"
    return clean


# ---------------------------------------------------------------------------
# Tokenization + per-feature scoring
# ---------------------------------------------------------------------------
_STOP = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "for", "is",
         "are", "was", "were", "be", "by", "at", "as", "it", "that", "this",
         "with", "from", "will", "has", "have", "had", "yes", "no", "market"}


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if t not in _STOP and len(t) > 1}


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a))


def credibility_score(source_type: str, source_name: str = "") -> float:
    st = str(source_type or "unknown").strip().lower()
    base = SOURCE_CREDIBILITY.get(st, SOURCE_CREDIBILITY["unknown"])
    return round(base, 6)


def recency_score(published_ts, now_ms: int) -> float:
    """Exponential recency decay. Missing timestamp -> 0 (fail-safe: unknown
    age cannot count as fresh)."""
    if published_ts is None:
        return 0.0
    try:
        age_s = max(0.0, (int(now_ms) - int(published_ts)) / 1000.0)
    except (TypeError, ValueError):
        return 0.0
    return round(0.5 ** (age_s / _RECENCY_HALF_LIFE_S), 6)


def freshness_vs_close(published_ts, close_ts_ms, now_ms: int) -> float:
    """Freshness relative to market close. Evidence published after close (for a
    market that already closed) is worthless; recent-before-close is best."""
    r = recency_score(published_ts, now_ms)
    if close_ts_ms is None or published_ts is None:
        return r
    try:
        if int(published_ts) > int(close_ts_ms):
            return round(r * 0.25, 6)  # published after close — heavily discount
    except (TypeError, ValueError):
        return r
    return r


def relevance_score(item_tokens: set, question_tokens: set) -> float:
    return round(_overlap(question_tokens, item_tokens)
                 if question_tokens else _overlap(item_tokens, question_tokens), 6)


def settlement_relevance_score(item_tokens: set, resolution_tokens: set) -> float:
    if not resolution_tokens:
        return 0.0
    return round(_overlap(resolution_tokens, item_tokens), 6)


def directness_score(title: str, snippet: str) -> float:
    """Higher when the claim is concrete (numbers, dates, definitive verbs)
    rather than speculation."""
    text = f"{title} {snippet}".lower()
    score = 0.3
    if re.search(r"\b(confirmed|announced|official|ruled|certified|settled|"
                 r"declared|reported|won|lost|approved|rejected)\b", text):
        score += 0.4
    if re.search(r"\d", text):
        score += 0.2
    if re.search(r"\b(may|might|could|expected|rumou?r|speculat|likely|"
                 r"possibly|reportedly)\b", text):
        score -= 0.25
    return round(max(0.0, min(1.0, score)), 6)


def contradiction_score_for(item: NewsEvidenceItem, others) -> float:
    """Fraction of *other directional* items whose direction disagrees with this
    item, weighted by their credibility. 0 when no directional peers exist."""
    if item.direction not in ("supports_yes", "supports_no"):
        return 0.0
    opp = "supports_no" if item.direction == "supports_yes" else "supports_yes"
    agree_w = 0.0
    disagree_w = 0.0
    for o in others:
        if o is item:
            continue
        if o.direction == item.direction:
            agree_w += max(o.credibility_score, 0.05)
        elif o.direction == opp:
            disagree_w += max(o.credibility_score, 0.05)
    denom = agree_w + disagree_w
    if denom <= 0:
        return 0.0
    return round(disagree_w / denom, 6)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------
def dedupe(items):
    """Deduplicate by URL, title hash, snippet hash, and normalized claim hash.
    Keeps the first occurrence (stable)."""
    seen_url, seen_title, seen_snip, seen_claim = set(), set(), set(), set()
    out = []
    for it in items:
        uk = it.url_key
        th, sh, ch = it.title_hash, it.snippet_hash, it.claim_hash
        if uk and uk in seen_url:
            continue
        if th in seen_title or sh in seen_snip or ch in seen_claim:
            continue
        if uk:
            seen_url.add(uk)
        seen_title.add(th)
        seen_snip.add(sh)
        seen_claim.add(ch)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# Scoring + ranking
# ---------------------------------------------------------------------------
def score_items(items, *, market_ctx: dict, now_ms: int):
    """Populate per-item feature scores in place and return the list."""
    q_tokens = _tokens(market_ctx.get("question"))
    q_tokens |= _tokens(market_ctx.get("slug"))
    q_tokens |= _tokens(" ".join(market_ctx.get("asset_keywords") or []))
    res_tokens = _tokens(market_ctx.get("resolution_source"))
    res_tokens |= _tokens(market_ctx.get("description"))
    close_ts = market_ctx.get("close_ts_ms")

    for it in items:
        it_tokens = _tokens(it.title) | _tokens(it.snippet)
        it.credibility_score = credibility_score(it.source_type, it.source_name)
        it.freshness_score = freshness_vs_close(it.published_ts, close_ts, now_ms)
        it.relevance_score = relevance_score(it_tokens, q_tokens)
        it.settlement_relevance_score = settlement_relevance_score(it_tokens, res_tokens)
    # contradiction needs the full set (after credibility is set)
    for it in items:
        it.contradiction_score = contradiction_score_for(it, items)
    # diversity bonus: distinct sources reduce over-reliance on one outlet
    by_source: dict[str, int] = {}
    for it in items:
        key = (it.source_name or it.url_key or it.source_type).lower()
        by_source[key] = by_source.get(key, 0) + 1
    for it in items:
        it.rank_score = _composite(it, market_ctx)
    return items


def _composite(it: NewsEvidenceItem, market_ctx: dict) -> float:
    direct = directness_score(it.title, it.snippet)
    # Weighted blend; contradiction and ambiguity REDUCE the rank.
    score = (
        0.28 * it.credibility_score
        + 0.22 * it.relevance_score
        + 0.18 * it.freshness_score
        + 0.16 * it.settlement_relevance_score
        + 0.10 * direct
        + 0.06 * (1.0 - it.contradiction_score)
    )
    score *= (1.0 - 0.5 * it.ambiguity_score)
    return round(max(0.0, min(1.0, score)), 6)


def rank_items(items, *, market_ctx: dict, now_ms: int):
    scored = score_items(list(items), market_ctx=market_ctx, now_ms=now_ms)
    # Deterministic ordering: rank desc, then credibility, then evidence_id.
    scored.sort(key=lambda it: (-it.rank_score, -it.credibility_score, it.evidence_id))
    return scored


# ---------------------------------------------------------------------------
# Packet builder (dedupe -> score -> filter -> rank -> cap -> sanitize)
# ---------------------------------------------------------------------------
def build_packet(items, *, market_ctx: dict, now_ms: int, max_items: int = 8,
                 max_snippet_chars: int = 500, min_relevance: float = 0.0,
                 min_credibility: float = 0.0, queries=None,
                 provider_mode: str = "offline_cache",
                 require_published_at: bool = False, reject_unclear_date: bool = False,
                 max_age_hours: float = 0.0) -> NewsPacket:
    market_id = str(market_ctx.get("market_id") or "")
    raw = list(items)
    fetched = len(raw)
    rejected_reasons: dict[str, int] = {}

    def _reject(reason: str) -> None:
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

    deduped = dedupe(raw)
    for _ in range(fetched - len(deduped)):
        _reject("duplicate")

    ranked = rank_items(deduped, market_ctx=market_ctx, now_ms=now_ms)

    kept = []
    stale = 0
    contradiction = 0
    ambiguity = 0
    for it in ranked:
        if contains_injection(it.title) or contains_injection(it.snippet):
            # We still sanitize and keep, but flag — never drop silently so the
            # audit trail shows an injection attempt was neutralized.
            _reject("injection_sanitized")
        it.snippet = sanitize_snippet(it.snippet, max_snippet_chars)
        it.title = sanitize_snippet(it.title, 200)
        # Date-quality filters: reject unclear/missing publish dates and items
        # older than the configured cap (advisory quality tightening).
        if (require_published_at or reject_unclear_date) and it.published_ts is None:
            _reject("no_published_date")
            continue
        if max_age_hours and it.published_ts is not None:
            age_h = (int(now_ms) - int(it.published_ts)) / 3_600_000.0
            if age_h > float(max_age_hours):
                _reject("too_old")
                continue
        if it.relevance_score < min_relevance:
            _reject("low_relevance")
            continue
        if it.credibility_score < min_credibility:
            _reject("low_credibility")
            continue
        if it.freshness_score <= 0.0 and it.published_ts is None:
            stale += 1
        if it.contradiction_score >= 0.5:
            contradiction += 1
        if it.ambiguity_score >= 0.5:
            ambiguity += 1
        kept.append(it)
        if len(kept) >= max_items:
            break

    rejected = fetched - len(kept)
    return NewsPacket(
        market_id=market_id, items=kept, provider_mode=provider_mode,
        queries=list(queries or []), fetched=fetched, used=len(kept),
        rejected=max(0, rejected), stale_count=stale,
        contradiction_count=contradiction, ambiguity_count=ambiguity,
        rejected_reasons=rejected_reasons, max_items=max_items,
        max_snippet_chars=max_snippet_chars)


# ---------------------------------------------------------------------------
# News-conditioned advisory adjustment.
#
# Output is a small, bounded set of adjustments applied to the RESEARCH bundle
# only — confidence haircut, ambiguity bump, a tiny directional probability
# nudge, and an optional no-trade veto. News can NEVER increase confidence past
# the research value, NEVER size/approve a trade, and NEVER bypass a risk gate.
# ---------------------------------------------------------------------------
def news_adjustment(packet: NewsPacket, *, max_prob_delta: float = 0.05,
                    min_relevance: float = 0.2, min_credibility: float = 0.4,
                    contradiction_veto: float = 0.6,
                    settlement_min: float = 0.1) -> dict:
    """Compute advisory adjustments from a ranked NewsPacket (deterministic)."""
    base = {
        "items_used": 0, "confidence_factor": 1.0, "ambiguity_add": 0.0,
        "prob_delta": 0.0, "support_direction": "neutral",
        "contradiction": False, "stale": False, "settlement_warning": False,
        "veto_reason": None,
    }
    if packet is None or packet.is_empty():
        return base

    items = list(packet.items)
    n = len(items)
    usable = [it for it in items
              if it.relevance_score >= min_relevance
              and it.credibility_score >= min_credibility]
    base["items_used"] = len(usable)
    if not usable:
        # Evidence exists but is all weak/irrelevant -> reduce confidence a touch
        # and warn; never a hard veto on its own.
        base["confidence_factor"] = 0.95
        base["settlement_warning"] = True
        return base

    avg_contra = sum(it.contradiction_score for it in usable) / len(usable)
    avg_settle = sum(it.settlement_relevance_score for it in usable) / len(usable)
    avg_fresh = sum(it.freshness_score for it in usable) / len(usable)
    all_stale = all(it.freshness_score <= 0.05 for it in usable)

    # Directional support, credibility*relevance*freshness weighted.
    yes_w = no_w = 0.0
    for it in usable:
        w = it.credibility_score * max(it.relevance_score, 0.05) * max(it.freshness_score, 0.1)
        if it.direction == "supports_yes":
            yes_w += w
        elif it.direction == "supports_no":
            no_w += w
    total_w = yes_w + no_w
    support = "neutral"
    prob_delta = 0.0
    if total_w > 0:
        net = (yes_w - no_w) / total_w
        support = "supports_yes" if net > 0.15 else "supports_no" if net < -0.15 else "neutral"
        prob_delta = round(max(-1.0, min(1.0, net)) * float(max_prob_delta), 6)

    # Contradiction reduces confidence and dampens / vetoes the nudge.
    confidence_factor = 1.0
    contradiction = avg_contra >= 0.4
    veto_reason = None
    if avg_contra >= contradiction_veto:
        confidence_factor *= 0.6
        prob_delta = 0.0
        veto_reason = "news_contradiction"
    elif contradiction:
        confidence_factor *= 0.85
        prob_delta *= 0.5

    if all_stale:
        confidence_factor *= 0.8
        prob_delta = 0.0
        base["stale"] = True

    settlement_warning = avg_settle < settlement_min
    if settlement_warning:
        confidence_factor *= 0.9

    ambiguity_add = round(0.5 * max(0.0, avg_contra - 0.2)
                          + 0.3 * (1.0 - avg_fresh) * (n > 0), 6)
    ambiguity_add = min(0.25, ambiguity_add)

    base.update({
        "confidence_factor": round(max(0.4, min(1.0, confidence_factor)), 6),
        "ambiguity_add": ambiguity_add,
        "prob_delta": prob_delta,
        "support_direction": support,
        "contradiction": contradiction,
        "settlement_warning": settlement_warning,
        "veto_reason": veto_reason,
    })
    return base
