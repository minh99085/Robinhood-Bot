"""Robust Polymarket outcome-price parsing + PRECISE outcome diagnostics (PAPER).

Proves: prices parse from floats / numeric strings / ``$`` / ``%`` / JSON arrays /
nulls / blanks / malformed; gamma binary markets (JSON-string outcomes) build ONE
complement group with distinct outcome ids (no duplicate market id); multi-outcome
markets build a MECE group; price count mismatch / non-numeric / missing /
duplicate-label produce PRECISE non-contradictory reasons with counts; and a 13-way
market is NEVER reported as ``insufficient_outcomes``. Pure; never fabricates prices.
"""

import pytest

from engine.arbitrage.price_parsing import (parse_price, analyze_outcomes,
                                            REASON_NON_NUMERIC, REASON_MISSING_PRICES,
                                            REASON_PRICE_COUNT_MISMATCH,
                                            REASON_INSUFFICIENT, REASON_DUPLICATE_LABELS)
from engine.arbitrage.constraint_discovery import discover_constraints


@pytest.mark.parametrize("raw,expected", [
    (0.42, 0.42), (1, 1.0), ("0.42", 0.42), ("$0.42", 0.42), ("42%", 0.42),
    ("42.5%", 0.425), ("1,234.5", 1234.5), ("[0.42]", 0.42), ('["0.42"]', 0.42),
    ("", None), ("   ", None), ("None", None), ("null", None), ("nan", None),
    ("N/A", None), (None, None), ("abc", None), (True, None), (False, None),
])
def test_parse_price_all_formats(raw, expected):
    assert parse_price(raw) == expected


def test_parse_price_never_raises_on_junk():
    for v in ([], {}, object(), "[1,2,3]", "$", "%"):
        parse_price(v)        # must not raise


# --- precise outcome diagnostics ------------------------------------------- #
def test_valid_multiway_outcomes_have_no_reason():
    d = analyze_outcomes(["A", "B", "C"], ["0.5", "0.3", "0.2"], ["t1", "t2", "t3"])
    assert d["reason"] is None
    assert d["valid_priced_outcome_count"] == 3
    assert d["invalid_price_count"] == 0


def test_price_count_mismatch_is_precise_not_insufficient():
    d = analyze_outcomes(["A", "B", "C"], ["0.5", "0.5"], ["t1", "t2", "t3"])
    assert d["reason"] == REASON_PRICE_COUNT_MISMATCH


def test_non_numeric_prices_reported_with_counts_and_sample():
    d = analyze_outcomes(["A", "B"], ["N/A", "null"], ["t1", "t2"])
    assert d["reason"] == REASON_NON_NUMERIC
    assert d["invalid_price_count"] == 2
    assert d["example_invalid_value"] is not None


def test_many_outcomes_never_reported_as_insufficient():
    labels = [f"C{i}" for i in range(13)]
    prices = [f"{0.9/13:.4f}" for _ in range(13)]
    d = analyze_outcomes(labels, prices, labels)
    assert d["reason"] is None
    assert d["raw_outcome_count"] == 13


def test_missing_prices_reason():
    assert analyze_outcomes(["A", "B"], [], ["t1", "t2"])["reason"] == REASON_MISSING_PRICES


def test_genuinely_insufficient_outcomes():
    assert analyze_outcomes(["A"], ["0.9"], ["t1"])["reason"] == REASON_INSUFFICIENT


def test_duplicate_labels_reason():
    d = analyze_outcomes(["Yes", "yes"], ["0.5", "0.5"], ["t1", "t2"])
    assert d["reason"] == REASON_DUPLICATE_LABELS
    assert d["duplicate_label_count"] == 1


# --- discovery integration (gamma JSON strings) ---------------------------- #
def _gamma(mid, outcomes, prices, tokens, **kw):
    m = {"id": mid, "outcomes": outcomes, "outcomePrices": prices,
         "clobTokenIds": tokens, "active": True, "enableOrderBook": True,
         "bestBid": "0.40", "bestAsk": "0.42", "topDepthUsd": "500"}
    m.update(kw)
    return m


def test_gamma_binary_builds_one_complement_no_duplicate_market_id():
    res = discover_constraints([_gamma("573655", '["Yes", "No"]',
                                       '["0.41","0.59"]', '["tokA","tokB"]')])
    assert len(res.groups) == 1
    g = res.groups[0]
    assert g.relation == "complement"
    assert g.market_ids == ["573655"]                 # ONE market, not duplicated
    assert len(set(g.outcome_ids)) == 2               # two DISTINCT outcome ids
    assert g.outcome_labels == ["YES", "NO"]
    assert g.outcome_prices == [0.41, 0.59]
    assert res.skipped == []


def test_gamma_multiway_builds_mece_group():
    n = 13
    labels = '["' + '","'.join(f"C{i}" for i in range(n)) + '"]'
    prices = '["' + '","'.join(f"{0.9/n:.4f}" for _ in range(n)) + '"]'
    tokens = '["' + '","'.join(f"t{i}" for i in range(n)) + '"]'
    res = discover_constraints([_gamma("mw1", labels, prices, tokens)])
    assert len(res.groups) == 1
    g = res.groups[0]
    assert g.relation == "mece"
    assert g.n_outcomes == 13
    assert len(set(g.outcome_ids)) == 13


def test_gamma_non_numeric_prices_precise_diagnostic():
    res = discover_constraints([_gamma("bad", '["Yes","No"]',
                                        '["N/A","null"]', '["a","b"]')])
    assert res.groups == []
    s = res.skipped[0]
    assert s["reason"] == "non_numeric_outcome_prices"
    assert s["detail"]["invalid_price_count"] == 2
    assert s["market_ids"] == ["bad"]


def test_no_contradictory_insufficient_outcomes_with_many_outcomes():
    # a market whose outcomes JSON string is 13 chars must NOT be called
    # "insufficient_outcomes / 13 outcomes" — it parses to a valid binary.
    res = discover_constraints([_gamma("573655", '["Yes", "No"]',
                                        '["0.41","0.59"]', '["a","b"]')])
    for s in res.skipped:
        assert not (s["reason"] == "insufficient_outcomes")


def test_parse_metrics_reported():
    res = discover_constraints([_gamma("ok", '["Yes","No"]', '["0.41","0.59"]', '["a","b"]'),
                                _gamma("bad", '["Yes","No"]', '["x","y"]', '["c","d"]')])
    m = res.metrics
    assert "parsed_price_success_rate" in m
    assert "non_numeric_price_count" in m
    assert m["non_numeric_price_count"] >= 1
