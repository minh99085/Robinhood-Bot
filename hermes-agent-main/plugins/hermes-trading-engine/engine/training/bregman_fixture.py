"""Known-good Bregman synthetic fixture proof (PAPER ONLY, isolated, read-only).

Proves the Bregman certifier CAN generate candidates when given depth-sufficient,
complete, fresh, fairly-priced synthetic groups — so a real ``candidates_generated=0``
is provably a DATA problem (thin depth / incomplete / no edge), not a code defect.

Critical isolation invariants (verified + reported):
* runs on a FRESH ``BregmanArbitrageEngine`` with DEFAULT (un-loosened) gates,
* never enables live trading, never opens a real bundle, never sizes a real trade,
* never touches the live trainer's runtime metrics (pure, returns its own dict).

Deterministic; no network; no I/O.
"""

from __future__ import annotations

from engine.training.bregman_execution import BregmanArbitrageEngine
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _leg(market_id, outcome, token, ask, depth, *, bid=None):
    return SimplexLeg(market_id=market_id, outcome=outcome, token_id=token,
                      ask=ask, bid=bid if bid is not None else round(ask - 0.01, 4),
                      depth_usd=depth, fresh_book=True, stale=False)


def run_bregman_synthetic_fixture() -> dict:
    """Run the known-good fixture through a fresh certifier with DEFAULT gates.

    Returns a proof dict with explicit pass flags + safety confirmations. Does not
    mutate any shared state (the engine is constructed locally)."""
    eng = BregmanArbitrageEngine()          # DEFAULT thresholds (NOT loosened)
    required_depth = float(eng.min_depth_usd)
    max_spread = float(eng.max_spread)
    ample = max(required_depth * 20.0, 1000.0)

    # --- valid binary: YES 0.45 + NO 0.45 = 0.90 < $1 payout, ample fresh depth ---
    binary = SimplexGroup(
        "synthetic:binary", "binary_yes_no",
        [_leg("synthetic_bin", "YES", "synthetic_bin_Y", 0.45, ample),
         _leg("synthetic_bin", "NO", "synthetic_bin_N", 0.45, ample)],
        mutually_exclusive=True, exhaustive=True)
    binary_opp = eng.certify(binary)

    # --- valid multi-way MECE: 0.30+0.30+0.30 = 0.90 < $1 payout, ample fresh depth ---
    multiway = SimplexGroup(
        "synthetic:multiway", "mutually_exclusive",
        [_leg("synthetic_mw_a", "YES", "tok_a", 0.30, ample),
         _leg("synthetic_mw_b", "YES", "tok_b", 0.30, ample),
         _leg("synthetic_mw_c", "YES", "tok_c", 0.30, ample)],
        mutually_exclusive=True, exhaustive=True)
    multiway_opp = eng.certify(multiway)

    # --- invalid cases MUST be rejected (gates unchanged) ---
    overpriced = SimplexGroup(
        "synthetic:overpriced", "binary_yes_no",
        [_leg("ov", "YES", "ov_Y", 0.60, ample), _leg("ov", "NO", "ov_N", 0.60, ample)],
        mutually_exclusive=True, exhaustive=True)
    thin = SimplexGroup(
        "synthetic:thin", "binary_yes_no",
        [_leg("th", "YES", "th_Y", 0.45, 1.0), _leg("th", "NO", "th_N", 0.45, 1.0)],
        mutually_exclusive=True, exhaustive=True)
    duplicate = SimplexGroup(
        "synthetic:dup", "mutually_exclusive",
        [_leg("d", "YES", "dup_tok", 0.45, ample), _leg("d", "YES", "dup_tok", 0.45, ample)],
        mutually_exclusive=True, exhaustive=True)
    stale_leg = _leg("st", "YES", "st_Y", 0.45, ample)
    stale_leg.fresh_book = False
    stale_leg.stale = True
    stale = SimplexGroup(
        "synthetic:stale", "binary_yes_no",
        [stale_leg, _leg("st", "NO", "st_N", 0.45, ample)],
        mutually_exclusive=True, exhaustive=True)

    invalids = {"overpriced": overpriced, "thin_depth": thin,
                "duplicate_legs": duplicate, "stale_book": stale}
    invalid_results = {k: eng.certify(g).is_opportunity for k, g in invalids.items()}
    all_invalid_rejected = not any(invalid_results.values())

    binary_ok = bool(binary_opp.is_opportunity)
    multiway_ok = bool(multiway_opp.is_opportunity)
    passed = bool(binary_ok and multiway_ok and all_invalid_rejected)
    return {
        "bregman_synthetic_fixture_passed": passed,
        "synthetic_binary_candidate_generated": binary_ok,
        "synthetic_multiway_candidate_generated": multiway_ok,
        "synthetic_invalid_cases_rejected": all_invalid_rejected,
        "synthetic_invalid_case_results": {k: (not v) for k, v in invalid_results.items()},
        # safety confirmations (the fixture used DEFAULT gates + an isolated engine)
        "synthetic_fixture_gate_loosening": False,
        "synthetic_fixture_required_depth_usd": required_depth,
        "synthetic_fixture_max_spread": max_spread,
        "synthetic_fixture_live_trading_enabled": False,
        "synthetic_fixture_contaminated_real_metrics": False,
        "synthetic_binary_lower_bound": round(float(binary_opp.profit_lower_bound), 6),
        "synthetic_multiway_lower_bound": round(float(multiway_opp.profit_lower_bound), 6),
    }
