"""Bregman event-family completeness: false-incomplete fix + diagnostics (B).

Proves a COMPLETE family (declared outcome count == scanned legs, or a truthy
completeness marker) is NOT falsely rejected as incomplete, a GENUINELY incomplete
family IS still rejected (never certified), a string ``"false"`` marker does not
certify an incomplete set, and per-family completeness diagnostics are produced.
"""

from engine.training.bregman_grouping import (
    _group_is_exhaustive, _truthy, family_completeness_report, group_markets,
    validate_simplex)


class _Rec:
    def __init__(self, raw):
        self.raw = raw
        self.market_id = str(raw.get("id", "m"))
        self.question = raw.get("question", "Will X resolve YES?")
        self.clob_token_ids = raw.get("clobTokenIds", [])
        self.group_key = raw.get("group_key", "")
        self.top_depth_usd = float(raw.get("topDepthUsd", 100.0))
        self.book_age_s = 1.0

    def __getattr__(self, k):  # tolerate attrs the grouping reads
        return None


def _recs(n, **raw_over):
    out = []
    for i in range(n):
        raw = {"id": f"m{i}", "group_key": "evt1", "bestAsk": "0.30", "bestBid": "0.29",
               "clobTokenIds": [f"t{i}a", f"t{i}b"], "topDepthUsd": 100.0}
        raw.update(raw_over)
        out.append(_Rec(raw))
    return out


# --------------------------------------------------------------------------- #
# truthiness (the false-positive safety fix)
# --------------------------------------------------------------------------- #
def test_truthy_rejects_false_strings():
    assert _truthy(True) and _truthy("true") and _truthy(1) and _truthy("complete")
    assert not _truthy(False) and not _truthy("false") and not _truthy("0")
    assert not _truthy("no") and not _truthy("") and not _truthy(0)


def test_string_false_marker_is_not_exhaustive():
    recs = _recs(2, negRiskComplete="false")
    assert _group_is_exhaustive(recs) is False        # must NOT certify incomplete set


# --------------------------------------------------------------------------- #
# complete families are NOT falsely rejected
# --------------------------------------------------------------------------- #
def test_complete_by_declared_count_not_false_incomplete():
    recs = _recs(3, outcomeCount=3)                   # declared 3, scanned 3 -> complete
    assert _group_is_exhaustive(recs) is True
    fc = family_completeness_report(recs)
    assert fc["complete"] is True and fc["missing_outcome_count"] == 0
    assert fc["would_be_false_incomplete"] is False


def test_complete_by_truthy_marker():
    recs = _recs(4, negRiskComplete=True)
    assert _group_is_exhaustive(recs) is True


def test_complete_family_group_is_exhaustive_and_validates():
    recs = _recs(3, outcomeCount=3)
    groups = group_markets(recs, include_binary=False)
    evt = [g for g in groups if len(g.legs) >= 2]
    assert evt and all(g.exhaustive for g in evt)
    ok, why = validate_simplex(evt[0])
    assert ok or why != "not_exhaustive"              # completeness no longer the blocker


# --------------------------------------------------------------------------- #
# genuinely incomplete families STAY rejected (never certified)
# --------------------------------------------------------------------------- #
def test_incomplete_family_stays_rejected():
    recs = _recs(3, outcomeCount=5)                   # declared 5, only 3 scanned
    assert _group_is_exhaustive(recs) is False
    fc = family_completeness_report(recs)
    assert fc["complete"] is False and fc["missing_outcome_count"] == 2
    assert fc["would_be_false_incomplete"] is False   # honest incomplete, not a false reject
    groups = group_markets(recs, include_binary=False)
    evt = [g for g in groups if len(g.legs) >= 2][0]
    assert evt.exhaustive is False
    ok, why = validate_simplex(evt)
    assert not ok and why == "not_exhaustive"         # still rejected


def test_undeclared_family_stays_incomplete():
    recs = _recs(3)                                   # no completeness signal at all
    assert _group_is_exhaustive(recs) is False
    fc = family_completeness_report(recs)
    assert fc["complete"] is False and fc["declared_outcome_count"] is None


# --------------------------------------------------------------------------- #
# diagnostics carried on the group meta
# --------------------------------------------------------------------------- #
def test_group_meta_carries_family_completeness_diagnostics():
    recs = _recs(3, outcomeCount=5)
    groups = group_markets(recs, include_binary=False)
    evt = [g for g in groups if len(g.legs) >= 2][0]
    fc = evt.meta.get("family_completeness")
    assert fc and fc["n_legs_scanned"] == 3 and fc["declared_outcome_count"] == 5
    assert fc["missing_outcome_count"] == 2
    assert isinstance(fc["present_outcomes_sample"], list)
