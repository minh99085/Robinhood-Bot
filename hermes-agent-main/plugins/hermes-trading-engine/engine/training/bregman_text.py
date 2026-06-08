"""Deterministic text normalization for Bregman/ABCAS complete-set discovery.

Quant scope — *Data Preprocessing & Feature Engineering*: normalize Polymarket
market titles/slugs/outcomes so markets that belong to the same mutually-exclusive
/ exhaustive event family are more reliably linked, and so completeness can be
*diagnosed* (never fabricated). Pure, offline, no network, no execution.

Nothing here weakens certification: it only improves grouping + produces richer
``not_exhaustive`` / outcome-family diagnostics. If a set is incomplete it stays
rejected — these helpers just explain *why*.
"""

from __future__ import annotations

import re
from typing import Optional

# Phrases that carry no event identity — dropped so "Will X win?" and "X to win"
# normalize to the same family stem.
_FILLER = (
    "will", "the", "a", "an", "to", "be", "is", "are", "was", "were", "in",
    "on", "at", "of", "for", "by", "happen", "occur", "reach", "hit", "this",
    "that", "during", "before", "after", "market", "question", "outcome",
)
_MONTHS = {
    "jan": "january", "feb": "february", "mar": "march", "apr": "april",
    "jun": "june", "jul": "july", "aug": "august", "sep": "september",
    "sept": "september", "oct": "october", "nov": "november", "dec": "december",
}
_YESNO = {"yes", "no", "y", "n", "true", "false"}

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
_NUM_RANGE_RE = re.compile(
    r"(\d[\d,\.]*)\s*(?:-|to|–|—|through|thru|and)\s*(\d[\d,\.]*)")
_NUM_CMP_RE = re.compile(
    r"\b(above|below|over|under|greater than|less than|at least|at most|"
    r">=|<=|>|<)\b")


def normalize_text(s: Optional[str]) -> str:
    """Lowercase, strip punctuation, expand month abbreviations, drop filler/yes-no
    phrasing and collapse whitespace. Deterministic. Empty input -> ``""``.

    Makes "Will the Fed cut rates in Sept.?" and "Fed to cut rates in September"
    normalize to the same stem so they can be recognized as the same family.
    """
    if not s:
        return ""
    t = str(s).lower()
    t = _PUNCT_RE.sub(" ", t)
    toks = []
    for tok in _WS_RE.split(t):
        if not tok:
            continue
        tok = _MONTHS.get(tok, tok)
        if tok in _FILLER or tok in _YESNO:
            continue
        toks.append(tok)
    return " ".join(toks).strip()


def _slug(s: str) -> str:
    return _WS_RE.sub("-", normalize_text(s)).strip("-")


def event_family_key(market) -> Optional[str]:
    """Best-effort normalized family key for a market when no explicit event id is
    present. Derived from (normalized event slug/title) + category + expiry day so
    markets in the same winner/nominee/range family bucket together. Returns
    ``None`` when there is not enough signal to form a non-degenerate key (so we
    NEVER fabricate a family from a bare market id)."""
    raw = _get(market, "raw", {}) or {}
    # 1) explicit event identifiers always win
    for k in ("event_id", "eventId", "eventSlug", "event_slug",
              "negRiskMarketID", "negRiskMarketId", "conditionId"):
        v = raw.get(k) or _get(market, k, None)
        if v:
            return f"evt:{v}"
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        ev = events[0]
        for k in ("id", "slug", "ticker", "title"):
            if ev.get(k):
                return f"evt:{_slug(str(ev[k]))}" if k in ("title",) else f"evt:{ev[k]}"
    # 2) derive a family stem from the question/title (drop the variable outcome)
    title = (_get(market, "question", None) or raw.get("question")
             or raw.get("title") or _get(market, "title", None) or "")
    stem = _family_stem(str(title))
    if not stem:
        return None
    cat = (raw.get("category") or _get(market, "category", "") or "").strip().lower()
    expiry = _expiry_day(market)
    parts = [stem]
    if cat:
        parts.append(cat)
    if expiry:
        parts.append(expiry)
    return "fam:" + "|".join(parts)


def _family_stem(title: str) -> str:
    """Family stem: the normalized title with the trailing variable outcome token
    (candidate/team/player/range bound) removed so sibling outcomes share a stem.

    Conservative — only the FIRST few normalized tokens are kept, which captures
    the event ("us presidential election") while dropping the per-leg outcome
    ("trump" / "biden"). Empty when the title carries no signal."""
    # "Event question? Outcome" — the candidate/outcome lives AFTER the '?', so the
    # text BEFORE the '?' is the shared event identity. Use it when present.
    head = title.split("?", 1)[0] if "?" in title else title
    norm = normalize_text(head)
    if not norm:
        norm = normalize_text(title)
    if not norm:
        return ""
    toks = norm.split()
    # range/threshold markets collapse the numeric bound so buckets share a stem.
    if _NUM_RANGE_RE.search(title) or _NUM_CMP_RE.search(title.lower()):
        toks = [t for t in toks if not _is_numeric(t)]
        return " ".join(toks[:8])
    # keep up to the first 8 event tokens (the event identity, not the outcome).
    return " ".join(toks[:8])


def _is_numeric(tok: str) -> bool:
    return bool(re.fullmatch(r"\d[\d,\.]*", tok or ""))


def infer_outcome_label(question: Optional[str], outcomes=None) -> str:
    """Best-effort human outcome label for a leg (YES/NO/candidate/team/range bound).

    Used only for completeness DIAGNOSTICS — to list observed vs missing outcomes.
    Returns the ACTUAL outcome label when present (so a binary leg reports ``YES`` /
    ``NO`` rather than ``unknown``); otherwise the normalized last distinctive token
    of the question. Never affects certification."""
    if outcomes:
        for o in outcomes:
            so = str(o).strip()
            if so:
                return so.upper() if so.lower() in _YESNO else so
    norm = normalize_text(question)
    if not norm:
        return "unknown"
    return norm.split()[-1] if norm.split() else "unknown"


def classify_market_kind(question: Optional[str], n_legs: int = 1,
                         outcomes=None) -> str:
    """Classify the resolution structure: ``binary`` / ``multi_way`` / ``range`` /
    ``winner_take_all`` / ``ambiguous``. Diagnostic only (drives near-miss labels)."""
    q = (question or "").lower()
    if _NUM_RANGE_RE.search(q) or _NUM_CMP_RE.search(q):
        return "range"
    win_words = ("win", "winner", "nominee", "elected", "champion", "mvp")
    if any(w in q for w in win_words) and n_legs >= 3:
        return "winner_take_all"
    if n_legs >= 3:
        return "multi_way"
    if n_legs == 2 or (outcomes and len(outcomes) == 2):
        return "binary"
    if not q:
        return "ambiguous"
    return "binary" if n_legs <= 2 else "multi_way"


def _expiry_day(market) -> str:
    raw = _get(market, "raw", {}) or {}
    for k in ("endDate", "end_date", "endDateIso", "closeTime", "expiry",
              "resolutionDate"):
        v = raw.get(k) or _get(market, k, None)
        if v:
            sv = str(v)
            m = re.match(r"(\d{4}-\d{2}-\d{2})", sv)
            return m.group(1) if m else sv[:10]
    return ""


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
