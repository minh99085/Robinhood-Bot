"""Robust Polymarket outcome-price parsing + precise outcome diagnostics (PAPER).

Quant scope — *Data Acquisition & Preprocessing*: parse Polymarket ``outcomePrices``
/ token prices / best bid-ask in every shape they actually arrive (float, numeric
string, ``$``/``%`` formatted, JSON-list-encoded string, blank, ``"None"``/``"null"``)
and — when a group cannot be used — return a PRECISE, non-contradictory diagnostic
that distinguishes too-few-outcomes from many-outcomes-with-unusable-prices.

Pure, deterministic, offline. Valid numerics become floats; truly-invalid values are
rejected with a redaction-safe sample. Missing prices are NEVER fabricated.
"""

from __future__ import annotations

import json
from typing import Optional

# precise outcome-diagnostic reasons (replace the contradictory bare
# "insufficient_outcomes" when a group actually has many outcomes).
REASON_MISSING_PRICES = "missing_outcome_prices"
REASON_INSUFFICIENT = "insufficient_outcomes"
REASON_PRICE_COUNT_MISMATCH = "outcome_price_count_mismatch"
REASON_NON_NUMERIC = "non_numeric_outcome_prices"
REASON_DUPLICATE_LABELS = "duplicate_outcome_labels"
REASON_INCOMPLETE_MULTIWAY = "incomplete_multway_family"
REASON_AMBIGUOUS_MULTIWAY = "ambiguous_multway_group"

_NULLISH = {"", "none", "null", "nan", "n/a", "na", "-", "--"}


def parse_price(v) -> Optional[float]:
    """Parse a single price/number from any Polymarket shape. Returns a float or
    ``None`` (never raises, never fabricates).

    Accepts: floats/ints; numeric strings (``"0.42"``); ``$``-prefixed (``"$0.42"``);
    ``%``-suffixed (``"42%"`` -> 0.42); thousands separators (``"1,234.5"``);
    JSON-list-encoded singletons (``"[0.42]"``); and treats blank / ``"None"`` /
    ``"null"`` / ``"nan"`` as missing (``None``)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if (f != f) else f          # reject NaN
    if isinstance(v, (list, tuple)):
        return parse_price(v[0]) if len(v) == 1 else None
    if isinstance(v, str):
        s = v.strip()
        if s.lower() in _NULLISH:
            return None
        # JSON-list-encoded singleton, e.g. '["0.42"]'
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list) and len(arr) == 1:
                    return parse_price(arr[0])
            except Exception:  # noqa: BLE001
                pass
            return None
        pct = s.endswith("%")
        s2 = s.lstrip("$").rstrip("%").replace(",", "").replace("$", "").strip()
        if s2.lower() in _NULLISH:
            return None
        try:
            f = float(s2)
            return (f / 100.0) if pct else f
        except (TypeError, ValueError):
            return None
    return None


def _redact(v) -> str:
    """Short, log-safe sample of an invalid value (never a secret/long blob)."""
    s = str(v)
    return (s[:40] + "…") if len(s) > 40 else s


def analyze_outcomes(labels, prices, tokens, *, min_outcomes: int = 2,
                     valid_lo: float = 0.0, valid_hi: float = 1.0) -> dict:
    """Precise outcome diagnostic for a (would-be) constraint group.

    Returns a dict that ALWAYS reports the counts (raw outcome count, parsed price
    count, valid priced-outcome count, invalid price count, duplicate label count,
    example invalid value) and a ``reason`` that is ``None`` when the outcomes are
    usable, else one of the precise reasons above. Never contradicts itself: a group
    with many outcomes but unusable prices is NOT reported as ``insufficient_outcomes``."""
    labels = list(labels or [])
    prices = list(prices or [])
    tokens = list(tokens or [])
    raw_outcome_count = max(len(labels), len(prices), len(tokens))
    parsed = [parse_price(p) for p in prices]
    in_band = [p for p in parsed if p is not None and valid_lo < p < valid_hi]
    valid_priced = sum(1 for p in parsed if p is not None)
    invalid_price = sum(1 for p in parsed if p is None)
    example_invalid = next((_redact(prices[i]) for i, p in enumerate(parsed)
                            if p is None), None)
    norm_labels = [str(l).strip().lower() for l in labels if str(l).strip()]
    dup_label_count = len(norm_labels) - len(set(norm_labels))

    reason: Optional[str] = None
    if len(prices) == 0:
        reason = REASON_MISSING_PRICES
    elif raw_outcome_count < min_outcomes:
        reason = REASON_INSUFFICIENT          # genuinely too few outcomes
    elif labels and prices and len(labels) != len(prices):
        reason = REASON_PRICE_COUNT_MISMATCH
    elif tokens and prices and len(tokens) != len(prices):
        reason = REASON_PRICE_COUNT_MISMATCH
    elif invalid_price > 0:
        reason = REASON_NON_NUMERIC           # many outcomes but unusable prices
    elif dup_label_count > 0:
        reason = REASON_DUPLICATE_LABELS
    return {
        "reason": reason,
        "raw_outcome_count": raw_outcome_count,
        "parsed_price_count": len(parsed),
        "valid_priced_outcome_count": len(in_band),
        "priced_outcome_count": valid_priced,
        "invalid_price_count": invalid_price,
        "duplicate_label_count": dup_label_count,
        "example_invalid_value": example_invalid,
        "parsed_prices": [None if p is None else round(p, 6) for p in parsed],
    }
