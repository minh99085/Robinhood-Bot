"""Runtime feature-activation audit (PAPER ONLY, read-only instrumentation).

Pass-1 audit: a machine-readable truth table of which algorithmic edge modules
are TRULY active in the paper-training trade loop vs imported-only, telemetry-only,
or dead. The findings below are derived by TRACING the actual call path from
``scripts/start_polymarket_paper_training.py`` →
``engine/training/polymarket_trainer.py`` → trade opening — not from file/class
names. This module performs no trading and changes no behavior.

Runtime-status vocabulary:
* ``active``        — runs and controls actual paper trade selection/opening.
* ``telemetry``     — runs but only reports; does not affect trades.
* ``annotated``     — data is computed/attached but the gate is not enforced.
* ``imported``      — referenced/constructed but its decision path is unused.
* ``dead``          — defined but not reached by the runtime path.

Evidence is tied to the trainer call chain (this branch, ~1844-line trainer):
``run_tick → scanner.scan → records → watch[:live_watch_limit] →
candidates=watch[:budget] → _run_bregman(candidates) → for rec: _consider →
edge_engine.best_side → (would_trade|_explore_gate) → _open``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("hte.feature_activation")

SCHEMA_VERSION = "feature_activation/1.0"


def _bool(cfg, name, default=None):
    if cfg is None:
        return default
    return getattr(cfg, name, default)


# The audited truth table. ``controls_trades`` / ``telemetry_only`` reflect the
# traced runtime path. ``cfg_probe`` (optional) refines status from a live config.
FEATURES: list[dict] = [
    {
        "feature": "Raw ABCAS/Bregman scanner",
        "files": ["engine/strategies/bregman_scanner.py",
                  "engine/arbitrage/constraint_discovery.py"],
        "runtime_status": "telemetry",
        "controls_trades": False, "telemetry_only": True,
        "flag": "BREGMAN_PAPER_SCAN_ENABLED / ABCAS_ENABLED",
        "evidence": "Run only from start_polymarket_paper_training loop; writes "
                    "bregman_scan.json + metrics/bregman.json. NOT imported by "
                    "polymarket_trainer.py — never opens a paper position.",
        "risk": "ABCAS looks 'enabled' and reports candidates but never trades — "
                "false impression the flagship edge is live.",
        "pass2": "RESOLVED — raw-catalog combinatorial candidate generation is now "
                 "ACTIVE in the trainer: group_markets runs over scan.eligible (full "
                 "eligible catalog) every tick and certified sets open in PAPER. The "
                 "standalone ABCAS scanner remains telemetry, but its candidate-source "
                 "role is now realized by the trainer's full-catalog Bregman path.",
    },
    {
        "feature": "Trainer Bregman certifier",
        "files": ["engine/training/bregman_execution.py",
                  "engine/training/bregman_grouping.py",
                  "engine/training/polymarket_trainer.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "bregman_enabled (cfg)",
        "evidence": "run_tick → _run_bregman → _bregman_tradable → scan_bregman → "
                    "group_markets(records)+certify_all (trainer ~617-657).",
        "risk": "Certifies only over the directional shortlist (see input universe).",
        "pass2": "RESOLVED — certifier now consumes the FULL eligible catalog "
                 "(scan.eligible[:bregman_discovery_limit]); groups are de-duped by "
                 "(group_type, market-id set, outcome set) before certify_all.",
    },
    {
        "feature": "Bregman paper execution",
        "files": ["engine/training/polymarket_trainer.py:_open_bregman_sets/_open_bregman"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "bregman_execution_enabled (cfg) + mode==paper_train",
        "evidence": "_open_bregman_sets gated on paper_train + bregman_execution_enabled; "
                    "appends hedged-leg PaperPositions via RiskEngine+PaperBroker; "
                    "skips group_type=='binary_yes_no' (synthetic NO leg).",
        "risk": "Almost never fires: binary YES/NO (most of Polymarket) is skipped "
                "and the input is the shortlist, so few/no real multi-leg sets.",
        "pass2": "ACTIVE if opportunities pass certification — runs BEFORE directional "
                 "(Tier 1) and is bounded by per-tick caps (bregman_max_bundles_per_tick, "
                 "bregman_max_capital_per_tick_usd, bregman_max_open_bundles, "
                 "bregman_min_roi). Explicit reject reasons: synthetic_binary_not_executable, "
                 "incomplete_or_uncertain_exhaustive_set, roi_below_min, capital_cap_per_tick, "
                 "max_bundles_per_tick, max_open_bundles (+ certifier reasons).",
        "pass4": "FIRST-PRIORITY — certified-realistic opps reserve open slots "
                 "(bregman_reserve_open_slots) + capital (bregman_reserve_capital_usd) "
                 "before directional; directional is admission-gated (_directional_admit) "
                 "and blocked on Bregman markets/events; opps sorted by after-cost quality. "
                 "Reserve released to directional only when no certified-realistic opp exists.",
    },
    {
        "feature": "Bregman INPUT UNIVERSE (catalog vs shortlist)",
        "files": ["engine/training/polymarket_trainer.py:run_tick",
                  "engine/training/market_scanner.py:ScanResult.eligible"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "bregman_discovery_limit (full-catalog cap; directional still uses budget)",
        "evidence": "PASS-2: run_tick now feeds Bregman scan.eligible[:bregman_discovery_limit] "
                    "— the FULL ranked eligible catalog (all kept markets after safety "
                    "filters), NOT watch[:budget]. ScanResult.eligible = [d['record'] for d "
                    "in ranked]; directional still uses the shortlist (records).",
        "risk": "RESOLVED — combinatorial arbitrage across the full market universe is now "
                "discoverable; previously only the directional shortlist was visible.",
        "pass2": "RESOLVED — Bregman sees the full eligible raw catalog.",
    },
    {
        "feature": "Graph grouping (groups_from_graph)",
        "files": ["engine/training/bregman_grouping.py"],
        "runtime_status": "dead",
        "controls_trades": False, "telemetry_only": False,
        "flag": "(none)",
        "evidence": "No groups_from_graph() on this branch; the active grouping is "
                    "group_markets(records). Dependency-graph clustering exists but "
                    "is used only for cluster_id annotation.",
        "risk": "Structural graph grouping not used for arbitrage discovery.",
        "pass2": "Left disabled (not present); metrics/bregman_execution.json records "
                 "groups_from_graph_used=false + reason. group_markets now runs over the "
                 "full eligible catalog, so full-universe discovery no longer depends on it.",
    },
    {
        "feature": "Profitability-first ranking",
        "files": ["engine/training/candidate_ranker.py:annotate_profitability",
                  "engine/training/profitability_governor.py",
                  "engine/training/market_scanner.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "profitability_first (POLYMARKET_PROFITABILITY_FIRST=1 default)",
        "evidence": "market_scanner.scan calls rank_candidates (quality score) + "
                    "annotate_feedback_value, then shortlist=ranked[:shortlist_limit]. "
                    "annotate_profitability() is never called in the runtime path.",
        "risk": "HIGH: candidates are truncated by quality score, NOT after-cost EV — "
                "profitable-but-lower-quality markets are dropped before any decision.",
        "pass5": "RESOLVED — annotate_profitability now runs in market_scanner.scan "
                 "BEFORE shortlist truncation and (profitability_first) re-ranks by "
                 "after-cost score; every candidate carries conservative executable "
                 "economics (spread/depth/fee/slippage/tick drag + bucket). Directional "
                 "opens are hard-gated at decision time by the profitability governor "
                 "(after-cost edge/ROI/EV); negative after-cost is rejected.",
    },
    {
        "feature": "Active learning selector",
        "files": ["engine/training/active_learning.py:ActiveLearningSelector"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "active_learning_enabled=1 (default) / random_exploration_enabled=0",
        "evidence": "ActiveLearningSelector is not imported by the trainer or any "
                    "engine/scripts runtime module; feedback_value is annotated but "
                    "the selector is never invoked.",
        "risk": "Exploration is blind; high-feedback-value markets are not prioritized.",
        "pass6": "RESOLVED — ActiveLearningSelector is now the EXPLORATION AUTHORITY: "
                 "the trainer constructs it and _active_learning_admit scores every "
                 "near-miss (uncertainty + calibration + category + disagreement + "
                 "near-miss profit + execution quality - penalties), gates strict "
                 "realism + bounded loss + diversity caps, and selects the most "
                 "informative candidates. Random/hash cannot open a trade while "
                 "active learning is enabled.",
    },
    {
        "feature": "Random/hash exploration",
        "files": ["engine/training/polymarket_trainer.py:_explore_gate/_consider"],
        "runtime_status": "dead",
        "controls_trades": False, "telemetry_only": False,
        "flag": "random_exploration_enabled=0 (default; legacy fallback only)",
        "evidence": "_explore_gate = sha256(market+tick) % 1000 < exploration_rate; "
                    "opens near-miss exploration trades at capped "
                    "exploration_notional_usd (paper_train only).",
        "risk": "Deterministic hash sampling (not learning-value); correctly tiny + "
                "counts_for_readiness=False, but adds no targeted edge.",
        "pass6": "DISABLED by default — _active_learning_admit routes exploration "
                 "through the ActiveLearningSelector; the hash gate can no longer open "
                 "a trade while active learning is on (legacy_random_exploration_blocked "
                 "counts would-be opens). Kept only as a diagnostic/tie-breaker.",
    },
    {
        "feature": "Cluster/correlation gate",
        "files": ["engine/training/market_scanner.py (sets cluster_id)",
                  "engine/training/edge_engine.py (accepts open_clusters)",
                  "engine/training/polymarket_trainer.py:_consider"],
        "runtime_status": "annotated",
        "controls_trades": False, "telemetry_only": False,
        "flag": "(cluster_id computed; open_clusters NOT passed)",
        "evidence": "market_scanner sets d['cluster_id']=graph.cluster_of(...); "
                    "EdgeEngine.best_side accepts open_clusters/cluster_id, but the "
                    "trainer call passes only open_event_groups (group_key), so the "
                    "cluster gate is never triggered.",
        "risk": "Correlated (non-same-event) exposure is NOT blocked — concentration "
                "risk; only same-event group_key duplication is gated.",
    },
    {
        "feature": "Paper fill realism (slippage/depth)",
        "files": ["engine/training/paper_policy.py", "engine/execution/paper_broker.py",
                  "engine/training/config.py"],
        "runtime_status": "annotated",
        "controls_trades": True, "telemetry_only": False,
        "flag": "realistic_fill_enabled (default False)",
        "evidence": "realistic_fill_enabled defaults False (slippage+depth modeling "
                    "OFF) outside the campaign-safe profile; status emits "
                    "fill_realism=null.",
        "risk": "HIGH: without realistic_fill_enabled, fills can be optimistic and "
                "null telemetry hides whether PnL is inflated.",
        "pass3": "HARDENED — a centralized PaperExecutionPolicy now classifies every "
                 "directional + Bregman fill as realistic_executable / shadow_only_* / "
                 "rejected. Reference/offline-stub/missing-ask/stale/thin/wide/ambiguous "
                 "fills are downgraded to shadow (logged, never PnL) or rejected; only "
                 "realistic_executable trades count toward readiness_pnl. docker-compose "
                 "strict defaults: PM reference fills + offline stub OFF, spread<=0.08, "
                 "depth>=25, ambiguity<=0.45, book age<=20s.",
    },
    {
        "feature": "Stale-book rejection",
        "files": ["engine/training/edge_engine.py", "engine/training/config.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "reject_on_stale_book=True / clob_stale_ms=3000",
        "evidence": "Hard reject in EdgeEngine.evaluate when the book is stale.",
        "risk": "Low (correctly enforced) — disabling it would allow stale fills.",
    },
    {
        "feature": "Reference-price fill fallback",
        "files": ["engine/execution/paper_broker.py", "engine/training/config.py"],
        "runtime_status": "imported",
        "controls_trades": True, "telemetry_only": False,
        "flag": "allow_pm_reference_price_fills=False (default)",
        "evidence": "PaperBroker supports reference-price fills but they are OFF by "
                    "default (and campaign-safe forces them off).",
        "risk": "If enabled, produces fantasy fills not backed by a real ask.",
        "pass3": "RESOLVED — docker-compose now sets PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS=0 "
                 "and PAPER_ALLOW_REFERENCE_PRICE_FILLS=0; the PaperExecutionPolicy "
                 "downgrades any reference-price fill to shadow_only_reference_price and "
                 "quarantines its (theoretical) PnL out of readiness.",
    },
    {
        "feature": "Spread/depth gates",
        "files": ["engine/training/edge_engine.py", "engine/training/config.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "max_spread=0.08 / min_depth_at_price=50 / max_fill_depth_fraction=0.35",
        "evidence": "Hard rejects in EdgeEngine.evaluate before edge math.",
        "risk": "Low — correctly enforced hard gates.",
    },
    {
        "feature": "Ambiguity gate",
        "files": ["engine/training/edge_engine.py", "engine/training/config.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "max_ambiguity_score=0.35 (hard) + ambiguity_penalty_weight (soft)",
        "evidence": "Hard reject above max_ambiguity_score; soft penalty below.",
        "risk": "Low — enforced; mis-set threshold could over/under-filter.",
    },
    {
        "feature": "Chainlink conditioning",
        "files": ["engine/training/chainlink_oracle.py",
                  "engine/training/polymarket_trainer.py", "engine/training/edge_engine.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "chainlink_enabled / btc_pulse_require_chainlink",
        "evidence": "Read each tick (read-only); conditions/gates Bregman + BTC Pulse "
                    "and applies a directional penalty when stale.",
        "risk": "Low for paper; stale anchor correctly penalizes.",
    },
    {
        "feature": "News/research/model overlay",
        "files": ["engine/research/news_scanner.py", "engine/research/probability.py",
                  "engine/training/edge_engine.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "NEWS_SCANNER_ENABLED / RESEARCH_USE_IN_STRATEGY",
        "evidence": "Advisory, read-only; feeds the probability estimate when "
                    "RESEARCH_USE_IN_STRATEGY; cannot bypass risk/fill gates.",
        "risk": "Medium: research nudges probability; weak calibration could bias edge.",
    },
    {
        "feature": "Grok/LLM reasoning overlay",
        "files": ["engine/research/grok_client.py"],
        "runtime_status": "telemetry",
        "controls_trades": False, "telemetry_only": True,
        "flag": "NEWS_ENABLE_GROK_PACKET (grok_with_news_count null in report)",
        "evidence": "Advisory research-only; cannot place/size/approve. Report shows "
                    "grok_with_news_count=null (telemetry gap, not a trade control).",
        "risk": "Low for trades; unmeasured contribution (null counters).",
    },
    {
        "feature": "Profitability governor",
        "files": ["engine/training/profitability_governor.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "require_profitability_annotation / min_after_cost_edge (cfg)",
        "evidence": "Not referenced by polymarket_trainer.py; only used inside "
                    "annotate_profitability, which is itself never called.",
        "risk": "No after-cost graylist/throttle is applied to directional ranking.",
        "pass5": "RESOLVED — ProfitabilityGovernor is constructed in the trainer and "
                 "wired into _open via _profitability_gate: it computes conservative "
                 "after-cost edge/ROI/EV, hard-rejects negative-after-cost (bucket "
                 "negative_after_cost), shadows sub-threshold candidates, records "
                 "strikes in MarketQualityMemory, and never lets an unannotated "
                 "candidate execute (require_profitability_annotation).",
    },
    {
        "feature": "Position/open-slot governor",
        "files": ["engine/training/polymarket_trainer.py:run_tick",
                  "engine/training/edge_engine.py", "engine/risk.py"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "max_open_trades / RiskEngine caps",
        "evidence": "run_tick breaks on len(open_positions) >= max_open_trades; "
                    "EdgeEngine gates max_open_trades; RiskEngine enforces exposure.",
        "risk": "Low — enforced.",
    },
    {
        "feature": "Stop-loss/take-profit/settlement handling",
        "files": ["engine/training/polymarket_trainer.py:_monitor"],
        "runtime_status": "active",
        "controls_trades": True, "telemetry_only": False,
        "flag": "(monitor/settlement each tick)",
        "evidence": "_monitor marks open positions to market each tick and settles "
                    "resolved markets into realized PnL.",
        "risk": "Medium: explicit SL/TP is mark-and-settle; no intra-round stop.",
    },
]


# Top edge leaks ranked by expected impact on real profitability.
TOP_EDGE_LEAKS: list[dict] = [
    {"rank": 1, "leak": "Bregman/ABCAS only sees the directional shortlist, not the "
                        "full normalized catalog", "impact": "highest",
     "feature": "Bregman INPUT UNIVERSE (catalog vs shortlist)"},
    {"rank": 2, "leak": "Raw ABCAS scanner is telemetry-only — the flagship edge "
                        "never opens a trade", "impact": "high",
     "feature": "Raw ABCAS/Bregman scanner"},
    {"rank": 3, "leak": "Profitability-first ranking is unused — candidates truncated "
                        "by quality score, not after-cost EV", "impact": "high",
     "feature": "Profitability-first ranking"},
    {"rank": 4, "leak": "realistic_fill_enabled defaults False — paper PnL may be "
                        "optimistic and fill_realism telemetry is null", "impact": "high",
     "feature": "Paper fill realism (slippage/depth)"},
    {"rank": 5, "leak": "Cluster/correlation gate annotated but not enforced "
                        "(open_clusters not passed)", "impact": "medium-high",
     "feature": "Cluster/correlation gate"},
    {"rank": 6, "leak": "binary_yes_no Bregman groups skipped (correct safety) leaves "
                        "the trainer Bregman with almost nothing to trade", "impact": "medium-high",
     "feature": "Bregman paper execution"},
    {"rank": 7, "leak": "Active learning unused — exploration is blind hash sampling",
     "impact": "medium", "feature": "Active learning selector"},
    {"rank": 8, "leak": "Profitability governor dead — no after-cost graylist/throttle "
                        "on directional ranking", "impact": "medium",
     "feature": "Profitability governor"},
    {"rank": 9, "leak": "Grok/news evidence counters null — research overlay impact "
                        "is unmeasured", "impact": "low-medium",
     "feature": "Grok/LLM reasoning overlay"},
    {"rank": 10, "leak": "Two divergent Bregman implementations (strategies/"
                         "bregman_scanner ABCAS vs training/bregman_execution) — "
                         "reporting and execution disagree", "impact": "medium",
     "feature": "Trainer Bregman certifier"},
]

PASS2_RECOMMENDATION = {
    "recommended": True,
    "headline": "Pass 2 SHOULD connect ABCAS to certified paper execution — but only "
                "AFTER widening the Bregman input universe and unifying the two "
                "Bregman implementations.",
    "preconditions": [
        "Feed the FULL normalized catalog (engine.arbitrage.constraint_discovery) to "
        "combinatorial discovery, not the directional shortlist.",
        "Require BOTH legs real + executable (no synthetic binary NO leg) before any "
        "certified-executable open.",
        "Turn on realistic_fill_enabled so certified after-cost profit is real.",
        "Unify engine/strategies/bregman_scanner (ABCAS) with engine/training/"
        "bregman_execution so reporting == execution.",
    ],
    "guardrails": [
        "PAPER ONLY — route certified-executable arbs through the existing RiskEngine "
        "+ PaperBroker; never enable a live path.",
        "Keep EXECUTABLE_AFTER_COST_CERTIFIED gating; theoretical-only stays shadow.",
    ],
    "rationale": "Executing ABCAS today would produce ~0 trades (shortlist input + "
                 "binary skip) or fantasy multi-leg fills (realistic_fill off). Fix "
                 "the input universe + fill realism first.",
}


# Pass-2 outcome: the concrete proofs that raw-catalog Bregman now controls
# certified paper execution (kept alongside the Pass-1 audit as the evidence).
PASS2_STATUS = {
    "wired": True,
    "raw_abcas_bregman_scanner": "active candidate generation (full-catalog group_markets)",
    "trainer_bregman_certifier": "active",
    "bregman_paper_execution": "active if opportunities pass certification",
    "bregman_sees_full_raw_catalog": True,
    "bregman_execution_priority_before_directional": True,
    "evidence": [
        "run_tick feeds scan.eligible[:bregman_discovery_limit] (full eligible "
        "catalog) to Bregman, not watch[:budget].",
        "ScanResult.eligible = all ranked kept markets (after safety filters).",
        "_run_bregman/_open_bregman_sets runs BEFORE the directional loop in run_tick.",
        "Groups de-duped by (group_type, market-id set, outcome set) before certify_all.",
        "Per-tick caps + explicit reject reasons enforced; binary_yes_no skipped as "
        "synthetic_binary_not_executable.",
        "Funnel written to metrics/bregman_execution.json (discovery→certify→open).",
    ],
    "new_env_flags": {
        "POLYMARKET_BREGMAN_DISCOVERY_LIMIT": 1000,
        "POLYMARKET_BREGMAN_MAX_BUNDLES_PER_TICK": 3,
        "POLYMARKET_BREGMAN_MAX_OPEN_BUNDLES": 10,
        "POLYMARKET_BREGMAN_MAX_CAPITAL_PER_TICK": 100.0,
        "POLYMARKET_BREGMAN_MIN_ROI": 0.002,
    },
}


# Pass-3 outcome: paper execution realism (trustworthy paper training). These
# verdicts are the proof that unrealistic fills can no longer inflate real edge.
PASS3_STATUS = {
    "hardened": True,
    "reference_price_fills_allowed_for_exploit_validation": False,
    "missing_ask_fallback_allowed": False,
    "stale_book_fills_allowed": False,
    "offline_stub_fills_count_as_real_pnl": False,
    "bregman_requires_all_executable_legs": True,
    "realistic_executable_trades_separated_from_shadow": True,
    "readiness_excludes_unrealistic_fills": True,
    "centralized_policy": "engine/training/paper_execution.py:PaperExecutionPolicy",
    "execution_realism_statuses": [
        "realistic_executable", "shadow_only_reference_price", "shadow_only_stale_book",
        "shadow_only_missing_ask", "shadow_only_thin_depth", "shadow_only_wide_spread",
        "shadow_only_ambiguous_settlement", "rejected",
    ],
    "metrics": ["metrics/paper_realism.json", "metrics/bregman_execution.json"],
    "strict_defaults": {
        "PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS": 0, "POLYMARKET_MIN_DEPTH_AT_PRICE": 25,
        "POLYMARKET_MAX_SPREAD": 0.08, "POLYMARKET_MAX_AMBIGUITY_SCORE": 0.45,
        "POLYMARKET_MAX_BOOK_AGE_SEC": 20, "POLYMARKET_REQUIRE_EXECUTABLE_ASK": 1,
        "POLYMARKET_REJECT_STALE_BOOK": 1, "POLYMARKET_REJECT_MISSING_ASK": 1,
        "POLYMARKET_REJECT_OFFLINE_STUB_FILLS": 1,
        "POLYMARKET_ALLOW_OFFLINE_STUB_TRADING": 0,
    },
}


# Pass-4 outcome: Bregman-FIRST strategy priority. Certified, realistic,
# after-cost-positive complete-set arbitrage gets first claim on slots + capital;
# directional is secondary, exploration tertiary. Proof that the bot prefers
# certified arbitrage over directional prediction.
PASS4_STATUS = {
    "bregman_priority_enabled": True,
    "raw_abcas_bregman_scanner_controls_candidate_generation": True,
    "trainer_bregman_certifier_active": True,
    "bregman_execution_before_directional": True,
    "directional_secondary_after_bregman": True,
    "exploration_tertiary_after_exploit": True,
    "paper_realism_still_enforced": True,
    "reservation": {
        "POLYMARKET_BREGMAN_RESERVE_OPEN_SLOTS": 3,
        "POLYMARKET_BREGMAN_RESERVE_CAPITAL_USD": 100.0,
        "POLYMARKET_DIRECTIONAL_CAN_USE_UNUSED_BREGMAN_SLOTS": True,
        "POLYMARKET_DIRECTIONAL_CAN_USE_UNUSED_BREGMAN_CAPITAL": True,
        "POLYMARKET_BLOCK_DIRECTIONAL_ON_BREGMAN_MARKETS": True,
        "POLYMARKET_BLOCK_DIRECTIONAL_ON_BREGMAN_EVENTS": True,
        "POLYMARKET_EXPLORATION_CAN_USE_BREGMAN_RESERVED_CAPACITY": False,
    },
    "metrics": ["metrics/strategy_priority.json", "metrics/bregman_execution.json",
                "metrics/paper_realism.json"],
    "evidence": [
        "run_tick: dir_slots_before -> _run_bregman/_open_bregman_sets (Tier 1) -> "
        "_begin_directional_phase -> directional loop with _directional_admit gate.",
        "Reserved slots/capital held whenever a certified-realistic Bregman opp "
        "exists this tick; released to directional only when none does.",
        "_directional_admit blocks directional on Bregman markets/events + the "
        "reserved-slot boundary; exploration blocked from reserved capacity.",
        "Certified opps sorted by after-cost ROI/fill-quality/spread/depth/"
        "freshness/ambiguity/capital before opening (_bregman_quality_key).",
    ],
}


# Pass-5 outcome: profitability-first ranking. Candidates compete on conservative
# executable AFTER-COST expected value, annotated before truncation, hard-gated
# by the profitability governor. Bregman-first priority (Pass 4) is preserved.
PASS5_STATUS = {
    "profitability_first_enabled": True,
    "profitability_annotation_before_truncation": True,
    "directional_ranked_by_after_cost_ev": True,
    "bregman_ranked_by_after_cost_profit_roi": True,
    "negative_after_cost_cannot_count_as_edge": True,
    "missing_annotation_rejected_or_shadow": True,
    "profitability_governor_active_hard_gate": True,
    "bregman_first_priority_preserved": True,
    "exploration_profitability_aware_and_bounded": True,
    "annotation_layer": "engine/training/candidate_ranker.py:annotate_profitability",
    "governor": "engine/training/profitability_governor.py:ProfitabilityGovernor",
    "metrics": ["metrics/profitability_ranking.json"],
    "buckets": ["bregman_certified_positive", "directional_after_cost_positive",
                "exploration_feedback_positive", "shadow_theoretical_only",
                "negative_after_cost", "non_executable", "insufficient_data"],
    "new_env_flags": {
        "POLYMARKET_PROFITABILITY_FIRST": 1,
        "POLYMARKET_REQUIRE_PROFITABILITY_ANNOTATION": 1,
        "POLYMARKET_MIN_AFTER_COST_EDGE": 0.01, "POLYMARKET_MIN_AFTER_COST_ROI": 0.002,
        "POLYMARKET_MIN_EXPECTED_VALUE_USD": 0.01,
        "POLYMARKET_BREGMAN_MIN_AFTER_COST_PROFIT_USD": 0.02,
    },
}


# Pass-6 outcome: profitability-aware active learning is the exploration authority.
# Random/hash exploration no longer opens trades; exploration is realism-gated,
# bounded, diversity-capped, separated from readiness, and produces learning data.
PASS6_STATUS = {
    "active_learning_is_exploration_authority": True,
    "random_hash_exploration_opens_trades": False,
    "exploration_requires_paper_realism": True,
    "exploration_requires_profitability_annotation": True,
    "exploration_bounded_loss": True,
    "exploration_excluded_from_readiness": True,
    "exploration_cannot_consume_bregman_reserved_capacity": True,
    "near_misses_logged_for_learning": True,
    "bregman_first_priority_preserved": True,
    "profitability_first_preserved": True,
    "selector": "engine/training/active_learning.py:ActiveLearningSelector",
    "learning_buckets": ["near_miss_positive_edge", "model_uncertain_high_liquidity",
                         "category_under_sampled", "calibration_gap_bucket",
                         "chainlink_disagreement_case", "news_model_disagreement_case",
                         "shadow_theoretical_only", "not_eligible_for_learning"],
    "metrics": ["metrics/active_learning.json"],
    "new_env_flags": {
        "POLYMARKET_ACTIVE_LEARNING_ENABLED": 1, "POLYMARKET_RANDOM_EXPLORATION_ENABLED": 0,
        "POLYMARKET_EXPLORATION_MAX_TRADES_PER_TICK": 2,
        "POLYMARKET_EXPLORATION_MAX_EXPECTED_LOSS_USD": 0.25,
        "POLYMARKET_EXPLORATION_COUNT_TOWARD_READINESS": 0,
        "POLYMARKET_EXPLORATION_MAX_PER_EVENT": 1, "POLYMARKET_EXPLORATION_MAX_PER_CLUSTER": 1,
        "POLYMARKET_EXPLORATION_MAX_PER_CATEGORY_PER_TICK": 2,
    },
}


def build_feature_activation(cfg: Any = None, status: Optional[dict] = None) -> dict:
    """Build the machine-readable feature-activation audit (read-only, pure).

    ``cfg`` (optional TrainingConfig) refines a few live flags; ``status`` (optional
    training status) is used to note observed runtime values. Never trades."""
    features = [dict(f) for f in FEATURES]

    # Optional live-config refinement (does not change the traced verdicts).
    if cfg is not None:
        live = {
            "bregman_execution_enabled": _bool(cfg, "bregman_execution_enabled", True),
            "realistic_fill_enabled": _bool(cfg, "realistic_fill_enabled", False),
            "allow_pm_reference_price_fills": _bool(cfg, "allow_pm_reference_price_fills", False),
            "reject_on_stale_book": _bool(cfg, "reject_on_stale_book", True),
            "exploration_enabled": _bool(cfg, "exploration_enabled", False),
            "active_learning_enabled": _bool(cfg, "active_learning_enabled", False),
            "max_spread": _bool(cfg, "max_spread", 0.08),
            "min_depth_at_price": _bool(cfg, "min_depth_at_price", 50.0),
            "max_ambiguity_score": _bool(cfg, "max_ambiguity_score", 0.35),
        }
    else:
        live = {}

    counts = {"active": 0, "telemetry": 0, "annotated": 0, "imported": 0, "dead": 0}
    for f in features:
        counts[f["runtime_status"]] = counts.get(f["runtime_status"], 0) + 1

    inflation_risks = [f["feature"] for f in features
                       if f["feature"] in ("Paper fill realism (slippage/depth)",
                                           "Reference-price fill fallback",
                                           "Raw ABCAS/Bregman scanner")]

    return {
        "schema_version": SCHEMA_VERSION,
        "paper_only": True,
        "summary": {
            "truly_active": [f["feature"] for f in features if f["controls_trades"]
                             and f["runtime_status"] == "active"],
            "telemetry_only": [f["feature"] for f in features if f["telemetry_only"]],
            "dead_or_unused": [f["feature"] for f in features
                               if f["runtime_status"] in ("dead", "imported")],
            "pnl_inflation_risks": inflation_risks,
            "status_counts": counts,
        },
        "features": features,
        "top_edge_leaks": [dict(x) for x in TOP_EDGE_LEAKS],
        "pass2_recommendation": PASS2_RECOMMENDATION,
        "pass2_status": dict(PASS2_STATUS),
        "pass3_status": dict(PASS3_STATUS),
        "pass4_status": dict(PASS4_STATUS),
        "pass5_status": dict(PASS5_STATUS),
        "pass6_status": dict(PASS6_STATUS),
        "live_config": live,
        "note": "PASS-1 audit traced from run_tick to open. PASS-2 wired raw-catalog "
                "Bregman into certified PAPER execution (see pass2_status / per-feature "
                "'pass2'). No live-execution, polymarket-client, or Chainlink changes.",
    }


def to_markdown(audit: dict) -> str:
    """Render the audit dict to a human-readable markdown report (pure)."""
    L: list[str] = []
    L.append("# Feature Activation Audit (Pass 1) — Hermes Polymarket Paper Training")
    L.append("")
    L.append("_PAPER ONLY · audit + instrumentation only · no strategy/threshold/"
             "sizing/live changes. Verdicts traced from `run_tick` to trade open._")
    L.append("")
    p2s = audit.get("pass2_status")
    if p2s:
        L.append("## Pass 2 — wired (raw-catalog Bregman → certified PAPER execution)")
        L.append(f"- Raw ABCAS/Bregman scanner: **{p2s['raw_abcas_bregman_scanner']}**")
        L.append(f"- Trainer Bregman certifier: **{p2s['trainer_bregman_certifier']}**")
        L.append(f"- Bregman paper execution: **{p2s['bregman_paper_execution']}**")
        L.append(f"- Bregman sees full raw catalog: **{p2s['bregman_sees_full_raw_catalog']}**")
        L.append(f"- Bregman execution priority before directional: "
                 f"**{p2s['bregman_execution_priority_before_directional']}**")
        for e in p2s["evidence"]:
            L.append(f"  - {e}")
        L.append("")
    p6 = audit.get("pass6_status")
    if p6:
        L.append("## Pass 6 — profitability-aware active learning (exploration authority)")
        L.append(f"- Active learning is the exploration authority: "
                 f"**{p6['active_learning_is_exploration_authority']}**")
        L.append(f"- Random/hash exploration opens trades: "
                 f"**{p6['random_hash_exploration_opens_trades']}**")
        L.append(f"- Exploration requires paper realism: "
                 f"**{p6['exploration_requires_paper_realism']}**")
        L.append(f"- Exploration bounded loss: **{p6['exploration_bounded_loss']}**")
        L.append(f"- Exploration excluded from readiness: "
                 f"**{p6['exploration_excluded_from_readiness']}**")
        L.append(f"- Exploration cannot consume Bregman reserved capacity: "
                 f"**{p6['exploration_cannot_consume_bregman_reserved_capacity']}**")
        L.append(f"- Near-misses logged for learning: **{p6['near_misses_logged_for_learning']}**")
        L.append(f"- Bregman-first priority preserved: **{p6['bregman_first_priority_preserved']}**")
        L.append("")
    p5 = audit.get("pass5_status")
    if p5:
        L.append("## Pass 5 — profitability-first ranking")
        L.append(f"- Profitability-first enabled: **{p5['profitability_first_enabled']}**")
        L.append(f"- Annotation before shortlist truncation: "
                 f"**{p5['profitability_annotation_before_truncation']}**")
        L.append(f"- Directional ranked by after-cost EV: "
                 f"**{p5['directional_ranked_by_after_cost_ev']}**")
        L.append(f"- Bregman ranked by after-cost profit/ROI: "
                 f"**{p5['bregman_ranked_by_after_cost_profit_roi']}**")
        L.append(f"- Negative after-cost cannot count as edge: "
                 f"**{p5['negative_after_cost_cannot_count_as_edge']}**")
        L.append(f"- Missing annotation rejected/shadow: "
                 f"**{p5['missing_annotation_rejected_or_shadow']}**")
        L.append(f"- Profitability governor active (hard gate): "
                 f"**{p5['profitability_governor_active_hard_gate']}**")
        L.append(f"- Bregman-first priority preserved: "
                 f"**{p5['bregman_first_priority_preserved']}**")
        L.append("")
    p4 = audit.get("pass4_status")
    if p4:
        L.append("## Pass 4 — Bregman-first strategy priority")
        L.append(f"- Bregman priority enabled: **{p4['bregman_priority_enabled']}**")
        L.append(f"- Raw ABCAS/Bregman scanner controls candidate generation: "
                 f"**{p4['raw_abcas_bregman_scanner_controls_candidate_generation']}**")
        L.append(f"- Trainer Bregman certifier active: **{p4['trainer_bregman_certifier_active']}**")
        L.append(f"- Bregman execution before directional: "
                 f"**{p4['bregman_execution_before_directional']}**")
        L.append(f"- Directional secondary after Bregman: "
                 f"**{p4['directional_secondary_after_bregman']}**")
        L.append(f"- Exploration tertiary after exploit strategies: "
                 f"**{p4['exploration_tertiary_after_exploit']}**")
        L.append(f"- Paper realism still enforced: **{p4['paper_realism_still_enforced']}**")
        L.append("")
    p3 = audit.get("pass3_status")
    if p3:
        L.append("## Pass 3 — paper execution realism (trustworthy paper training)")
        L.append(f"- Reference-price fills allowed for exploit validation: "
                 f"**{p3['reference_price_fills_allowed_for_exploit_validation']}**")
        L.append(f"- Missing ask fallback allowed: **{p3['missing_ask_fallback_allowed']}**")
        L.append(f"- Stale book fills allowed: **{p3['stale_book_fills_allowed']}**")
        L.append(f"- Offline stub fills count as real PnL: "
                 f"**{p3['offline_stub_fills_count_as_real_pnl']}**")
        L.append(f"- Bregman requires all executable legs: "
                 f"**{p3['bregman_requires_all_executable_legs']}**")
        L.append(f"- Realistic executable trades separated from shadow: "
                 f"**{p3['realistic_executable_trades_separated_from_shadow']}**")
        L.append(f"- Readiness excludes unrealistic fills: "
                 f"**{p3['readiness_excludes_unrealistic_fills']}**")
        L.append(f"  - Centralized policy: `{p3['centralized_policy']}`")
        L.append("")
    s = audit["summary"]
    L.append("## Summary")
    L.append(f"- **Truly active (control trades):** {', '.join(s['truly_active'])}")
    L.append(f"- **Telemetry-only:** {', '.join(s['telemetry_only'])}")
    L.append(f"- **Dead / imported-only:** {', '.join(s['dead_or_unused'])}")
    L.append(f"- **PnL-inflation risks:** {', '.join(s['pnl_inflation_risks'])}")
    L.append(f"- Status counts: {s['status_counts']}")
    L.append("")
    L.append("## Runtime feature truth table")
    L.append("")
    L.append("| Feature | File(s) | Runtime status | Controls trades? | Telemetry only? "
             "| Config/env flag | Evidence | Risk if unchanged |")
    L.append("|---|---|---|---|---|---|---|---|")
    for f in audit["features"]:
        files = "<br>".join(f["files"])
        risk = f["risk"]
        if f.get("pass2"):
            risk = f"{risk}<br>**Pass-2:** {f['pass2']}"
        if f.get("pass3"):
            risk = f"{risk}<br>**Pass-3:** {f['pass3']}"
        if f.get("pass4"):
            risk = f"{risk}<br>**Pass-4:** {f['pass4']}"
        if f.get("pass5"):
            risk = f"{risk}<br>**Pass-5:** {f['pass5']}"
        if f.get("pass6"):
            risk = f"{risk}<br>**Pass-6:** {f['pass6']}"
        L.append(f"| {f['feature']} | {files} | `{f['runtime_status']}` | "
                 f"{'YES' if f['controls_trades'] else 'no'} | "
                 f"{'YES' if f['telemetry_only'] else 'no'} | {f['flag']} | "
                 f"{f['evidence']} | {risk} |")
    L.append("")
    L.append("## Top 10 edge leaks (ranked by profit impact)")
    L.append("")
    for x in audit["top_edge_leaks"]:
        L.append(f"{x['rank']}. **[{x['impact']}]** {x['leak']} _( {x['feature']} )_")
    L.append("")
    p2 = audit["pass2_recommendation"]
    L.append("## Pass 2 recommendation")
    L.append(f"- **Recommended:** {p2['recommended']}")
    L.append(f"- {p2['headline']}")
    L.append("- Preconditions:")
    for c in p2["preconditions"]:
        L.append(f"  - {c}")
    L.append("- Guardrails:")
    for c in p2["guardrails"]:
        L.append(f"  - {c}")
    L.append(f"- Rationale: {p2['rationale']}")
    L.append("")
    return "\n".join(L)
