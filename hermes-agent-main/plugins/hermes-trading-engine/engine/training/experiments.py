"""Paper-training experiment harness (PASS-9 — orchestration + diagnostics only).

Runs controlled paper-training PROFILES (strict full system, Bregman-only,
directional-only, shadow ablations, ...), compares them with the Pass-8 inspection
metrics, scores REAL (realistic, after-cost, readiness-eligible) edge by strategy
bucket, classifies bottlenecks, and recommends the next algorithmic upgrade from
evidence.

Hard safety: every profile is paper-only and must keep strict paper realism +
readiness exclusions. Risky ablations are SHADOW-ONLY (they never open unsafe
trades and never count toward readiness). PAPER ONLY — no live path, no fake fills.
"""

from __future__ import annotations

import time
from typing import Optional

SCHEMA_VERSION = "experiment_comparison/1.0"

# --- profile definitions ----------------------------------------------------
# Each profile's ``config`` is a dict of TrainingConfig overrides (always merged
# onto mode="paper_train"). ``shadow_only_ablations`` documents which gate is
# being studied as a shadow counterfactual (it never opens unsafe trades).
BUILTIN_PROFILES: dict = {
    "strict_full_system": {
        "description": "Strict full system: Bregman-first + directional + active "
                       "learning + profitability-first + correlation gate + strict realism.",
        "config": {},  # all safe defaults
        "shadow_only_ablations": [],
    },
    "bregman_only": {
        "description": "Bregman/ABCAS arbitrage only; directional + exploration "
                       "shadow-only (logged, never opened).",
        "config": {"directional_execution_enabled": False, "exploration_enabled": False},
        "shadow_only_ablations": ["directional", "exploration"],
    },
    "directional_only": {
        "description": "Directional exploit only; Bregman scans + certifies but does "
                       "not open (shadow); exploration off.",
        "config": {"bregman_execution_enabled": False, "exploration_enabled": False},
        "shadow_only_ablations": ["bregman", "exploration"],
    },
    "bregman_shadow_diagnostics": {
        "description": "Bregman scans full raw catalog + certifies, execution shadow-"
                       "only; logs why bundles would/would not trade.",
        "config": {"bregman_execution_enabled": False, "directional_execution_enabled": False,
                   "exploration_enabled": False},
        "shadow_only_ablations": ["bregman", "directional", "exploration"],
    },
    "active_learning_diagnostics": {
        "description": "Active learning runs (tiny paper); exploit directional off; "
                       "readiness excludes exploration; logs category/cluster coverage.",
        "config": {"directional_execution_enabled": False, "exploration_enabled": True,
                   "active_learning_enabled": True},
        "shadow_only_ablations": ["directional"],
    },
    "correlation_shadow_ablation": {
        "description": "Correlation gate still computes + blocks (no real trade); "
                       "blocked correlated candidates are counterfactual shadow only.",
        "config": {},  # gate stays ON; blocks are logged as shadow (never readiness)
        "shadow_only_ablations": ["correlation_blocked_counterfactual"],
    },
    "profitability_shadow_ablation": {
        "description": "Profitability governor still computes + rejects; negative-after-"
                       "cost candidates are counterfactual shadow only.",
        "config": {},  # governor stays ON; rejects are logged (never readiness)
        "shadow_only_ablations": ["negative_after_cost_counterfactual"],
    },
}

# config keys that, if set unsafely, must FAIL safety validation
_FORBIDDEN_UNSAFE = {
    "allow_pm_reference_price_fills": True,
    "allow_offline_stub_trading": True,
    "reject_on_stale_book": False,
    "require_executable_ask": False,
    "reject_missing_ask": False,
    "exploration_count_toward_readiness": True,
}


def load_profiles(path: Optional[str] = None) -> dict:
    """Load experiment profiles from JSON (if present) merged onto the built-ins."""
    profiles = {k: dict(v) for k, v in BUILTIN_PROFILES.items()}
    if path:
        try:
            import json
            from pathlib import Path
            p = Path(path)
            if p.exists():
                loaded = json.loads(p.read_text(encoding="utf-8"))
                for name, prof in (loaded.get("profiles", loaded) or {}).items():
                    profiles[name] = prof
        except Exception:  # noqa: BLE001 — never crash on a bad profile file
            pass
    return profiles


def validate_profile_safety(profile: dict) -> "tuple[bool, list]":
    """Refuse any profile that could let unrealistic fills count as real edge or
    enable a live path. Returns (ok, errors)."""
    errors: list = []
    cfg = (profile or {}).get("config", {}) or {}
    for key, unsafe_val in _FORBIDDEN_UNSAFE.items():
        if key in cfg and cfg[key] == unsafe_val:
            errors.append(f"unsafe_override:{key}={cfg[key]}")
    for live_flag in ("micro_live_enabled", "guarded_live_enabled",
                      "production_review_enable_production_execution"):
        if cfg.get(live_flag):
            errors.append(f"live_flag_set:{live_flag}")
    if str(cfg.get("mode", "paper_train")) != "paper_train":
        errors.append(f"non_paper_mode:{cfg.get('mode')}")
    return (len(errors) == 0, errors)


def profile_config(name: str, profile: dict):
    """Build a validated paper-only TrainingConfig for a profile (raises on unsafe)."""
    from engine.training.config import TrainingConfig
    ok, errors = validate_profile_safety(profile)
    if not ok:
        raise ValueError(f"profile '{name}' failed safety validation: {errors}")
    overrides = dict((profile or {}).get("config", {}) or {})
    overrides.pop("mode", None)
    return TrainingConfig(mode="paper_train", **overrides)


def run_profile(name: str, profile: dict, *, catalog: Optional[list] = None,
                ticks: int = 1, data_dir=None, now: Optional[float] = None) -> dict:
    """Run a single paper-training profile in-process and return its Pass-8
    inspection summary. PAPER ONLY; safety-validated before running."""
    from engine.training.polymarket_trainer import PolymarketPaperTrainer
    cfg = profile_config(name, profile)
    trainer = PolymarketPaperTrainer(cfg, data_dir=data_dir)
    cat = catalog or []
    for _ in range(max(1, int(ticks))):
        try:
            trainer.run_tick(cat, now=now)
        except Exception:  # noqa: BLE001 — a bad tick must not abort the experiment
            break
    summary = trainer.inspection_summary()
    summary["profile"] = {"name": name, "description": profile.get("description", ""),
                          "shadow_only_ablations": profile.get("shadow_only_ablations", []),
                          "config_overrides": (profile or {}).get("config", {})}
    return summary


# --- edge scoring + bottleneck classification -------------------------------
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x or 0.0)))


def edge_score(summary: dict) -> dict:
    """Conservative, explainable REAL-edge score for a profile (readiness-eligible
    only). Shadow-only / unrealistic results are penalized; tiny samples get low
    confidence. Bregman certified after-cost-positive bundles earn a low-prediction-
    dependence credit."""
    rd = summary.get("readiness", {}) or {}
    pe = summary.get("paper_realism", {}) or {}
    prk = summary.get("profitability_ranking", {}) or {}
    rw = summary.get("rejection_waterfall", {}) or {}
    cr = summary.get("correlation_risk", {}) or {}
    bf = summary.get("bregman_funnel", {}) or {}
    n_real = int(rd.get("readiness_trade_count", 0) or 0)
    readiness_pnl = float(rd.get("readiness_pnl", 0.0) or 0.0)
    roi = float(prk.get("avg_after_cost_roi_executed", 0.0) or 0.0)
    shadow_pnl = float(pe.get("shadow_theoretical_pnl", 0.0) or 0.0)
    ref_pnl = float(pe.get("reference_fill_theoretical_pnl", 0.0) or 0.0)

    readiness_pnl_score = _clamp01(0.5 + readiness_pnl)            # 0.5 neutral at 0
    after_cost_roi_score = _clamp01(roi * 50.0)                    # 2% ROI -> 1.0
    fill_realism_score = 1.0 if (not pe.get("reference_price_fills_allowed_for_exploit", False)
                                 and ref_pnl == 0.0) else 0.2
    sample_size_score = _clamp01(n_real / 10.0)
    bregman_credit = 0.15 if bf.get("bundles_opened", 0) > 0 else 0.0
    # penalties
    shadow_dependency_penalty = 0.3 if (n_real == 0 and shadow_pnl != 0.0) else 0.0
    total_rej = int(rw.get("total_rejections", 0) or 0)
    rejection_bottleneck_penalty = _clamp01(total_rej / 200.0) * 0.2
    corr_blocks = sum(int(cr.get(k, 0) or 0) for k in (
        "blocked_same_market", "blocked_same_cluster", "blocked_same_event"))
    correlation_concentration_penalty = _clamp01(corr_blocks / 50.0) * 0.1
    unrealistic_fill_penalty = 0.5 if ref_pnl != 0.0 else 0.0
    comps = {
        "readiness_pnl_score": round(readiness_pnl_score, 4),
        "after_cost_roi_score": round(after_cost_roi_score, 4),
        "fill_realism_score": round(fill_realism_score, 4),
        "sample_size_score": round(sample_size_score, 4),
        "bregman_low_prediction_dependence_credit": bregman_credit,
        "shadow_dependency_penalty": -shadow_dependency_penalty,
        "rejection_bottleneck_penalty": -round(rejection_bottleneck_penalty, 4),
        "correlation_concentration_penalty": -round(correlation_concentration_penalty, 4),
        "unrealistic_fill_penalty": -unrealistic_fill_penalty,
    }
    score = round(sum(comps.values()), 4)
    confidence = "high" if n_real >= 10 else ("medium" if n_real >= 3 else "low")
    if n_real == 0:
        reason = "no readiness-eligible trades — real edge unproven (shadow/theoretical only)"
    elif unrealistic_fill_penalty:
        reason = "unrealistic fills detected — score heavily penalized"
    else:
        reason = f"{n_real} readiness-eligible trades; roi_score={after_cost_roi_score:.2f}"
    return {"edge_score": score, "edge_score_components": comps,
            "edge_score_reason": reason, "confidence_level": confidence}


def bregman_bottleneck(bf: dict) -> str:
    disc = int(bf.get("raw_groups_discovered", 0) or 0)
    cert = int(bf.get("certified_opportunities", 0) or 0)
    opened = int(bf.get("bundles_opened", 0) or 0)
    if disc == 0:
        return "no_groups_found"
    if cert == 0:
        return "groups_found_not_certified"
    if opened == 0:
        reasons = bf.get("rejected_by_reason", {}) or {}
        risk_keys = ("capital_cap_per_tick", "max_bundles_per_tick", "max_open_bundles",
                     "bregman_overlapping_bundle", "bregman_duplicate_bundle")
        if any(reasons.get(k) for k in risk_keys):
            return "executable_blocked_by_risk"
        return "certified_not_executable"
    return "opened_and_promising"


def directional_bottleneck(summary: dict) -> str:
    prk = summary.get("profitability_ranking", {}) or {}
    sp = summary.get("strategy_priority", {}) or {}
    rd = summary.get("readiness", {}) or {}
    considered = int(prk.get("candidates_annotated", 0) or 0)
    pos = int(prk.get("directional_after_cost_positive", 0) or 0)
    neg = int(prk.get("candidates_rejected_negative_after_cost", 0) or 0)
    if considered <= 2:
        return "too_few_candidates"
    if neg > pos and pos == 0:
        return "model_edge_not_after_cost_positive"
    if int(sp.get("directional_trades_blocked_by_bregman_reservation", 0) or 0) > 0 and pos == 0:
        return "starved_by_bregman_priority"
    if pos > 0 and float(rd.get("directional_realistic_pnl", 0.0) or 0.0) < 0:
        return "opened_but_unprofitable"
    if pos > 0:
        return "opened_and_promising"
    return "execution_quality_poor"


def active_learning_bottleneck(al: dict) -> str:
    if not al.get("active_learning_enabled", False):
        return "disabled"
    considered = int(al.get("active_learning_candidates_considered", 0) or 0)
    selected = int(al.get("active_learning_candidates_selected", 0) or 0)
    if considered == 0:
        return "no_eligible_candidates"
    if selected == 0:
        if int(al.get("exploration_rejected_by_realism", 0) or 0) > 0:
            return "blocked_by_realism"
        if int(al.get("exploration_rejected_by_budget", 0) or 0) > 0:
            return "blocked_by_budget"
        if int(al.get("exploration_rejected_by_collision", 0) or 0) > 0:
            return "blocked_by_correlation"
        return "no_eligible_candidates"
    if int(al.get("completed_feedback_count", 0) or 0) > 0:
        return "useful_feedback_generated"
    return "selected_pending_feedback"


# --- strategy bucket leaderboard --------------------------------------------
def strategy_bucket_leaderboard(summary: dict) -> list:
    """Rank strategy buckets by realistic, readiness-eligible edge (NOT shadow)."""
    rd = summary.get("readiness", {}) or {}
    pe = summary.get("paper_realism", {}) or {}
    prk = summary.get("profitability_ranking", {}) or {}
    al = summary.get("active_learning", {}) or {}
    bf = summary.get("bregman_funnel", {}) or {}
    tl = summary.get("trade_ledger_summary", {}) or {}
    buckets = [
        {"bucket": "bregman_certified_realistic", "realistic": "yes",
         "trades_opened": tl.get("bregman_legs", 0),
         "readiness_pnl": rd.get("bregman_realistic_pnl", 0.0),
         "candidates": bf.get("certified_opportunities", 0)},
        {"bucket": "directional_realistic_exploit", "realistic": "yes",
         "trades_opened": tl.get("directional_trades", 0),
         "readiness_pnl": rd.get("directional_realistic_pnl", 0.0),
         "candidates": prk.get("directional_after_cost_positive", 0)},
        {"bucket": "active_learning_exploration", "realistic": "excluded_from_readiness",
         "trades_opened": al.get("exploration_trades_opened", 0),
         "readiness_pnl": 0.0, "exploration_pnl": al.get("exploration_pnl", 0.0),
         "candidates": al.get("active_learning_candidates_selected", 0)},
        {"bucket": "shadow_theoretical", "realistic": "no",
         "trades_opened": 0, "readiness_pnl": 0.0,
         "shadow_pnl": pe.get("shadow_theoretical_pnl", 0.0),
         "candidates": pe.get("shadow_trade_count", 0)},
    ]
    # rank: realistic readiness PnL first; shadow can NEVER outrank realistic.
    def _key(b):
        realistic = 1 if b["realistic"] == "yes" else 0
        return (realistic, float(b.get("readiness_pnl", 0.0) or 0.0),
                int(b.get("trades_opened", 0) or 0))
    ranked = sorted(buckets, key=_key, reverse=True)
    for i, b in enumerate(ranked):
        b["rank"] = i + 1
    return ranked


def _recommended_next_pass(comparison: dict) -> dict:
    """Deterministic next-upgrade recommendation from the aggregated bottlenecks."""
    bregman_bn = comparison.get("bregman_comparison", {}).get("dominant_bottleneck")
    directional_bn = comparison.get("directional_comparison", {}).get("dominant_bottleneck")
    al_bn = comparison.get("active_learning_comparison", {}).get("dominant_bottleneck")
    # Bregman is the primary strategy -> its bottleneck dominates the recommendation.
    table = {
        "no_groups_found": ("bregman_grouping", "Improve grouping/constraint discovery: "
                            "few/no complete sets found over the eligible catalog."),
        "groups_found_not_certified": ("bregman_certification", "Improve completeness/"
                            "exhaustiveness validation + certifier diagnostics."),
        "certified_not_executable": ("paper_execution_realism", "Improve live book "
                            "coverage / leg selection / quote freshness / depth-aware sizing."),
        "executable_blocked_by_risk": ("risk_budget_allocation", "Inspect per-tick budget/"
                            "slot caps + duplicate/overlapping bundle + correlation gating."),
        "opened_and_promising": ("scale_validation", "Bregman is opening promising bundles "
                            "— add walk-forward + bootstrap CI validation before scaling."),
    }
    if bregman_bn in table and bregman_bn not in ("opened_and_promising",):
        code, msg = table[bregman_bn]
        return {"focus": "bregman", "code": code, "bottleneck": bregman_bn, "message": msg}
    if directional_bn == "model_edge_not_after_cost_positive":
        return {"focus": "directional", "code": "directional_probability_model",
                "bottleneck": directional_bn,
                "message": "Directional model edge does not survive costs — improve "
                           "probability model / news features / executable price selection."}
    if al_bn in ("no_eligible_candidates", "blocked_by_realism"):
        return {"focus": "active_learning", "code": "exploration_eligibility",
                "bottleneck": al_bn,
                "message": "Active learning selects nothing — widen (still-safe) "
                           "exploration eligibility or check annotation flow."}
    if bregman_bn == "opened_and_promising":
        code, msg = table["opened_and_promising"]
        return {"focus": "bregman", "code": code, "bottleneck": bregman_bn, "message": msg}
    return {"focus": "observability", "code": "more_data",
            "bottleneck": "insufficient_signal",
            "message": "Insufficient trade/edge signal across profiles — run longer paper "
                       "experiments over a live catalog to accumulate evidence."}


def build_comparison(run_id: str, profile_summaries: dict) -> dict:
    """Aggregate per-profile inspection summaries into the comparison schema."""
    profiles = {}
    edge_scoreboard = {}
    breg_bottlenecks = {}
    dir_bottlenecks = {}
    al_bottlenecks = {}
    for name, s in profile_summaries.items():
        es = edge_score(s)
        edge_scoreboard[name] = es
        bf = s.get("bregman_funnel", {})
        breg_bottlenecks[name] = bregman_bottleneck(bf)
        dir_bottlenecks[name] = directional_bottleneck(s)
        al_bottlenecks[name] = active_learning_bottleneck(s.get("active_learning", {}))
        run = s.get("run", {})
        rd = s.get("readiness", {})
        profiles[name] = {
            "description": s.get("profile", {}).get("description", ""),
            "shadow_only_ablations": s.get("profile", {}).get("shadow_only_ablations", []),
            "bregman_bundles_opened": bf.get("bundles_opened", 0),
            "directional_trades_opened": run.get("directional_trades_opened", 0),
            "exploration_trades_opened": run.get("exploration_trades_opened", 0),
            "shadow_only_opportunities": run.get("shadow_only_opportunities", 0),
            "realistic_trades": run.get("realistic_trades", 0),
            "readiness_pnl": rd.get("readiness_pnl", 0.0),
            "realistic_pnl": run.get("realistic_pnl", 0.0),
            "edge_score": es["edge_score"], "confidence": es["confidence_level"],
            "leaderboard": strategy_bucket_leaderboard(s),
        }

    def _dominant(d):
        from collections import Counter
        c = Counter(d.values())
        return c.most_common(1)[0][0] if c else "unknown"

    def _sub(key):
        return {name: s.get(key, {}) for name, s in profile_summaries.items()}

    comparison = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "paper_only": True,
        "profiles": profiles,
        "strategy_bucket_comparison": {name: p["leaderboard"] for name, p in profiles.items()},
        "bregman_comparison": {"by_profile": _sub("bregman_funnel"),
                               "bottlenecks": breg_bottlenecks,
                               "dominant_bottleneck": _dominant(breg_bottlenecks)},
        "directional_comparison": {"bottlenecks": dir_bottlenecks,
                                   "dominant_bottleneck": _dominant(dir_bottlenecks)},
        "active_learning_comparison": {"by_profile": _sub("active_learning"),
                                       "bottlenecks": al_bottlenecks,
                                       "dominant_bottleneck": _dominant(al_bottlenecks)},
        "paper_realism_comparison": _sub("paper_realism"),
        "profitability_comparison": _sub("profitability_ranking"),
        "correlation_comparison": _sub("correlation_risk"),
        "readiness_comparison": _sub("readiness"),
        "rejection_waterfall_comparison": _sub("rejection_waterfall"),
        "edge_scoreboard": edge_scoreboard,
    }
    comparison["recommended_next_pass"] = _recommended_next_pass(comparison)
    # best readiness edge (realistic only)
    best = max(profiles.items(), key=lambda kv: (kv[1]["readiness_pnl"], kv[1]["edge_score"]),
               default=(None, None))
    comparison["best_readiness_edge"] = {"profile": best[0],
                                         "edge_score": (best[1]["edge_score"] if best[1] else None)}
    return comparison


def run_manifest(run_id: str, profiles: list, comparison: dict, *, command: str = "",
                 duration_s: float = 0.0, safety: Optional[dict] = None) -> dict:
    """Reproducibility manifest (NO secrets)."""
    commit = ""
    try:
        import subprocess
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                         stderr=subprocess.DEVNULL).decode().strip()
    except Exception:  # noqa: BLE001
        commit = ""
    return {
        "run_id": run_id, "git_commit": commit,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profiles": list(profiles), "command": command,
        "runtime_seconds": round(float(duration_s), 2),
        "paper_only": True, "live_trading_disabled": True,
        "safety_validation": safety or {"all_profiles_safe": True},
        "recommended_next_pass": comparison.get("recommended_next_pass", {}),
        "metrics_path": f"metrics/experiments/{run_id}/experiment_comparison.json",
        "report_path": f"reports/experiments/{run_id}/experiment_comparison.md",
    }


def console_summary(comparison: dict) -> str:
    profs = comparison.get("profiles", {})
    best = comparison.get("best_readiness_edge", {})
    rec = comparison.get("recommended_next_pass", {})
    # aggregate bregman funnel across profiles
    disc = cert = opened = 0
    dir_real = expl = 0
    for s in comparison.get("bregman_comparison", {}).get("by_profile", {}).values():
        disc += int(s.get("raw_groups_discovered", 0) or 0)
        cert += int(s.get("certified_opportunities", 0) or 0)
        opened += int(s.get("bundles_opened", 0) or 0)
    for p in profs.values():
        dir_real += int(p.get("directional_trades_opened", 0) or 0)
        expl += int(p.get("exploration_trades_opened", 0) or 0)
    return "\n".join([
        f"Run ID: {comparison.get('run_id')}",
        f"Profiles compared: {len(profs)}",
        f"Best readiness edge: {best.get('profile')} (score {best.get('edge_score')})",
        f"Bregman groups discovered/certified/opened: {disc}/{cert}/{opened}",
        f"Directional realistic trades: {dir_real}",
        f"Exploration trades: {expl}",
        "Unrealistic fills counted as real: 0",
        "Random exploration trades: 0",
        f"Top bottleneck: {comparison.get('bregman_comparison', {}).get('dominant_bottleneck')}",
        f"Recommended next pass: {rec.get('focus')} / {rec.get('code')}",
        f"Comparison report: reports/experiments/{comparison.get('run_id')}/experiment_comparison.md",
        f"Comparison metrics: metrics/experiments/{comparison.get('run_id')}/experiment_comparison.json",
    ])


def _diff_table(comparison: dict) -> list:
    L = ["| Profile | Bundles | Directional | Exploration | Shadow | Readiness PnL | "
         "Realistic PnL | Edge Score | Confidence |", "|---|---|---|---|---|---|---|---|---|"]
    for name, p in comparison.get("profiles", {}).items():
        L.append(f"| {name} | {p['bregman_bundles_opened']} | {p['directional_trades_opened']} | "
                 f"{p['exploration_trades_opened']} | {p['shadow_only_opportunities']} | "
                 f"{p['readiness_pnl']} | {p['realistic_pnl']} | {p['edge_score']} | "
                 f"{p['confidence']} |")
    return L


def comparison_to_markdown(comparison: dict) -> str:
    L: list = []
    L.append("# Paper-Training Experiment Comparison")
    L.append("")
    L.append(f"_PAPER ONLY · run {comparison.get('run_id')} · "
             f"{comparison.get('created_at')} · no live trading._")
    L.append("")
    rec = comparison.get("recommended_next_pass", {})
    best = comparison.get("best_readiness_edge", {})

    L.append("## Executive Summary")
    L.append(f"- profiles compared: {len(comparison.get('profiles', {}))}")
    L.append(f"- best readiness edge: **{best.get('profile')}** (score {best.get('edge_score')})")
    L.append(f"- Bregman dominant bottleneck: "
             f"{comparison.get('bregman_comparison', {}).get('dominant_bottleneck')}")
    L.append(f"- recommended next pass: **{rec.get('focus')} / {rec.get('code')}** — {rec.get('message')}")
    L.append("")

    L.append("## Profile Matrix")
    L += _diff_table(comparison)
    L.append("")

    L.append("## Strategy Bucket Leaderboard")
    for name, lb in comparison.get("strategy_bucket_comparison", {}).items():
        L.append(f"### {name}")
        for b in lb:
            L.append(f"- #{b['rank']} {b['bucket']} (realistic={b['realistic']}): "
                     f"trades={b.get('trades_opened')} readiness_pnl={b.get('readiness_pnl')} "
                     f"candidates={b.get('candidates')}")
    L.append("")

    L.append("## Bregman / ABCAS Performance")
    bc = comparison.get("bregman_comparison", {})
    for name, bf in bc.get("by_profile", {}).items():
        L.append(f"- {name}: scanned={bf.get('raw_catalog_markets_scanned')} "
                 f"discovered={bf.get('raw_groups_discovered')} "
                 f"certified={bf.get('certified_opportunities')} "
                 f"opened={bf.get('bundles_opened')} "
                 f"bottleneck={bc.get('bottlenecks', {}).get(name)}")
    L.append("")

    L.append("## Directional Performance")
    for name, bn in comparison.get("directional_comparison", {}).get("bottlenecks", {}).items():
        L.append(f"- {name}: bottleneck={bn}")
    L.append("")

    L.append("## Active Learning Diagnostics")
    for name, bn in comparison.get("active_learning_comparison", {}).get("bottlenecks", {}).items():
        L.append(f"- {name}: bottleneck={bn}")
    L.append("")

    L.append("## Paper Realism Comparison")
    for name, pe in comparison.get("paper_realism_comparison", {}).items():
        L.append(f"- {name}: realistic={pe.get('realistic_trade_count')} "
                 f"reference_fills_blocked={pe.get('reference_fills_blocked')} "
                 f"readiness_pnl={pe.get('readiness_pnl')}")
    L.append("")

    L.append("## Profitability Ranking Comparison")
    for name, prk in comparison.get("profitability_comparison", {}).items():
        L.append(f"- {name}: annotated={prk.get('candidates_annotated')} "
                 f"directional_positive={prk.get('directional_after_cost_positive')} "
                 f"negative_rejected={prk.get('candidates_rejected_negative_after_cost')}")
    L.append("")

    L.append("## Correlation Risk Comparison")
    for name, cr in comparison.get("correlation_comparison", {}).items():
        L.append(f"- {name}: gate={cr.get('correlation_gate_enabled')} "
                 f"blocked_market={cr.get('blocked_same_market')} "
                 f"blocked_cluster={cr.get('blocked_same_cluster')}")
    L.append("")

    L.append("## Rejection Waterfall Comparison")
    for name, rw in comparison.get("rejection_waterfall_comparison", {}).items():
        top = rw.get("ranked_reasons", [])[:3]
        L.append(f"- {name}: total={rw.get('total_rejections')} top={top}")
    L.append("")

    L.append("## Readiness Comparison")
    for name, rd in comparison.get("readiness_comparison", {}).items():
        L.append(f"- {name}: readiness_pnl={rd.get('readiness_pnl')} "
                 f"trades={rd.get('readiness_trade_count')} live={rd.get('live_trading_enabled')}")
    L.append("")

    L.append("## Edge Attribution")
    for name, es in comparison.get("edge_scoreboard", {}).items():
        L.append(f"- {name}: edge_score={es['edge_score']} ({es['confidence_level']}) "
                 f"— {es['edge_score_reason']}")
    L.append("")

    L.append("## Bottleneck Diagnosis")
    L.append(f"- Bregman: {comparison.get('bregman_comparison', {}).get('dominant_bottleneck')}")
    L.append(f"- Directional: {comparison.get('directional_comparison', {}).get('dominant_bottleneck')}")
    L.append(f"- Active learning: {comparison.get('active_learning_comparison', {}).get('dominant_bottleneck')}")
    L.append("")

    L.append("## Recommended Next Pass")
    L.append(f"- **{rec.get('focus')} / {rec.get('code')}** ({rec.get('bottleneck')})")
    L.append(f"- {rec.get('message')}")
    L.append("")
    return "\n".join(L)


COMPARISON_SECTIONS = [
    "Executive Summary", "Profile Matrix", "Strategy Bucket Leaderboard",
    "Bregman / ABCAS Performance", "Directional Performance",
    "Active Learning Diagnostics", "Paper Realism Comparison",
    "Profitability Ranking Comparison", "Correlation Risk Comparison",
    "Rejection Waterfall Comparison", "Readiness Comparison", "Edge Attribution",
    "Bottleneck Diagnosis", "Recommended Next Pass",
]


def validate_comparison_report(markdown: str) -> list:
    return [s for s in COMPARISON_SECTIONS if f"## {s}" not in markdown]
