"""Feature extraction, scorecard, and baseline comparison for the inspection report.

Inspection/reporting ONLY. Pure functions over already-collected data
(training status JSON + API snapshots + test results). No side effects, no
network, no trading.
"""

from __future__ import annotations

from typing import Any, Optional


def _get(d: Any, *path, default=None):
    """Nested dict getter that tolerates missing keys / non-dicts."""
    cur = d
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def _first(*vals, default=None):
    """First non-None value."""
    for v in vals:
        if v is not None:
            return v
    return default


def _num(v: Any) -> Optional[float]:
    """Best-effort float coercion (bool -> 1/0); None on failure."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sum_opt(*vals) -> Optional[float]:
    """Sum of the numeric values present, or None if none are numeric."""
    nums = [_num(v) for v in vals]
    nums = [n for n in nums if n is not None]
    return float(sum(nums)) if nums else None


def _ratio(numerator: Any, denominator: Any) -> Optional[float]:
    """Safe ratio in [0, 1+]; None when inputs are missing or denom <= 0."""
    n, d = _num(numerator), _num(denominator)
    if n is None or d is None or d <= 0:
        return None
    return round(n / d, 4)


def _dashboard_equity(api: dict | None) -> Optional[float]:
    """Pull the legacy-dashboard equity from a collected /api/state snapshot.

    The dashboard engine and the paper-training loop are distinct surfaces; this
    lets the report cross-check their equity for inconsistencies. Read-only.
    """
    state = _get(api or {}, "state", default={}) or {}
    return _num(_first(
        state.get("equity"),
        _get(state, "pnl", "equity"),
        _get(state, "portfolio", "equity"),
        _get(state, "accounting", "equity"),
    ))


def extract_features(status: dict | None, api: dict | None = None,
                     tests: dict | None = None, env: dict | None = None) -> dict:
    """Flatten the documented bot-health feature set from collected sources.

    Every field defaults to None ("missing/unknown") so downstream code can
    distinguish "feature absent" from "feature present but zero".
    """
    status = status or {}
    api = api or {}
    tests = tests or {}
    env = env or {}

    pnl = _get(status, "pnl", default={}) or {}
    scan = _get(status, "scan_metrics", default={}) or {}
    safety = _get(status, "safety", default={}) or {}
    mon = _get(status, "monitoring", default={}) or {}
    camp = _get(status, "training_campaign", default={}) or {}
    camp_ev = _get(camp, "evidence", default={}) or {}
    csafe = _get(status, "campaign_safety", default={}) or _get(camp, "safety_profile", default={}) or {}
    bp = _get(status, "btc_pulse", default={}) or {}
    news = _get(status, "news", default={}) or {}
    research = _get(status, "research", default={}) or _get(api, "research_status", default={}) or {}
    fast = _get(status, "btc_fast_price", default={}) or {}
    fa = _get(status, "feedback_accelerator", default={}) or {}

    # Chainlink: prefer API snapshot (validated), fall back to status.
    cl_api = _get(api, "chainlink_status", default={}) or {}
    cl_oracle = _get(cl_api, "btc_usd", default={}) or _get(status, "chainlink_oracle", default={}) or {}

    runtime_seconds = _first(status.get("runtime_seconds"), _get(camp_ev, "runtime_hours"))
    runtime_minutes = None
    if isinstance(status.get("runtime_seconds"), (int, float)):
        runtime_minutes = round(status["runtime_seconds"] / 60.0, 2)

    feats: dict[str, Any] = {
        # --- paper training core ---
        "paper_training_running": bool(status) and str(status.get("mode", "paper")).lower() == "paper"
        if status else None,
        "runtime_minutes": runtime_minutes,
        "scanned_markets": scan.get("scanned"),
        "kept_markets": scan.get("kept"),
        "open_positions": pnl.get("open_positions"),
        "closed_positions": _first(pnl.get("trades_closed"), pnl.get("closed_positions")),
        "paper_trades": _first(camp_ev.get("paper_trades"), pnl.get("trades_closed")),
        "equity": pnl.get("equity"),
        "total_pnl": pnl.get("total_pnl"),
        "after_cost_pnl": _first(camp_ev.get("after_cost_expectancy"),
                                 pnl.get("after_cost_pnl"), pnl.get("after_cost")),
        "win_rate_traded_only": pnl.get("win_rate"),
        "brier": _first(_get(status, "quality", "brier"), mon.get("brier"), pnl.get("brier")),
        "ece": _first(_get(status, "quality", "ece"), mon.get("ece"), pnl.get("ece")),
        "sharpe": _first(_get(status, "quality", "sharpe"), pnl.get("sharpe")),
        "sortino": _first(_get(status, "quality", "sortino"), pnl.get("sortino")),
        "calmar": _first(_get(status, "quality", "calmar"), pnl.get("calmar")),
        "max_drawdown": _first(pnl.get("max_drawdown"), mon.get("max_drawdown")),
        # --- safety ---
        "live_detected": safety.get("live_detected"),
        "preflight_ok": safety.get("ok"),
        # --- chainlink ---
        "chainlink_enabled": _first(cl_oracle.get("enabled"), cl_api.get("available")),
        "chainlink_valid": cl_oracle.get("valid"),
        "chainlink_stale": cl_oracle.get("stale"),
        "chainlink_age_seconds": _first(cl_oracle.get("age_seconds"), cl_oracle.get("age")),
        "chainlink_price": _first(cl_oracle.get("price"), cl_oracle.get("answer")),
        # --- btc fast price ---
        "btc_fast_price_enabled": fast.get("enabled"),
        "btc_fast_price_valid": fast.get("valid"),
        "btc_fast_price_age_seconds": fast.get("age_seconds"),
        "btc_fast_price_disagreement_bps": fast.get("disagreement_vs_chainlink_bps"),
        # --- btc pulse ---
        "btc_pulse_enabled": bp.get("btc_pulse_enabled"),
        "btc_pulse_frozen": bp.get("btc_pulse_frozen"),
        "btc_pulse_oracle_gate_active": _first(bp.get("btc_pulse_oracle_required"),
                                               bp.get("btc_pulse_oracle_gate_active")),
        "btc_pulse_rejection_reasons": bp.get("btc_pulse_rejection_reasons"),
        "btc_pulse_paper_trades": bp.get("btc_pulse_paper_trades"),
        "btc_pulse_after_cost_pnl": bp.get("btc_pulse_after_cost_pnl"),
        "btc_pulse_regime": _first(bp.get("btc_pulse_regime"), bp.get("regime")),
        # --- news scanner ---
        "news_scanner_enabled": news.get("news_scanner_enabled"),
        "news_provider_mode": news.get("news_provider_mode"),
        "news_items_fetched": news.get("news_items_fetched"),
        "news_items_used": news.get("news_items_used"),
        "news_rejected_stale": _first(news.get("news_rejected_stale"),
                                      _get(news, "news_rejection_reasons", "stale")),
        "news_rejected_unclear_date": _first(news.get("news_rejected_unclear_date"),
                                             _get(news, "news_rejection_reasons", "unclear_date")),
        "news_rejected_low_credibility": _first(news.get("news_rejected_low_credibility"),
                                                _get(news, "news_rejection_reasons", "low_credibility")),
        # --- grok / research ---
        "grok_enabled": _first(research.get("grok_enabled"), research.get("enabled")),
        "grok_has_api_key": bool(env.get("GROK_API_KEY") or env.get("XAI_API_KEY")) or None,
        "grok_with_news_count": _first(research.get("grok_with_news_count"),
                                       research.get("requests_with_news")),
        "grok_cache_hits": _first(research.get("grok_cache_hits"), research.get("cache_hits")),
        # --- bregman ---
        "bregman_paper_enabled": _first(csafe.get("realistic_fill_enabled"),
                                        mon.get("bregman_enabled")),
        "bregman_candidates_found": _first(mon.get("bregman_opportunities"),
                                           camp_ev.get("bregman_candidates")),
        "bregman_certified_count": _first(camp_ev.get("bregman_certified"),
                                          mon.get("bregman_certified")),
        "bregman_certified_profit": _first(mon.get("certified_bregman_profit"),
                                           camp_ev.get("after_cost_expectancy")),
        "bregman_false_positive_rate": _first(mon.get("bregman_false_positive_rate"),
                                              camp_ev.get("bregman_false_positives")),
        # --- attribution / fill realism / scan ---
        "market_scan_limit_effective": _first(scan.get("scan_limit"), scan.get("scanned")),
        "paper_attribution_enabled": _first(csafe.get("realistic_fill_enabled"), True if pnl else None),
        "exploration_validation_separated": _first(fa.get("exploration_counts_for_readiness") is False
                                                   if fa else None,
                                                   csafe.get("clean_label_guard_enabled")),
        "fill_realism_enabled": _first(csafe.get("realistic_fill_enabled"),
                                       _get(status, "pnl", "realistic_fill")),
        "fantasy_fill_rejections": _first(pnl.get("fantasy_fill_rejections"),
                                          mon.get("fantasy_fill_rejections")),
        "fill_attempts": _first(pnl.get("fill_attempts"), pnl.get("orders_submitted"),
                                _get(status, "execution", "fill_attempts")),
        # --- exploration vs validation separation (counts where available) ---
        "exploration_trades": _first(fa.get("exploration_trades"),
                                     _get(status, "pnl", "exploration_trades")),
        "validation_trades": _first(fa.get("validation_trades"),
                                    _get(status, "pnl", "validation_trades")),
        # --- cross-surface equity (for consistency checks) ---
        "dashboard_equity": _dashboard_equity(api),
        # --- tests ---
        "tests_present": tests.get("present"),
        "tests_passing": tests.get("passing"),
    }
    # Derived: realistic-fill rejection RATE = rejected / (rejected + filled).
    feats["fill_realism_rejection_rate"] = _ratio(
        feats.get("fantasy_fill_rejections"),
        _first(feats.get("fill_attempts"),
               _sum_opt(feats.get("fantasy_fill_rejections"), feats.get("paper_trades"))))
    # Helpful raw-section presence flags for the report narrative.
    feats["_sections_present"] = {
        "pnl": bool(pnl), "scan_metrics": bool(scan), "btc_pulse": bool(bp),
        "news": bool(news), "research": bool(research), "btc_fast_price": bool(fast),
        "campaign": bool(camp), "campaign_safety": bool(csafe), "monitoring": bool(mon),
        "chainlink": bool(cl_oracle),
    }
    return feats


# Direction of "good": metrics where higher is better vs. lower is better.
HIGHER_BETTER = {
    "equity", "total_pnl", "after_cost_pnl", "closed_positions", "paper_trades",
    "win_rate_traded_only", "sharpe", "sortino", "calmar",
    "btc_pulse_after_cost_pnl", "bregman_certified_profit", "news_quality_ratio",
}
LOWER_BETTER = {"brier", "ece", "max_drawdown"}
BOOL_BETTER_TRUE = {"chainlink_valid", "tests_passing"}

COMPARISON_METRICS = sorted(
    HIGHER_BETTER | LOWER_BETTER | BOOL_BETTER_TRUE
)


def _news_quality_ratio(feats: dict) -> Optional[float]:
    fetched = feats.get("news_items_fetched")
    used = feats.get("news_items_used")
    try:
        if fetched and float(fetched) > 0 and used is not None:
            return round(float(used) / float(fetched), 4)
    except (TypeError, ValueError):
        return None
    return None


def _coerce_num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compare_baseline(current: dict, baseline: dict | None,
                     material_pct: float = 0.10) -> dict:
    """Compare current features vs a baseline report's features.

    Returns ``{"available": bool, "metrics": {name: {...}}, "regression": bool,
    "improved": [...], "degraded": [...]}``. ``regression`` is True if a material
    degradation is detected on any key metric.
    """
    cur = dict(current or {})
    cur["news_quality_ratio"] = _news_quality_ratio(cur)
    if not baseline:
        return {"available": False, "metrics": {}, "regression": False,
                "improved": [], "degraded": [], "flat": [], "missing_data": []}

    base_feats = baseline.get("features") if "features" in baseline else baseline
    base_feats = dict(base_feats or {})
    base_feats["news_quality_ratio"] = _news_quality_ratio(base_feats)

    metrics: dict[str, dict] = {}
    improved, degraded, flat, missing = [], [], [], []
    regression = False

    for name in COMPARISON_METRICS:
        c = _coerce_num(cur.get(name))
        b = _coerce_num(base_feats.get(name))
        if c is None or b is None:
            metrics[name] = {"current": cur.get(name), "baseline": base_feats.get(name),
                             "delta": None, "direction": "MISSING_DATA"}
            missing.append(name)
            continue
        delta = round(c - b, 6)
        higher_better = name in HIGHER_BETTER or name in BOOL_BETTER_TRUE
        # Relative change vs baseline magnitude (guard divide-by-zero).
        denom = abs(b) if abs(b) > 1e-9 else 1.0
        rel = (c - b) / denom
        if abs(delta) < 1e-9:
            direction = "FLAT"
            flat.append(name)
        else:
            good = (delta > 0) if higher_better else (delta < 0)
            direction = "IMPROVED" if good else "DEGRADED"
            (improved if good else degraded).append(name)
            # Material regression detection on key metrics.
            material = abs(rel) >= material_pct
            key_metric = name in {
                "after_cost_pnl", "equity", "total_pnl", "sharpe",
                "win_rate_traded_only", "btc_pulse_after_cost_pnl",
                "bregman_certified_profit", "max_drawdown", "brier", "ece",
            } or name in BOOL_BETTER_TRUE
            if direction == "DEGRADED" and key_metric and (material or name in BOOL_BETTER_TRUE):
                regression = True
        metrics[name] = {"current": cur.get(name), "baseline": base_feats.get(name),
                         "delta": delta, "direction": direction}

    return {"available": True, "metrics": metrics, "regression": regression,
            "improved": improved, "degraded": degraded, "flat": flat,
            "missing_data": missing}


# ----------------------------------------------------------------------------- #
# Missing-feature detection
# ----------------------------------------------------------------------------- #
def detect_missing_features(feats: dict, api: dict | None = None,
                            tests: dict | None = None) -> list[dict]:
    """Return a list of missing/disabled/weak feature findings."""
    api = api or {}
    out: list[dict] = []

    def add(key: str, severity: str, detail: str):
        out.append({"feature": key, "severity": severity, "detail": detail})

    # Chainlink
    if not feats.get("chainlink_enabled"):
        add("chainlink", "P1", "Chainlink oracle not enabled / not reporting.")
    elif feats.get("chainlink_valid") is False or feats.get("chainlink_stale") is True:
        add("chainlink", "P1", "Chainlink anchor is stale or invalid.")

    # BTC fast price
    if not feats.get("btc_fast_price_enabled"):
        add("btc_fast_price", "P1", "BTC fast price feed missing / disabled.")
    elif feats.get("btc_fast_price_valid") is False:
        add("btc_fast_price", "P2", "BTC fast price feed present but currently invalid/stale.")

    # BTC pulse oracle gate
    if feats.get("btc_pulse_enabled") and not feats.get("btc_pulse_oracle_gate_active"):
        add("btc_pulse_oracle_gate", "P1", "BTC Pulse enabled but oracle gate not active.")

    # Bregman
    if feats.get("bregman_candidates_found") in (None,) and feats.get("bregman_certified_count") in (None,):
        add("bregman", "P1", "Bregman scanner disabled or emitting no diagnostics.")

    # News scanner
    if not feats.get("news_scanner_enabled"):
        add("news_scanner", "P2", "News scanner disabled.")
    else:
        ratio = _news_quality_ratio(feats)
        if ratio is not None and ratio < 0.1 and (feats.get("news_items_fetched") or 0):
            add("news_scanner", "P2", f"News scanner noisy: low used/fetched ratio ({ratio}).")

    # Grok evidence packet
    if feats.get("grok_enabled") and not feats.get("grok_has_api_key"):
        add("grok_evidence", "P2", "Grok enabled but no API key present (advisory layer idle).")
    if feats.get("grok_with_news_count") in (None, 0) and feats.get("news_scanner_enabled"):
        add("grok_evidence", "P3", "No evidence Grok received news packets yet.")

    # Paper attribution / fill realism / calibration
    if feats.get("paper_attribution_enabled") in (None, False):
        add("paper_attribution", "P2", "Paper strategy attribution not visible.")
    if feats.get("fill_realism_enabled") in (None, False):
        add("fill_realism", "P1", "Realistic-fill modeling not enabled / not visible.")
    if feats.get("brier") is None and feats.get("ece") is None:
        add("calibration", "P2", "Calibration metrics (Brier/ECE) missing.")

    # Tests
    if feats.get("tests_present") is False:
        add("tests", "P1", "Test suite not found.")
    elif feats.get("tests_passing") is False:
        add("tests", "P1", "Tests are failing.")

    # API endpoints missing
    missing_eps = [k for k, v in (api or {}).items()
                   if isinstance(v, dict) and v.get("ok") is False]
    if missing_eps:
        add("api_endpoints", "P3", f"Unreachable API endpoints: {', '.join(sorted(missing_eps))}.")

    return out


# ----------------------------------------------------------------------------- #
# Scorecard (0-100, explainable)
# ----------------------------------------------------------------------------- #
SCORE_WEIGHTS = {
    "safety": 25,
    "tests": 15,
    "runtime": 15,
    "feature_completeness": 20,
    "performance_trend": 15,
    "observability": 10,
}

# Features that count toward "feature completeness".
_COMPLETENESS_FEATURES = [
    "chainlink_enabled", "btc_fast_price_enabled", "btc_pulse_oracle_gate_active",
    "news_scanner_enabled", "fill_realism_enabled", "paper_attribution_enabled",
    "bregman_candidates_found", "grok_enabled",
]


def compute_scorecard(feats: dict, safety: dict, tests: dict,
                      runtime_available: bool, comparison: dict | None,
                      observability: dict | None = None) -> dict:
    """Compute a deterministic 0-100 bot-health score with per-component
    breakdown that is fully explainable in JSON."""
    feats = feats or {}
    safety = safety or {}
    tests = tests or {}
    comparison = comparison or {}
    observability = observability or {}
    comp: dict[str, dict] = {}

    # Safety (25): full unless WARN/CRITICAL.
    sstat = safety.get("status", "OK")
    if safety.get("critical") or sstat == "CRITICAL":
        s_safety = 0.0
    elif safety.get("warn") or sstat == "WARN":
        s_safety = SCORE_WEIGHTS["safety"] * 0.6
    else:
        s_safety = float(SCORE_WEIGHTS["safety"])
    comp["safety"] = {"score": round(s_safety, 2), "max": SCORE_WEIGHTS["safety"],
                      "reason": f"safety audit = {sstat}"}

    # Tests (15).
    if tests.get("present") and tests.get("passing"):
        s_tests = float(SCORE_WEIGHTS["tests"])
        treason = "tests present and passing"
    elif tests.get("present") and tests.get("passing") is False:
        s_tests = SCORE_WEIGHTS["tests"] * 0.3
        treason = "tests present but failing"
    elif tests.get("skipped"):
        s_tests = SCORE_WEIGHTS["tests"] * 0.5
        treason = "tests skipped (--skip-tests)"
    else:
        s_tests = 0.0
        treason = "tests missing / not run"
    comp["tests"] = {"score": round(s_tests, 2), "max": SCORE_WEIGHTS["tests"], "reason": treason}

    # Runtime availability (15): paper status readable + preflight ok.
    if runtime_available and feats.get("preflight_ok") is not False:
        s_rt = float(SCORE_WEIGHTS["runtime"])
        rreason = "paper-training status collected"
    elif runtime_available:
        s_rt = SCORE_WEIGHTS["runtime"] * 0.6
        rreason = "status collected but preflight not ok"
    else:
        s_rt = 0.0
        rreason = "no paper-training status available"
    comp["runtime"] = {"score": round(s_rt, 2), "max": SCORE_WEIGHTS["runtime"], "reason": rreason}

    # Feature completeness (20): fraction of expected features present/enabled.
    present = 0
    for k in _COMPLETENESS_FEATURES:
        v = feats.get(k)
        if v not in (None, False, 0):
            present += 1
    frac = present / len(_COMPLETENESS_FEATURES)
    s_feat = round(SCORE_WEIGHTS["feature_completeness"] * frac, 2)
    comp["feature_completeness"] = {
        "score": s_feat, "max": SCORE_WEIGHTS["feature_completeness"],
        "reason": f"{present}/{len(_COMPLETENESS_FEATURES)} key features active"}

    # Performance trend (15): neutral if no baseline.
    if not comparison.get("available"):
        s_perf = SCORE_WEIGHTS["performance_trend"] * 0.6
        preason = "no baseline (neutral)"
    elif comparison.get("regression"):
        s_perf = 0.0
        preason = "material regression vs baseline"
    else:
        n_imp = len(comparison.get("improved", []))
        n_deg = len(comparison.get("degraded", []))
        total = max(1, n_imp + n_deg)
        s_perf = round(SCORE_WEIGHTS["performance_trend"] * (n_imp / total), 2)
        preason = f"{n_imp} improved / {n_deg} degraded vs baseline"
    comp["performance_trend"] = {"score": round(s_perf, 2),
                                 "max": SCORE_WEIGHTS["performance_trend"], "reason": preason}

    # Observability (10): artifacts + logs + api snapshots collected.
    obs_points = 0
    obs_total = 3
    if observability.get("artifacts_found"):
        obs_points += 1
    if observability.get("logs_collected"):
        obs_points += 1
    if observability.get("api_ok"):
        obs_points += 1
    s_obs = round(SCORE_WEIGHTS["observability"] * (obs_points / obs_total), 2)
    comp["observability"] = {"score": s_obs, "max": SCORE_WEIGHTS["observability"],
                             "reason": f"{obs_points}/{obs_total} observability sources present"}

    total = round(sum(c["score"] for c in comp.values()), 2)
    return {"score": total, "max": 100, "components": comp}


# ----------------------------------------------------------------------------- #
# Algorithmic benchmark layer
# ----------------------------------------------------------------------------- #
# Each spec: (name, feature_key, direction, target, fail) where direction is
# "higher"/"lower"/"bool". A value at-or-better-than ``target`` => pass; between
# target and ``fail`` => warn; at-or-worse-than ``fail`` => fail; None => missing.
# Thresholds are quant defaults for a PAPER training bot, not live mandates.
BenchmarkSpec = tuple
BENCHMARK_SPECS: list[BenchmarkSpec] = [
    ("after_cost_pnl", "after_cost_pnl", "higher", 0.0, -5.0,
     "After-cost paper PnL/expectancy (net of fees+slippage)."),
    ("bregman_certified_profit", "bregman_certified_profit", "higher", 0.0, -1.0,
     "Certified Bregman opportunity profit (paper)."),
    ("btc_pulse_after_cost_pnl", "btc_pulse_after_cost_pnl", "higher", 0.0, -5.0,
     "BTC Pulse after-cost paper PnL."),
    ("win_rate_traded_only", "win_rate_traded_only", "higher", 0.5, 0.4,
     "Win rate over traded-only paper decisions."),
    ("sharpe", "sharpe", "higher", 1.0, 0.0, "Sharpe ratio (paper equity curve)."),
    ("sortino", "sortino", "higher", 1.5, 0.0, "Sortino ratio (downside-only)."),
    ("calmar", "calmar", "higher", 1.0, 0.0, "Calmar ratio (return / max drawdown)."),
    ("max_drawdown", "max_drawdown", "lower", 0.15, 0.25, "Max drawdown (fraction of equity)."),
    ("brier", "brier", "lower", 0.25, 0.33, "Brier score (probability calibration)."),
    ("ece", "ece", "lower", 0.05, 0.10, "Expected calibration error."),
    ("fill_realism_rejection_rate", "fill_realism_rejection_rate", "lower", 0.5, 0.8,
     "Realistic-fill (fantasy-fill) rejection rate; very high => feed/book problem."),
    ("exploration_validation_separated", "exploration_validation_separated", "bool", True, False,
     "Exploration trades are tracked separately from validation evidence."),
    ("paper_attribution_enabled", "paper_attribution_enabled", "bool", True, False,
     "Per-strategy paper attribution is available."),
    ("fill_realism_enabled", "fill_realism_enabled", "bool", True, False,
     "Realistic-fill modeling is enabled."),
]


def _benchmark_status(value: Optional[float], direction: str, target: Any,
                      fail: Any) -> str:
    if value is None:
        return "missing"
    if direction == "bool":
        return "pass" if bool(value) == bool(target) else "fail"
    v = _num(value)
    t, f = _num(target), _num(fail)
    if v is None or t is None or f is None:
        return "missing"
    if direction == "higher":
        if v >= t:
            return "pass"
        return "fail" if v <= f else "warn"
    # lower-is-better
    if v <= t:
        return "pass"
    return "fail" if v >= f else "warn"


def build_benchmarks(feats: dict) -> dict:
    """Build the algorithmic benchmark scorecard from extracted features.

    Returns ``{"benchmarks": [...], "summary": {pass, warn, fail, missing}}``.
    Each benchmark is ``{name, value, direction, target, fail_at, status,
    description}``. Pure + deterministic; no I/O.
    """
    feats = feats or {}
    rows: list[dict] = []
    counts = {"pass": 0, "warn": 0, "fail": 0, "missing": 0}
    for name, key, direction, target, fail, desc in BENCHMARK_SPECS:
        value = feats.get(key)
        status = _benchmark_status(value, direction, target, fail)
        counts[status] += 1
        rows.append({
            "name": name, "value": value, "direction": direction,
            "target": target, "fail_at": fail, "status": status,
            "description": desc,
        })
    return {"benchmarks": rows, "summary": counts,
            "failing": [r["name"] for r in rows if r["status"] == "fail"],
            "warning": [r["name"] for r in rows if r["status"] == "warn"]}


# ----------------------------------------------------------------------------- #
# Cross-surface consistency checks
# ----------------------------------------------------------------------------- #
def detect_inconsistencies(feats: dict, status: dict | None = None,
                           api: dict | None = None,
                           equity_tolerance_pct: float = 0.01) -> list[dict]:
    """Detect inconsistencies across collected surfaces (read-only).

    Currently checks: dashboard equity vs paper-training equity; live-detected
    disagreement between training status and the dashboard API. Returns a list of
    ``{check, severity, detail, values}`` (empty when everything agrees).
    """
    feats = feats or {}
    status = status or {}
    api = api or {}
    out: list[dict] = []

    # --- dashboard equity vs paper-training equity ---
    paper_eq = _num(feats.get("equity"))
    dash_eq = _num(feats.get("dashboard_equity"))
    if paper_eq is not None and dash_eq is not None:
        denom = max(abs(paper_eq), abs(dash_eq), 1.0)
        rel = abs(paper_eq - dash_eq) / denom
        if rel > equity_tolerance_pct:
            out.append({
                "check": "equity_mismatch", "severity": "WARN",
                "detail": (f"dashboard equity ${dash_eq} vs paper-training equity "
                           f"${paper_eq} differ by {round(rel * 100, 2)}% "
                           "(separate surfaces; expected to roughly agree)."),
                "values": {"dashboard_equity": dash_eq, "paper_equity": paper_eq,
                           "rel_diff_pct": round(rel * 100, 4)},
            })

    # --- live-detected disagreement ---
    status_live = _get(status, "safety", "live_detected")
    api_live = _get(api, "state", "live_detected")
    if status_live is not None and api_live is not None and bool(status_live) != bool(api_live):
        out.append({
            "check": "live_detected_mismatch", "severity": "CRITICAL",
            "detail": (f"training status live_detected={status_live} but dashboard "
                       f"API live_detected={api_live}."),
            "values": {"status_live_detected": bool(status_live),
                       "api_live_detected": bool(api_live)},
        })

    # --- after-cost PnL exceeding gross PnL (cost accounting sanity) ---
    after = _num(feats.get("after_cost_pnl"))
    total = _num(feats.get("total_pnl"))
    if after is not None and total is not None and after > total + 1e-9:
        out.append({
            "check": "after_cost_exceeds_gross", "severity": "WARN",
            "detail": (f"after-cost PnL {after} exceeds gross/total PnL {total} — "
                       "cost accounting may be off."),
            "values": {"after_cost_pnl": after, "total_pnl": total},
        })

    return out


# ----------------------------------------------------------------------------- #
# Quant responsibilities matrix (documentation surfaced in the report)
# ----------------------------------------------------------------------------- #
# domain -> {owner, responsibilities, evidence_features}. ``evidence_features``
# are feature keys whose presence demonstrates the domain is observable.
QUANT_RESPONSIBILITIES: dict[str, dict] = {
    "data_ingestion": {
        "owner": "Data / market-data engineering",
        "responsibilities": [
            "Ingest Polymarket gamma/CLOB market data (read-only)",
            "Read Chainlink BTC/USD anchor + Coinbase fast spot feed",
            "Fetch market-news headlines (read-only)",
        ],
        "evidence_features": ["scanned_markets", "chainlink_enabled",
                              "btc_fast_price_enabled", "news_scanner_enabled"],
    },
    "preprocessing_features": {
        "owner": "Feature engineering",
        "responsibilities": [
            "Normalize/timestamp/dedupe inputs; build short-horizon returns",
            "Score + sanitize news evidence; cap feature nudges",
            "Apply the market-scan universe limits",
        ],
        "evidence_features": ["news_items_used", "btc_fast_price_disagreement_bps",
                              "market_scan_limit_effective"],
    },
    "statistical_modeling": {
        "owner": "Quant research / modeling",
        "responsibilities": [
            "Probability estimation + calibration (isotonic/Platt)",
            "Track Brier/ECE; guard against overfitting",
        ],
        "evidence_features": ["brier", "ece", "win_rate_traded_only"],
    },
    "bregman_signals": {
        "owner": "Quant research (convex/Bregman)",
        "responsibilities": [
            "Group markets; certify Bregman arbitrage-free opportunities (paper)",
            "Track false-positive rate + certified profit",
        ],
        "evidence_features": ["bregman_candidates_found", "bregman_certified_count",
                              "bregman_certified_profit", "bregman_false_positive_rate"],
    },
    "risk_portfolio": {
        "owner": "Risk / portfolio",
        "responsibilities": [
            "Deterministic RiskEngine gate on every paper order",
            "Exposure/daily-loss caps; drawdown control",
        ],
        "evidence_features": ["preflight_ok", "open_positions", "max_drawdown"],
    },
    "backtest_simulation": {
        "owner": "Simulation / backtest",
        "responsibilities": [
            "Paper OMS + realistic fills; after-cost expectancy",
            "Resolve labels; record closed trades",
        ],
        "evidence_features": ["paper_trades", "closed_positions", "after_cost_pnl"],
    },
    "robustness": {
        "owner": "Quant validation",
        "responsibilities": [
            "Exploration-vs-validation separation; regime/stress checks",
            "Risk-adjusted performance (Sharpe/Sortino/Calmar)",
        ],
        "evidence_features": ["exploration_validation_separated", "sharpe",
                              "sortino", "calmar"],
    },
    "clobv2_execution": {
        "owner": "Execution (CLOB v2, paper)",
        "responsibilities": [
            "Read-only CLOB v2 book freshness; realistic-fill modeling",
            "Reject fantasy fills; never submit real orders (paper)",
        ],
        "evidence_features": ["fill_realism_enabled", "fill_realism_rejection_rate"],
    },
    "monitoring": {
        "owner": "MLOps / monitoring",
        "responsibilities": [
            "Health/benchmark reporting; test suite green",
            "Uptime + drift/kill-switch monitoring",
        ],
        "evidence_features": ["tests_passing", "runtime_minutes"],
    },
    "compliance_security_ops": {
        "owner": "Compliance / security / ops",
        "responsibilities": [
            "PAPER-only enforcement; no live/wallet/order paths",
            "Secret redaction; forbidden-live-flag audit",
        ],
        "evidence_features": ["live_detected", "preflight_ok"],
    },
}


def build_quant_responsibilities(feats: dict | None = None) -> dict:
    """Return the quant responsibilities matrix annotated with observability
    coverage from the current features (``covered`` / ``gap``)."""
    feats = feats or {}
    out: dict[str, dict] = {}
    for domain, spec in QUANT_RESPONSIBILITIES.items():
        ev = spec.get("evidence_features", [])
        present = [k for k in ev if feats.get(k) not in (None,)]
        out[domain] = {
            "owner": spec["owner"],
            "responsibilities": list(spec["responsibilities"]),
            "evidence_features": list(ev),
            "observed_features": present,
            "coverage": "covered" if present else "gap",
        }
    return out
