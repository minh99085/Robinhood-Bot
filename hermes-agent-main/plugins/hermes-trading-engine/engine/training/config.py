"""Polymarket Training Engine v2 — configuration.

Single source of truth for the trainer config. PAPER ONLY. The default mode is
``observe_only`` (evaluate + record diagnostics, NEVER place paper trades);
``paper_train`` enables simulated paper trades (still no real orders); ``disabled``
turns the loop off entirely. Live-execution flags are tracked only so we can
FAIL CLOSED if any are set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from engine.markets import universe_manager as um

# Live-execution flags that must NEVER be on for a PAPER training run.
FORBIDDEN_LIVE_FLAGS = (
    "MICRO_LIVE_ENABLED", "KALSHI_MICRO_LIVE_ENABLED", "POLYMARKET_MICRO_LIVE_ENABLED",
    "MICRO_LIVE_ALLOW_PRODUCTION", "GUARDED_LIVE_ENABLED",
    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
    "PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE", "PRODUCTION_REVIEW_ALLOW_DASHBOARD_SUBMIT",
    "PRODUCTION_REVIEW_ALLOW_API_SUBMIT",
)

MODES = ("disabled", "observe_only", "paper_train")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _envb(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _env_csv_floats(name: str, default: str) -> list:
    raw = os.getenv(name, default)
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out or [float(x) for x in default.split(",")]


@dataclass
class TrainingConfig:
    # ---- mode / universe gating ----
    mode: str = "observe_only"
    polymarket_only: bool = True
    disable_btc_pulse_trading: bool = True
    # ---- scanner ----
    scan_limit: int = 1000
    scan_interval_seconds: float = 60.0
    metadata_cache_ttl_s: float = 60.0
    incremental_refresh: bool = True
    async_scan: bool = True
    max_concurrent_requests: int = 8
    scan_timeout_s: float = 20.0
    rate_limit_sleep_ms: float = 100.0
    min_volume: float = 1000.0
    min_liquidity: float = 250.0
    min_time_to_close_s: float = 3600.0
    max_time_to_close_days: float = 90.0
    # ---- ranker ----
    shortlist_limit: int = 150
    live_watch_limit: int = 100
    trade_candidate_limit: int = 30
    # ---- subscription ----
    clob_enabled: bool = True
    subscribe_trending: bool = True
    clob_stale_ms: float = 3000.0
    subscription_refresh_s: float = 120.0
    max_subscription_churn: int = 20
    # ---- probability ----
    require_research_or_model_edge: bool = True
    allow_offline_stub_trading: bool = False
    research_max_age_s: float = 900.0
    min_evidence_score: float = 0.50
    min_source_count: int = 2
    max_ambiguity_score: float = 0.35
    base_shrink_factor: float = 0.25
    max_shrink_factor: float = 0.60
    min_shrink_factor: float = 0.05
    # ---- edge engine ----
    min_net_edge: float = 0.03
    base_uncertainty: float = 0.01
    spread_penalty_weight: float = 0.50
    slippage_penalty_weight: float = 1.00
    ambiguity_penalty_weight: float = 0.03
    stale_penalty_weight: float = 0.02
    evidence_penalty_weight: float = 0.02
    calibration_penalty_weight: float = 0.05
    liquidity_penalty_weight: float = 0.03
    ambiguity_uncertainty_weight: float = 0.05
    spread_uncertainty_weight: float = 0.50
    stale_uncertainty_weight: float = 0.03
    evidence_uncertainty_weight: float = 0.04
    calibration_uncertainty_weight: float = 0.50
    max_spread: float = 0.08
    min_depth_at_price: float = 50.0
    taker_fee_bps: float = 0.0
    slippage_bps: float = 25.0
    max_fill_depth_fraction: float = 0.35
    # ---- paper policy / sizing ----
    fixed_notional_usd: float = 5.0
    max_open_trades: int = 5
    max_open_trades_hard_cap: int = 8
    max_market_exposure_usd: float = 20.0
    max_total_exposure_usd: float = 100.0
    max_daily_loss_usd: float = 50.0
    max_open_orders: int = 20
    reject_on_stale_book: bool = True
    allow_pm_reference_price_fills: bool = False
    # ---- Pass-3 strict paper execution realism (PAPER ONLY; safe defaults) ----
    # A paper trade only counts as REAL executable edge if it could plausibly fill
    # from the LIVE book. These hard gates reject (or downgrade to shadow) trades
    # that depend on reference-price/offline-stub/stale/missing-ask fills.
    max_book_age_sec: float = 20.0           # max age of the quote feeding a fill
    require_executable_ask: bool = True      # a real best-ask is mandatory
    reject_missing_ask: bool = True
    reject_offline_stub_fills: bool = True
    # Bregman/ABCAS bundle realism (every leg must be live-executable).
    bregman_require_executable_all_legs: bool = True
    bregman_allow_reference_fills: bool = False
    bregman_max_book_age_sec: float = 20.0
    use_kelly_for_diagnostics: bool = True
    use_kelly_for_size: bool = False
    kelly_fraction: float = 0.10
    max_kelly_size_usd: float = 5.0
    # ---- learner / feedback ----
    learner_enabled: bool = True
    ewma_alpha: float = 0.05
    min_bucket_samples: int = 20
    feedback_interval_seconds: float = 300.0
    markout_horizons_s: tuple = (5.0, 30.0, 60.0, 300.0, 900.0, 3600.0)
    feedback_enabled: bool = True
    # ---- exploration (aggressive paper training; bounded, PAPER ONLY) ----
    exploration_enabled: bool = False
    exploration_rate: float = 0.0          # fraction of near-miss candidates explored
    exploration_notional_usd: float = 2.0  # small exploratory size (clamped to caps)
    exploration_min_edge: float = -0.01    # min net_edge eligible for exploration
    # ---- active learning (aggressive paper mode; PAPER ONLY) ----
    # Fill idle paper budget with the highest-feedback-value near-misses. NEVER
    # bypasses a hard gate; bounded by exploration_budget_usd + the caps below.
    active_learning_enabled: bool = True
    exploration_split: float = 0.5         # max fraction of idle slots reserved for exploration
    category_sample_target: int = 50       # per-category feedback-sample target
    max_explore_per_category: int = 3      # diversity: max exploratory trades / category / tick
    max_explore_per_event: int = 1         # diversity: max exploratory trades / event / tick
    # ---- Pass-6: profitability-aware active learning is the EXPLORATION AUTHORITY ----
    # Random/hash exploration is a disabled legacy fallback; ActiveLearningSelector
    # chooses the most informative near-misses under strict realism + bounded loss.
    random_exploration_enabled: bool = False
    exploration_max_trades_per_tick: int = 2
    exploration_max_open_trades: int = 10
    exploration_max_capital_per_tick_usd: float = 20.0
    exploration_max_position_size_usd: float = 5.0
    exploration_max_expected_loss_usd: float = 0.25
    exploration_min_depth_at_price: float = 25.0
    exploration_max_spread: float = 0.08
    exploration_max_book_age_sec: float = 20.0
    exploration_max_ambiguity_score: float = 0.45
    exploration_require_profitability_annotation: bool = True
    exploration_require_realistic_fill: bool = True
    exploration_count_toward_readiness: bool = False
    exploration_max_per_event: int = 1
    exploration_max_per_cluster: int = 1
    exploration_max_per_category_per_tick: int = 2
    # ---- Closed-loop learning: shadow/no-trade learning quotas (PAPER ONLY) ----
    # A rejected candidate is still a learning example. Active learning may select
    # shadow/no-trade examples even when nothing is executable (tiny trades still
    # require realistic fills). Exploration stays excluded from readiness.
    active_learning_shadow_samples_per_tick: int = 50
    active_learning_tiny_trades_per_tick: int = 2
    active_learning_near_miss_samples_per_tick: int = 50
    active_learning_no_trade_labels_per_tick: int = 100
    active_learning_diagnostic_samples_per_tick: int = 50
    # ---- Grok advisory proof call (PAPER ONLY, research-only) ----
    # When Grok is enabled + a key + news packets exist but no real call has been
    # made recently, schedule at most one ADVISORY-ONLY proof call per hour so the
    # report can show grok_calls_total>0 instead of an ambiguous zero-call reason.
    # NEVER executes/sizes a trade and never bypasses a quant gate.
    grok_proof_call_enabled: bool = True
    grok_proof_call_max_per_hour: int = 1
    grok_proof_call_max_per_run: int = 1
    grok_proof_call_min_interval_seconds: int = 900
    grok_proof_call_advisory_only: bool = True
    # ---- bounded Grok ADVISORY scheduler (research only; never execution) ----
    # When enabled, replaces single-proof-call behaviour with a bounded scheduler
    # that makes multiple low-frequency advisory calls per run on high-value targets
    # (top Bregman near-misses / news-linked / high-liquidity markets).
    grok_advisory_enabled: bool = True
    grok_advisory_max_calls_per_hour: int = 4
    grok_advisory_min_interval_seconds: int = 900
    grok_advisory_require_news: bool = True
    grok_advisory_max_calls_per_run: int = 48
    active_learning_require_realistic_fill_for_trade: bool = True
    active_learning_allow_shadow_without_fill: bool = True
    # ---- Pass-7: cluster/correlation risk is an ACTIVE hard gate + allocator ----
    # Correlated markets are not independent edges. Duplicate market/condition/
    # event/cluster exposure is blocked or size-capped; unknown clusters become
    # shadow-only; directional/exploration cannot collide with open Bregman bundles.
    correlation_gate_enabled: bool = True
    require_cluster_metadata: bool = True
    unknown_cluster_policy: str = "shadow"        # "shadow" | "reject"
    max_open_per_market: int = 1
    max_open_per_event: int = 1
    max_open_per_cluster: int = 1
    max_cluster_exposure_usd: float = 25.0
    max_correlation_group_exposure_usd: float = 50.0
    block_duplicate_market: bool = True
    block_duplicate_event: bool = True
    block_duplicate_cluster: bool = True
    block_exploration_on_bregman_markets: bool = True
    block_exploration_on_bregman_events: bool = True
    correlation_allow_size_cap: bool = True
    bregman_block_duplicate_bundles: bool = True
    bregman_block_overlapping_bundles: bool = True
    bregman_max_open_per_event: int = 1
    bregman_max_cluster_exposure_usd: float = 100.0
    # ---- Chainlink oracle layer (additive; default OFF) ----
    chainlink_enabled: bool = False
    chainlink_history_limit: int = 30
    # ---- Bregman arbitrage (flagship Polymarket strategy; PAPER ONLY) ----
    # Bregman opportunities are scanned every tick and outrank directional trades
    # only when certified with a positive profit lower bound after all costs.
    bregman_enabled: bool = True
    bregman_execution_enabled: bool = True
    # Pass-9 ablation: directional execution can be disabled for an experiment
    # profile (directional candidates are logged shadow-only, never opened). PAPER.
    directional_execution_enabled: bool = True
    bregman_min_profit_usd: float = 0.001
    bregman_target_capital_usd: float = 50.0
    # ---- Pass-2: raw-catalog Bregman discovery + per-tick budget caps ----
    # Bregman groups are discovered over the FULL eligible catalog (capped to bound
    # cost), and execution is bounded per tick. Safe defaults; never loosened here.
    bregman_discovery_limit: int = 1000          # max eligible raw markets grouped
    bregman_max_bundles_per_tick: int = 3
    bregman_max_open_bundles: int = 10
    # ---- near-miss diagnostics (read-only; never executes / never loosens gates) ----
    bregman_near_miss_store_cap: int = 1000      # max rejected groups tracked
    bregman_top_near_misses: int = 10            # top-N near-misses surfaced in report
    bregman_max_capital_per_tick_usd: float = 100.0
    bregman_min_roi: float = 0.002               # min after-cost ROI per certified set
    # ---- Pass-4: Bregman-FIRST strategy priority + slot/capital reservation ----
    # Certified, realistic, after-cost-positive Bregman complete-set arbitrage gets
    # first claim on open slots + capital each tick. Directional is secondary;
    # exploration tertiary. Reserved capacity is released to directional ONLY when
    # no certified-realistic Bregman opportunity exists this tick.
    bregman_priority_enabled: bool = True
    bregman_reserve_open_slots: int = 3
    bregman_reserve_capital_usd: float = 100.0
    directional_can_use_unused_bregman_slots: bool = True
    directional_can_use_unused_bregman_capital: bool = True
    block_directional_on_bregman_markets: bool = True
    block_directional_on_bregman_events: bool = True
    exploration_can_use_bregman_reserved_capacity: bool = False
    # ---- Pass-5: profitability-first ranking + hard after-cost governor ----
    # Candidates compete on conservative, executable, AFTER-COST expected value —
    # not surface quality or model score alone. Annotation runs before shortlist
    # truncation; the governor hard-rejects negative-after-cost trades. Safe
    # defaults; thresholds only TIGHTEN (never loosen EdgeEngine).
    profitability_first: bool = True
    require_profitability_annotation: bool = True
    min_after_cost_edge: float = 0.01
    min_after_cost_roi: float = 0.002
    min_expected_value_usd: float = 0.01
    profitability_sort_weight: float = 1.0
    model_score_sort_weight: float = 0.35
    liquidity_sort_weight: float = 0.25
    freshness_sort_weight: float = 0.25
    ambiguity_penalty_sort_weight: float = 0.50
    execution_drag_penalty_weight: float = 1.00
    bregman_profitability_first: bool = True
    bregman_min_after_cost_profit_usd: float = 0.02
    bregman_min_after_cost_roi: float = 0.002
    bregman_profit_sort_weight: float = 1.0
    bregman_risk_penalty_weight: float = 0.5
    # ---- portfolio risk + aggressive sizing (PAPER ONLY; hard-clamped) ----
    # Additive caps that only ever TIGHTEN the mandatory TrainingRiskGate/RiskEngine.
    max_event_exposure_usd: float = 20.0
    max_category_exposure_usd: float = 40.0
    max_bregman_bundle_exposure_usd: float = 30.0
    diversity_target: int = 5
    exploration_budget_usd: float = 20.0
    max_drawdown_usd: float = 50.0
    cvar_alpha: float = 0.95
    kelly_max_fraction: float = 0.05
    leg_failure_haircut: float = 0.5
    chainlink_freshness_penalty_weight: float = 0.5
    settlement_ambiguity_penalty_weight: float = 0.5
    # ---- adaptive capital allocation (micro-live readiness; PAPER ONLY) ----
    # Capital flows only to proven, calibrated, after-cost edge. These knobs only
    # ever TIGHTEN the mandatory risk gate; certified Bregman is first priority.
    capital_allocation_enabled: bool = True
    capital_min_after_cost_edge: float = 0.0          # min after-cost edge to fund
    max_correlated_cluster_exposure_usd: float = 40.0  # correlated-cluster cap
    max_strategy_exposure_usd: float = 40.0            # per-strategy bucket cap
    max_open_capital_lock_usd: float = 100.0           # total deployed capital lock
    # drawdown governor (reduce / pause / downgrade on degraded conditions)
    dd_governor_max_loss_streak: int = 5               # reduce above this streak
    dd_governor_pause_loss_streak: int = 10            # pause strategy above this
    dd_governor_soft_fraction: float = 0.5             # reduce once dd budget used
    dd_governor_calibration_limit: float = 0.15        # calibration-instability ceil
    dd_governor_execution_floor: float = 0.5           # min realised fill quality
    # ---- research advisory gates (research is NEVER allowed to override) ----
    # When research confidence is high, the market is held to a stricter ambiguity
    # bar (research can never push a trade on an ambiguous market).
    research_high_confidence: float = 0.8
    research_confident_ambiguity_frac: float = 0.6
    # ---- institutional features + grouping (additive; default ON, offline) ----
    feature_extraction_enabled: bool = True
    grouping_enabled: bool = True
    # ---- paper decision budget + feedback targets (aggressive widens these) ----
    paper_decision_budget: int = 30      # max candidates evaluated per tick
    feedback_sample_target: int = 200    # target feedback-loop samples
    tiny_trade_min_liquidity: float = 100.0  # liquidity floor for tiny paper trades
    # ---- anti-overfitting / walk-forward parameter governance (PAPER ONLY) ----
    # Aggressive mode may learn fast online, but production-like parameters
    # (thresholds, shrink factors, risk sizes, exploration) can only be promoted
    # when walk-forward validation passes. These never relax a risk gate.
    walk_forward_enabled: bool = False
    walk_forward_train: int = 6           # train window length (observations)
    walk_forward_test: int = 3            # test window length (observations)
    oos_degrade_tolerance: float = 0.2    # max tolerated OOS/IS degradation
    min_param_stability: float = 0.5      # min walk-forward stability to promote
    max_overfit_penalty: float = 0.5      # max IS->OOS penalty to promote
    overfit_rollback_tolerance: float = 0.05  # val-error slack before learner rollback
    aggressive_can_promote_params: bool = False  # gated until walk-forward passes
    # ---- experiment manager (controlled strategy-variant experiments; PAPER) ----
    # Run distinct strategy variants (bregman / statistical / directional /
    # chainlink / exploration) as controlled experiments with a PAPER-ONLY slot
    # budget split across variants. Never relaxes a hard risk cap.
    experiments_enabled: bool = False
    experiment_id: str = "exp_default"
    variant_budget_weights: dict = field(default_factory=dict)
    bregman_first_budget: bool = True
    # ---- monitoring + kill-switch (auto-downgrade aggressive->conservative) ----
    # Kill-switch trips on calibration deterioration, excessive drawdown, bad
    # labels, stale data, high partial-fill, Bregman false positives, spread
    # blowout, or feedback corruption. It only ever DOWNGRADES paper aggression;
    # it never touches a live control.
    kill_switch_enabled: bool = True
    kill_switch_auto_downgrade: bool = True
    ks_max_calibration_error: float = 0.20
    ks_max_brier_trend: float = 0.05
    ks_max_loss_streak: int = 10
    ks_max_label_suppression_rate: float = 0.5
    ks_max_ambiguous_rate: float = 0.5
    ks_max_stale_rejection_rate: float = 0.5
    ks_max_partial_fill_rate: float = 0.5
    ks_max_bregman_fp_rate: float = 0.10
    ks_max_avg_spread: float = 0.15
    ks_max_learner_rollbacks: int = 3
    ks_min_samples: int = 10
    # ---- live-readiness gate (verdicts only; NEVER enables live trading) ----
    # Blocks real-money escalation unless durable after-cost profitability,
    # execution realism, calibration, label quality, and risk-gate cleanliness
    # are proven. These thresholds + caps only PRODUCE VERDICTS; the engine has no
    # live execution path and this gate never flips a live flag.
    readiness_min_eval_samples: int = 30
    readiness_min_qualified_samples: int = 200
    readiness_min_canary_samples: int = 500
    readiness_min_canary_full_samples: int = 1000
    readiness_min_oos_sharpe: float = 1.0
    readiness_min_canary_sharpe: float = 1.5
    readiness_min_oos_sortino: float = 1.0
    readiness_min_oos_calmar: float = 0.5
    readiness_max_drawdown_pct: float = 0.15
    readiness_max_calibration_error: float = 0.10
    readiness_max_ece: float = 0.10
    readiness_max_label_suppression_rate: float = 0.20
    readiness_max_unresolved_rate: float = 0.20
    readiness_max_ambiguous_rate: float = 0.20
    readiness_max_stale_rejection_rate: float = 0.10
    # capital-preservation caps for a FUTURE manual live escalation (hard, tiny)
    live_micro_canary_notional_usd: float = 5.0
    live_canary_notional_usd: float = 25.0
    live_max_daily_loss_usd: float = 10.0
    live_max_per_market_usd: float = 5.0
    live_max_event_usd: float = 5.0
    # ---- institutional paper-training campaign (PAPER ONLY; default OFF) ----
    # Campaign mode freezes algorithm development and runs the aggressive paper
    # engine purely to collect durable evidence. It NEVER enables live trading and
    # NEVER relaxes a risk gate; algorithm_freeze_mode forces param promotion off.
    campaign_enabled: bool = False
    campaign_name: str = "institutional_paper_campaign"
    algorithm_freeze_mode: bool = False
    campaign_start_ts: object = None       # float|int|ISO-string|None
    campaign_target_min_days: int = 14
    campaign_target_min_decisions: int = 1000
    campaign_target_min_paper_trades: int = 300
    campaign_target_min_resolved_labels: int = 100
    campaign_target_min_bregman_candidates: int = 50
    campaign_max_bregman_false_positives: int = 0
    # ---- campaign-safe profile (read-only realism features; fail-closed) ----
    # These only ever TIGHTEN safety: read-only CLOB/Chainlink, realistic fills,
    # the clean-label guard, and a mandatory RiskEngine. They never enable a live
    # path. Default OFF (NOT a global production default); turned on only by
    # ``institutional_campaign_defaults()`` / ``--campaign-safe-profile``.
    campaign_safe_profile: bool = False
    clob_read_only: bool = True            # CLOB v2 feed is consume-only (no submit)
    chainlink_read_only: bool = True       # Chainlink is price/feature-only (advisory)
    realistic_fill_enabled: bool = False   # slippage+depth fills, no fantasy fills
    clean_label_guard: bool = True         # only clean settled labels train (mandatory)
    risk_engine_enabled: bool = True       # RiskEngine is mandatory (no bypass)
    # ---- controlled market-news evidence scanner (advisory only; default OFF) ----
    # News is scanned, cached, timestamped, scored, sanitized, and handed to Grok
    # as a bounded read-only packet. It NEVER sizes/approves/submits a trade,
    # never bypasses EdgeEngine/RiskEngine/Bregman, and never makes a campaign
    # micro_canary_ready. Live provider mode is opt-in and never used in replay.
    news_scanner_enabled: bool = False
    news_provider_mode: str = "offline_cache"   # offline_cache | fixture | live_read_only
    news_live_read_only: bool = True
    news_max_queries_per_market: int = 3
    news_max_items_per_market: int = 8
    news_max_snippet_chars: int = 500
    news_cache_ttl_seconds: int = 1800
    news_min_relevance_score: float = 0.2
    news_min_source_credibility: float = 0.4
    news_enable_grok_packet: bool = True
    news_replay_timestamp_safe: bool = True
    # ---- news quality filters (advisory; tighten weak/stale items) ----
    news_advisory_enabled: bool = True
    news_trade_gate_enabled: bool = False        # advisory only — never gates a trade
    news_require_published_at: bool = False
    news_reject_unclear_date: bool = False
    news_max_age_hours: float = 0.0              # 0 = no age cap
    # ---- BTC 5-min Pulse PAPER-ONLY isolated experiment (default OFF) ----
    # An isolated simulated training module that runs beside the Polymarket
    # campaign for fast feedback. PAPER ONLY: it never places a live order,
    # never touches a wallet, never enables legacy BTC autotrade, and never
    # writes to the Polymarket learner namespace. Fail-closed on unsafe flags.
    btc_pulse_enabled: bool = False
    btc_pulse_paper_only: bool = True
    btc_pulse_isolated_learning: bool = True
    btc_pulse_allow_transfer_learning: bool = False
    btc_pulse_live_enabled: bool = False
    btc_pulse_legacy_autotrade_enabled: bool = False
    btc_pulse_tick_seconds: int = 30
    btc_pulse_round_seconds: int = 300
    btc_pulse_max_paper_notional_per_trade: float = 5.0
    btc_pulse_max_paper_trades_per_hour: int = 60
    btc_pulse_max_daily_paper_loss: float = 50.0
    btc_pulse_min_ev_threshold: float = 0.0
    btc_pulse_require_positive_ev: bool = True
    btc_pulse_require_risk_gate: bool = True
    btc_pulse_require_realistic_fill: bool = True
    # After-cost SHADOW GATE (default ON): demotes marginal rounds to shadow-only so
    # only proven positive-after-cost edges open a paper trade. Set OFF (env
    # BTC_PULSE_SHADOW_GATE_ENABLED=0) to LOOSEN trade frequency for higher training
    # volume — still PAPER ONLY, still gated by EV/risk caps + realistic fills.
    btc_pulse_shadow_gate_enabled: bool = True
    # ---- BTC Pulse Chainlink BTC/USD oracle gate (PAPER ONLY) ----
    # When required, BTC Pulse must use a FRESH Chainlink BTC/USD reading as its
    # reference price and skips new paper trades when the oracle is missing,
    # stale, invalid, or errored (recorded as oracle-blocked observations).
    btc_pulse_require_chainlink: bool = False
    btc_pulse_chainlink_heartbeat_seconds: int = 120
    btc_pulse_chainlink_max_age_seconds: int = 180
    btc_pulse_oracle_debug_log: bool = False
    # ---- Fast read-only BTC spot feed (short-horizon features) ----
    # Chainlink is the slow ANCHOR; this is a fast (seconds-fresh) spot price for
    # 30s/60s/300s returns. Read-only, key-less, paper-only.
    btc_fast_price_enabled: bool = False
    btc_fast_price_provider: str = "coinbase_readonly"
    btc_fast_price_symbol: str = "BTC-USD"
    btc_fast_price_max_age_seconds: int = 10
    btc_fast_price_timeout_seconds: float = 5.0
    btc_fast_price_max_retries: int = 2
    btc_fast_price_log_enabled: bool = False
    btc_pulse_require_fast_price: bool = False
    btc_pulse_max_oracle_disagreement_bps: float = 50.0
    btc_pulse_block_chop_regime: bool = False
    btc_pulse_min_fill_realism_score: float = 0.0
    # ---- 10x Feedback Accelerator (PAPER ONLY; default OFF) ----
    # Increases TRAINING FEEDBACK (decisions, shadow labels, no-trade labels,
    # tiny exploration trades) WITHOUT loosening any hard safety gate. Only soft
    # paper-training gates relax, and only for tiny exploration. Exploration is
    # tiny/capped/labeled/isolated and never counts as live-readiness proof until
    # cleanly resolved + validated.
    feedback_accelerator_enabled: bool = False
    feedback_accelerator_target_multiplier: int = 10
    feedback_accelerator_mode: str = "paper_only"
    exploration_tiny_size_enabled: bool = True
    exploration_notional_fraction: float = 0.002   # very small (of equity)
    exploration_max_trades_per_hour: int = 30
    exploration_max_daily_loss: float = 20.0
    exploration_max_event_exposure: float = 5.0
    exploration_max_category_exposure: float = 10.0
    exploration_min_book_freshness_required: bool = True
    exploration_requires_realistic_fill: bool = True
    exploration_requires_risk_gate: bool = True
    exploration_can_use_soft_edge: bool = True
    exploration_can_bypass_hard_gate: bool = False   # MUST stay False (invariant)
    exploration_counts_for_readiness: bool = False
    shadow_decision_logging_enabled: bool = True
    no_trade_labeling_enabled: bool = True
    btc_pulse_feedback_acceleration_enabled: bool = True
    polymarket_feedback_acceleration_enabled: bool = True
    # ---- run / sim ----
    take_profit: float = 0.05
    stop_loss: float = 0.05
    max_hold_ticks: int = 20
    signal_model: str = "research"
    starting_bankroll: float = 500.0
    universe: object = None

    # back-compat aliases used by older v1 code/tests
    @property
    def max_allowed_spread(self) -> float:
        return self.universe.max_allowed_spread if self.universe else 0.04

    @property
    def min_top_of_book_depth_usd(self) -> float:
        return self.min_depth_at_price

    @property
    def max_ambiguity(self) -> float:
        return self.max_ambiguity_score

    @property
    def min_evidence(self) -> float:
        return self.min_evidence_score

    @property
    def kelly_enabled(self) -> bool:
        return self.use_kelly_for_size

    @property
    def kelly_fraction_cap(self) -> float:
        return self.kelly_fraction

    @property
    def kelly_multiplier(self) -> float:
        return 1.0

    def __post_init__(self):
        if self.mode not in MODES:
            self.mode = "observe_only"
        # Algorithm-freeze (campaign) mode: evidence quality over new code. It can
        # NEVER promote production-like parameters and NEVER touches a live flag.
        if bool(self.algorithm_freeze_mode):
            self.aggressive_can_promote_params = False
        self.campaign_target_min_days = max(0, int(self.campaign_target_min_days))
        self.campaign_target_min_decisions = max(0, int(self.campaign_target_min_decisions))
        self.campaign_target_min_paper_trades = max(0, int(self.campaign_target_min_paper_trades))
        self.campaign_target_min_resolved_labels = max(
            0, int(self.campaign_target_min_resolved_labels))
        self.campaign_target_min_bregman_candidates = max(
            0, int(self.campaign_target_min_bregman_candidates))
        self.campaign_max_bregman_false_positives = max(
            0, int(self.campaign_max_bregman_false_positives))
        # Campaign-safe profile: force every read-only / realism / guard invariant
        # ON and every fantasy-fill / promotion path OFF — even if constructed
        # with unsafe overrides. This NEVER enables a live path.
        if bool(self.campaign_safe_profile):
            self.campaign_enabled = True
            self.algorithm_freeze_mode = True
            self.aggressive_can_promote_params = False
            self.clob_read_only = True
            self.chainlink_read_only = True
            self.realistic_fill_enabled = True
            self.clean_label_guard = True
            self.risk_engine_enabled = True
            self.allow_pm_reference_price_fills = False
            self.reject_on_stale_book = True
            self.disable_btc_pulse_trading = True
            # News scanner under campaign-safe profile: read-only + timestamped +
            # cached only. It may be enabled in offline_cache or live_read_only
            # mode, but can NEVER trigger live orders or bypass any guard.
            self.news_live_read_only = True
            self.news_replay_timestamp_safe = True
            if self.news_provider_mode not in ("offline_cache", "fixture", "live_read_only"):
                self.news_provider_mode = "offline_cache"
        # news scanner clamps (bounded; advisory only — cannot relax a risk gate)
        if self.news_provider_mode not in ("offline_cache", "fixture", "live_read_only"):
            self.news_provider_mode = "offline_cache"
        self.news_max_queries_per_market = max(1, min(int(self.news_max_queries_per_market), 10))
        self.news_max_items_per_market = max(1, min(int(self.news_max_items_per_market), 25))
        self.news_max_snippet_chars = max(50, min(int(self.news_max_snippet_chars), 2000))
        self.news_cache_ttl_seconds = max(0, min(int(self.news_cache_ttl_seconds), 86400))
        self.news_min_relevance_score = max(0.0, min(1.0, float(self.news_min_relevance_score)))
        self.news_min_source_credibility = max(
            0.0, min(1.0, float(self.news_min_source_credibility)))
        # BTC Pulse PAPER clamps (advisory experiment; cannot exceed paper caps)
        self.btc_pulse_tick_seconds = max(1, min(int(self.btc_pulse_tick_seconds), 3600))
        self.btc_pulse_round_seconds = max(
            self.btc_pulse_tick_seconds, min(int(self.btc_pulse_round_seconds), 86400))
        self.btc_pulse_max_paper_notional_per_trade = max(
            0.0, min(float(self.btc_pulse_max_paper_notional_per_trade), 50.0))
        self.btc_pulse_max_paper_trades_per_hour = max(
            0, min(int(self.btc_pulse_max_paper_trades_per_hour), 100000))
        self.btc_pulse_max_daily_paper_loss = max(
            0.0, min(float(self.btc_pulse_max_daily_paper_loss), 500.0))
        self.btc_pulse_chainlink_heartbeat_seconds = max(
            1, min(int(self.btc_pulse_chainlink_heartbeat_seconds), 86400))
        self.btc_pulse_chainlink_max_age_seconds = max(
            1, min(int(self.btc_pulse_chainlink_max_age_seconds), 86400))
        self.btc_fast_price_max_age_seconds = max(
            1, min(int(self.btc_fast_price_max_age_seconds), 3600))
        self.btc_fast_price_timeout_seconds = max(
            0.5, min(float(self.btc_fast_price_timeout_seconds), 60.0))
        self.btc_fast_price_max_retries = max(0, min(int(self.btc_fast_price_max_retries), 10))
        self.btc_pulse_max_oracle_disagreement_bps = max(
            0.0, min(float(self.btc_pulse_max_oracle_disagreement_bps), 10000.0))
        self.btc_pulse_min_fill_realism_score = max(
            0.0, min(float(self.btc_pulse_min_fill_realism_score), 1.0))
        self.news_max_age_hours = max(0.0, min(float(self.news_max_age_hours), 8760.0))
        # Campaign-safe profile: if pulse is explicitly enabled it MUST stay
        # paper-only + isolated, with live + legacy autotrade hard-off. This
        # never enables a live path; it only ever tightens the pulse experiment.
        if bool(self.campaign_safe_profile) and bool(self.btc_pulse_enabled):
            self.btc_pulse_paper_only = True
            self.btc_pulse_isolated_learning = True
            self.btc_pulse_live_enabled = False
            self.btc_pulse_legacy_autotrade_enabled = False
            self.btc_pulse_require_risk_gate = True
            self.btc_pulse_require_realistic_fill = True
        # Feedback Accelerator (PAPER ONLY): exploration MUST keep every hard gate
        # required and can NEVER bypass a hard gate. These are hard invariants —
        # re-asserted even if constructed with unsafe overrides.
        self.exploration_can_bypass_hard_gate = False
        self.exploration_requires_realistic_fill = True
        self.exploration_requires_risk_gate = True
        self.exploration_min_book_freshness_required = True
        self.feedback_accelerator_mode = "paper_only"
        self.feedback_accelerator_target_multiplier = max(
            1, min(int(self.feedback_accelerator_target_multiplier), 20))
        self.exploration_notional_fraction = max(
            0.0, min(float(self.exploration_notional_fraction), 0.02))
        self.exploration_max_trades_per_hour = max(
            0, min(int(self.exploration_max_trades_per_hour), 100000))
        self.exploration_max_daily_loss = max(
            0.0, min(float(self.exploration_max_daily_loss), 100.0))
        self.exploration_max_event_exposure = max(
            0.0, min(float(self.exploration_max_event_exposure), 50.0))
        self.exploration_max_category_exposure = max(
            0.0, min(float(self.exploration_max_category_exposure), 200.0))
        if bool(self.campaign_safe_profile):
            # Exploration trades NEVER count as proven readiness edge in a campaign.
            self.exploration_counts_for_readiness = False
        # hard PAPER clamps (cannot exceed even if env is misconfigured)
        self.fixed_notional_usd = max(0.0, min(self.fixed_notional_usd, 50.0))
        self.max_kelly_size_usd = max(0.0, min(self.max_kelly_size_usd, 50.0))
        self.max_market_exposure_usd = max(0.0, min(self.max_market_exposure_usd, 500.0))
        self.max_total_exposure_usd = max(0.0, min(self.max_total_exposure_usd, 5000.0))
        self.max_open_trades = max(0, min(self.max_open_trades, self.max_open_trades_hard_cap, 8))
        self.scan_limit = max(1, min(self.scan_limit, 2000))
        self.base_shrink_factor = max(self.min_shrink_factor,
                                      min(self.base_shrink_factor, self.max_shrink_factor))
        # exploration is bounded + PAPER-only: clamp rate and cap exploratory size
        # to the same hard paper order-notional ceiling (cannot bypass risk caps).
        self.exploration_rate = max(0.0, min(1.0, self.exploration_rate))
        self.exploration_notional_usd = max(0.0, min(self.exploration_notional_usd,
                                                     self.max_order_notional_usd))
        # active-learning controls (bounded; cannot relax any hard risk gate)
        self.exploration_split = max(0.0, min(1.0, float(self.exploration_split)))
        self.category_sample_target = max(1, min(int(self.category_sample_target), 100000))
        self.max_explore_per_category = max(0, min(int(self.max_explore_per_category), 100))
        self.max_explore_per_event = max(0, min(int(self.max_explore_per_event), 100))
        # hard PAPER clamps for the portfolio caps (cannot be raised by config/env)
        self.max_event_exposure_usd = max(0.0, min(self.max_event_exposure_usd, 500.0))
        self.max_category_exposure_usd = max(0.0, min(self.max_category_exposure_usd, 1000.0))
        self.max_bregman_bundle_exposure_usd = max(
            0.0, min(self.max_bregman_bundle_exposure_usd, 1000.0))
        self.exploration_budget_usd = max(0.0, min(self.exploration_budget_usd, 200.0))
        self.max_drawdown_usd = max(0.0, min(self.max_drawdown_usd, 5000.0))
        self.diversity_target = max(0, min(int(self.diversity_target), 100))
        # anti-overfitting governance clamps (bounded; cannot relax a risk gate)
        self.walk_forward_train = max(2, min(int(self.walk_forward_train), 100000))
        self.walk_forward_test = max(1, min(int(self.walk_forward_test), 100000))
        self.oos_degrade_tolerance = max(0.0, min(1.0, float(self.oos_degrade_tolerance)))
        self.min_param_stability = max(0.0, min(1.0, float(self.min_param_stability)))
        self.max_overfit_penalty = max(0.0, min(1.0, float(self.max_overfit_penalty)))
        self.overfit_rollback_tolerance = max(0.0, min(1.0, float(self.overfit_rollback_tolerance)))
        self.cvar_alpha = min(0.999, max(0.5, self.cvar_alpha))
        self.kelly_max_fraction = max(0.0, min(self.kelly_max_fraction, 0.5))
        self.leg_failure_haircut = max(0.0, min(self.leg_failure_haircut, 1.0))
        # adaptive capital allocation clamps (only ever TIGHTEN the risk gate)
        self.capital_min_after_cost_edge = max(0.0, min(
            float(self.capital_min_after_cost_edge), 1.0))
        self.max_correlated_cluster_exposure_usd = max(0.0, min(
            float(self.max_correlated_cluster_exposure_usd), 2000.0))
        self.max_strategy_exposure_usd = max(0.0, min(
            float(self.max_strategy_exposure_usd), 2000.0))
        self.max_open_capital_lock_usd = max(0.0, min(
            float(self.max_open_capital_lock_usd), 5000.0))
        self.dd_governor_max_loss_streak = max(1, min(
            int(self.dd_governor_max_loss_streak), 100000))
        self.dd_governor_pause_loss_streak = max(
            int(self.dd_governor_max_loss_streak),
            min(int(self.dd_governor_pause_loss_streak), 100000))
        self.dd_governor_soft_fraction = max(0.0, min(float(self.dd_governor_soft_fraction), 1.0))
        self.dd_governor_calibration_limit = max(0.0, min(
            float(self.dd_governor_calibration_limit), 1.0))
        self.dd_governor_execution_floor = max(0.0, min(
            float(self.dd_governor_execution_floor), 1.0))
        # capital-preservation hard ceilings (tiny; cannot be raised by env/config)
        self.live_micro_canary_notional_usd = max(0.0, min(self.live_micro_canary_notional_usd, 25.0))
        self.live_canary_notional_usd = max(0.0, min(self.live_canary_notional_usd, 100.0))
        self.live_max_daily_loss_usd = max(0.0, min(self.live_max_daily_loss_usd, 50.0))
        self.live_max_per_market_usd = max(0.0, min(self.live_max_per_market_usd, 50.0))
        self.live_max_event_usd = max(0.0, min(self.live_max_event_usd, 50.0))
        self.readiness_max_drawdown_pct = max(0.0, min(1.0, float(self.readiness_max_drawdown_pct)))
        self.research_high_confidence = max(0.0, min(1.0, float(self.research_high_confidence)))
        self.research_confident_ambiguity_frac = max(
            0.0, min(1.0, float(self.research_confident_ambiguity_frac)))
        if self.universe is None:
            self.universe = um.UniverseConfig.from_env()

    @property
    def is_paper_only(self) -> bool:
        """Hard invariant: this config can only drive PAPER training. Live order
        execution is never reachable from a TrainingConfig (no live mode exists)."""
        return self.mode in ("disabled", "observe_only", "paper_train")

    @property
    def max_order_notional_usd(self) -> float:
        return max(self.fixed_notional_usd, self.max_kelly_size_usd)

    @classmethod
    def from_env(cls) -> "TrainingConfig":
        # Campaign-safe profile shortcut: when the operator sets the safe-profile
        # env, resolve to the canonical institutional campaign defaults (aggressive
        # paper + all read-only realism features ON, every live path OFF). This is
        # opt-in and never a global production default.
        if _envb("POLYMARKET_CAMPAIGN_SAFE_PROFILE", False):
            overrides = {}
            if os.getenv("POLYMARKET_CAMPAIGN_NAME"):
                overrides["campaign_name"] = os.getenv("POLYMARKET_CAMPAIGN_NAME")
            return cls.institutional_campaign_defaults(**overrides)
        ucfg = um.UniverseConfig.from_env()
        mode = (os.getenv("POLYMARKET_TRAINING_MODE") or "observe_only").strip().lower()
        return cls(
            mode=mode,
            polymarket_only=_envb("POLYMARKET_ONLY_MODE", True),
            disable_btc_pulse_trading=_envb("DISABLE_BTC_PULSE_TRADING", True),
            scan_limit=_envi("MARKET_SCAN_LIMIT", _envi("POLYMARKET_SCAN_LIMIT", 1000)),
            scan_interval_seconds=_envf("POLYMARKET_SCAN_INTERVAL_SECONDS", 60.0),
            metadata_cache_ttl_s=_envf("POLYMARKET_METADATA_CACHE_TTL_SECONDS", 60.0),
            incremental_refresh=_envb("POLYMARKET_INCREMENTAL_REFRESH", True),
            async_scan=_envb("POLYMARKET_ASYNC_SCAN", True),
            max_concurrent_requests=_envi("POLYMARKET_MAX_CONCURRENT_REQUESTS", 8),
            scan_timeout_s=_envf("POLYMARKET_SCAN_TIMEOUT_SECONDS", 20.0),
            rate_limit_sleep_ms=_envf("POLYMARKET_RATE_LIMIT_SLEEP_MS", 100.0),
            min_volume=_envf("POLYMARKET_MIN_VOLUME", 1000.0),
            min_liquidity=_envf("POLYMARKET_MIN_LIQUIDITY", 250.0),
            min_time_to_close_s=_envf("POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS", 3600.0),
            max_time_to_close_days=_envf("POLYMARKET_MAX_TIME_TO_CLOSE_DAYS", 90.0),
            shortlist_limit=_envi("MARKET_SHORTLIST_LIMIT", _envi("POLYMARKET_SHORTLIST_LIMIT", 150)),
            live_watch_limit=_envi("MARKET_LIVE_WATCHLIST_LIMIT",
                                   _envi("POLYMARKET_LIVE_WATCH_LIMIT", 100)),
            trade_candidate_limit=_envi("MARKET_TRADE_CANDIDATE_LIMIT",
                                        _envi("POLYMARKET_TRADE_CANDIDATE_LIMIT", 30)),
            clob_enabled=_envb("POLYMARKET_CLOB_ENABLED", True),
            subscribe_trending=_envb("POLYMARKET_CLOB_SUBSCRIBE_TRENDING", True),
            clob_stale_ms=_envf("POLYMARKET_CLOB_STALE_MS", 3000.0),
            subscription_refresh_s=_envf("POLYMARKET_SUBSCRIPTION_REFRESH_SECONDS", 120.0),
            max_subscription_churn=_envi("POLYMARKET_MAX_SUBSCRIPTION_CHURN_PER_REFRESH", 20),
            require_research_or_model_edge=_envb("POLYMARKET_REQUIRE_RESEARCH_OR_MODEL_EDGE", True),
            allow_offline_stub_trading=_envb("POLYMARKET_ALLOW_OFFLINE_STUB_TRADING", False),
            research_max_age_s=_envf("POLYMARKET_RESEARCH_MAX_AGE_SECONDS", 900.0),
            min_evidence_score=_envf("POLYMARKET_MIN_EVIDENCE_SCORE", 0.50),
            min_source_count=_envi("POLYMARKET_MIN_SOURCE_COUNT", 2),
            max_ambiguity_score=_envf("POLYMARKET_MAX_AMBIGUITY_SCORE", 0.35),
            base_shrink_factor=_envf("POLYMARKET_BASE_SHRINK_FACTOR", 0.25),
            max_shrink_factor=_envf("POLYMARKET_MAX_SHRINK_FACTOR", 0.60),
            min_shrink_factor=_envf("POLYMARKET_MIN_SHRINK_FACTOR", 0.05),
            min_net_edge=_envf("POLYMARKET_MIN_NET_EDGE", 0.03),
            base_uncertainty=_envf("POLYMARKET_BASE_UNCERTAINTY", 0.01),
            spread_penalty_weight=_envf("POLYMARKET_SPREAD_PENALTY_WEIGHT", 0.50),
            slippage_penalty_weight=_envf("POLYMARKET_SLIPPAGE_PENALTY_WEIGHT", 1.00),
            ambiguity_penalty_weight=_envf("POLYMARKET_AMBIGUITY_PENALTY_WEIGHT", 0.03),
            stale_penalty_weight=_envf("POLYMARKET_STALE_PENALTY_WEIGHT", 0.02),
            evidence_penalty_weight=_envf("POLYMARKET_EVIDENCE_PENALTY_WEIGHT", 0.02),
            calibration_penalty_weight=_envf("POLYMARKET_CALIBRATION_PENALTY_WEIGHT", 0.05),
            liquidity_penalty_weight=_envf("POLYMARKET_LIQUIDITY_PENALTY_WEIGHT", 0.03),
            max_spread=_envf("POLYMARKET_MAX_SPREAD", 0.08),
            min_depth_at_price=_envf("POLYMARKET_MIN_DEPTH_AT_PRICE", 50.0),
            taker_fee_bps=_envf("PAPER_TAKER_FEE_BPS", 0.0),
            slippage_bps=_envf("PAPER_SLIPPAGE_BPS", 25.0),
            max_fill_depth_fraction=_envf("PAPER_MAX_FILL_DEPTH_FRACTION", 0.35),
            fixed_notional_usd=_envf("POLYMARKET_PAPER_FIXED_NOTIONAL_USD", 5.0),
            max_open_trades=_envi("POLYMARKET_MAX_OPEN_TRADES", 5),
            max_open_trades_hard_cap=_envi("POLYMARKET_MAX_OPEN_TRADES_HARD_CAP", 8),
            max_market_exposure_usd=_envf("POLYMARKET_MAX_MARKET_EXPOSURE_USD", 20.0),
            max_total_exposure_usd=_envf("POLYMARKET_MAX_TOTAL_EXPOSURE_USD", 100.0),
            max_daily_loss_usd=_envf("POLYMARKET_MAX_DAILY_LOSS_USD", 50.0),
            max_open_orders=_envi("PAPER_MAX_OPEN_ORDERS", 20),
            reject_on_stale_book=_envb("PAPER_REJECT_ON_STALE_BOOK", True),
            allow_pm_reference_price_fills=_envb("PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS", False),
            max_book_age_sec=_envf("POLYMARKET_MAX_BOOK_AGE_SEC", 20.0),
            require_executable_ask=_envb("POLYMARKET_REQUIRE_EXECUTABLE_ASK", True),
            reject_missing_ask=_envb("POLYMARKET_REJECT_MISSING_ASK", True),
            reject_offline_stub_fills=_envb("POLYMARKET_REJECT_OFFLINE_STUB_FILLS", True),
            bregman_require_executable_all_legs=_envb(
                "POLYMARKET_BREGMAN_REQUIRE_EXECUTABLE_ALL_LEGS", True),
            bregman_allow_reference_fills=_envb("POLYMARKET_BREGMAN_ALLOW_REFERENCE_FILLS", False),
            bregman_max_book_age_sec=_envf("POLYMARKET_BREGMAN_MAX_BOOK_AGE_SEC", 20.0),
            use_kelly_for_diagnostics=_envb("POLYMARKET_USE_KELLY_FOR_DIAGNOSTICS", True),
            use_kelly_for_size=_envb("POLYMARKET_USE_KELLY_FOR_SIZE", False),
            kelly_fraction=_envf("POLYMARKET_KELLY_FRACTION", 0.10),
            max_kelly_size_usd=_envf("POLYMARKET_MAX_KELLY_SIZE_USD", 5.0),
            learner_enabled=_envb("POLYMARKET_LEARNER_ENABLED", True),
            ewma_alpha=_envf("POLYMARKET_LEARNER_EWMA_ALPHA", 0.05),
            min_bucket_samples=_envi("POLYMARKET_MIN_BUCKET_SAMPLES", 20),
            feedback_interval_seconds=_envf("POLYMARKET_FEEDBACK_INTERVAL_SECONDS", 300.0),
            markout_horizons_s=tuple(_env_csv_floats(
                "POLYMARKET_MARKOUT_HORIZONS_SECONDS", "5,30,60,300,900,3600")),
            feedback_enabled=_envb("POLYMARKET_TRAINING_FEEDBACK_ENABLED", True),
            exploration_enabled=(_envb("POLYMARKET_EXPLORATION_ENABLED", False)
                                 or _envb("EXPLORATION_ENABLED", False)),
            exploration_rate=_envf("POLYMARKET_EXPLORATION_RATE", 0.0),
            exploration_notional_usd=_envf("POLYMARKET_EXPLORATION_NOTIONAL_USD", 2.0),
            exploration_min_edge=_envf("POLYMARKET_EXPLORATION_MIN_EDGE", -0.01),
            random_exploration_enabled=_envb("POLYMARKET_RANDOM_EXPLORATION_ENABLED", False),
            exploration_max_trades_per_tick=_envi("POLYMARKET_EXPLORATION_MAX_TRADES_PER_TICK", 2),
            exploration_max_open_trades=_envi("POLYMARKET_EXPLORATION_MAX_OPEN_TRADES", 10),
            exploration_max_capital_per_tick_usd=_envf(
                "POLYMARKET_EXPLORATION_MAX_CAPITAL_PER_TICK", 20.0),
            exploration_max_position_size_usd=_envf("POLYMARKET_EXPLORATION_MAX_POSITION_SIZE", 5.0),
            exploration_max_expected_loss_usd=_envf(
                "POLYMARKET_EXPLORATION_MAX_EXPECTED_LOSS_USD", 0.25),
            exploration_min_depth_at_price=_envf("POLYMARKET_EXPLORATION_MIN_DEPTH_AT_PRICE", 25.0),
            exploration_max_spread=_envf("POLYMARKET_EXPLORATION_MAX_SPREAD", 0.08),
            exploration_max_book_age_sec=_envf("POLYMARKET_EXPLORATION_MAX_BOOK_AGE_SEC", 20.0),
            exploration_max_ambiguity_score=_envf("POLYMARKET_EXPLORATION_MAX_AMBIGUITY_SCORE", 0.45),
            exploration_require_profitability_annotation=_envb(
                "POLYMARKET_EXPLORATION_REQUIRE_PROFITABILITY_ANNOTATION", True),
            exploration_require_realistic_fill=_envb(
                "POLYMARKET_EXPLORATION_REQUIRE_REALISTIC_FILL", True),
            exploration_count_toward_readiness=_envb(
                "POLYMARKET_EXPLORATION_COUNT_TOWARD_READINESS", False),
            exploration_max_per_event=_envi("POLYMARKET_EXPLORATION_MAX_PER_EVENT", 1),
            exploration_max_per_cluster=_envi("POLYMARKET_EXPLORATION_MAX_PER_CLUSTER", 1),
            exploration_max_per_category_per_tick=_envi(
                "POLYMARKET_EXPLORATION_MAX_PER_CATEGORY_PER_TICK", 2),
            active_learning_shadow_samples_per_tick=_envi(
                "POLYMARKET_ACTIVE_LEARNING_SHADOW_SAMPLES_PER_TICK", 50),
            active_learning_tiny_trades_per_tick=_envi(
                "POLYMARKET_ACTIVE_LEARNING_TINY_TRADES_PER_TICK", 2),
            active_learning_near_miss_samples_per_tick=_envi(
                "POLYMARKET_ACTIVE_LEARNING_NEAR_MISS_SAMPLES_PER_TICK", 50),
            active_learning_no_trade_labels_per_tick=_envi(
                "POLYMARKET_ACTIVE_LEARNING_NO_TRADE_LABELS_PER_TICK", 100),
            active_learning_diagnostic_samples_per_tick=_envi(
                "POLYMARKET_ACTIVE_LEARNING_DIAGNOSTIC_SAMPLES_PER_TICK", 50),
            # canonical GROK_PROOF_CALL_* env names (default ON for paper training),
            # with POLYMARKET_-prefixed names accepted as fallback. Advisory + bounded.
            grok_proof_call_enabled=_envb(
                "GROK_PROOF_CALL_ENABLED",
                _envb("POLYMARKET_GROK_PROOF_CALL_ENABLED", True)),
            grok_proof_call_max_per_hour=_envi(
                "GROK_PROOF_CALL_MAX_PER_HOUR",
                _envi("POLYMARKET_GROK_PROOF_CALL_MAX_PER_HOUR", 1)),
            grok_proof_call_max_per_run=_envi(
                "GROK_PROOF_CALL_MAX_PER_RUN",
                _envi("POLYMARKET_GROK_PROOF_CALL_MAX_PER_RUN", 1)),
            grok_proof_call_min_interval_seconds=_envi(
                "GROK_PROOF_CALL_MIN_INTERVAL_SECONDS", 900),
            grok_advisory_enabled=_envb("GROK_ADVISORY_ENABLED", True),
            grok_advisory_max_calls_per_hour=_envi("GROK_ADVISORY_MAX_CALLS_PER_HOUR", 4),
            grok_advisory_min_interval_seconds=_envi(
                "GROK_ADVISORY_MIN_INTERVAL_SECONDS", 900),
            grok_advisory_require_news=_envb("GROK_ADVISORY_REQUIRE_NEWS", True),
            grok_advisory_max_calls_per_run=_envi("GROK_ADVISORY_MAX_CALLS_PER_RUN", 48),
            grok_proof_call_advisory_only=_envb(
                "GROK_PROOF_CALL_ADVISORY_ONLY",
                _envb("POLYMARKET_GROK_PROOF_CALL_ADVISORY_ONLY", True)),
            active_learning_require_realistic_fill_for_trade=_envb(
                "POLYMARKET_ACTIVE_LEARNING_REQUIRE_REALISTIC_FILL_FOR_TRADE", True),
            active_learning_allow_shadow_without_fill=_envb(
                "POLYMARKET_ACTIVE_LEARNING_ALLOW_SHADOW_WITHOUT_FILL", True),
            correlation_gate_enabled=_envb("POLYMARKET_CORRELATION_GATE_ENABLED", True),
            require_cluster_metadata=_envb("POLYMARKET_REQUIRE_CLUSTER_METADATA", True),
            unknown_cluster_policy=os.getenv("POLYMARKET_UNKNOWN_CLUSTER_POLICY", "shadow"),
            max_open_per_market=_envi("POLYMARKET_MAX_OPEN_PER_MARKET", 1),
            max_open_per_event=_envi("POLYMARKET_MAX_OPEN_PER_EVENT", 1),
            max_open_per_cluster=_envi("POLYMARKET_MAX_OPEN_PER_CLUSTER", 1),
            max_cluster_exposure_usd=_envf("POLYMARKET_MAX_CLUSTER_EXPOSURE_USD", 25.0),
            max_correlation_group_exposure_usd=_envf(
                "POLYMARKET_MAX_CORRELATION_GROUP_EXPOSURE_USD", 50.0),
            block_duplicate_market=_envb("POLYMARKET_BLOCK_DUPLICATE_MARKET", True),
            block_duplicate_event=_envb("POLYMARKET_BLOCK_DUPLICATE_EVENT", True),
            block_duplicate_cluster=_envb("POLYMARKET_BLOCK_DUPLICATE_CLUSTER", True),
            block_exploration_on_bregman_markets=_envb(
                "POLYMARKET_BLOCK_EXPLORATION_ON_BREGMAN_MARKETS", True),
            block_exploration_on_bregman_events=_envb(
                "POLYMARKET_BLOCK_EXPLORATION_ON_BREGMAN_EVENTS", True),
            correlation_allow_size_cap=_envb("POLYMARKET_CORRELATION_ALLOW_SIZE_CAP", True),
            bregman_block_duplicate_bundles=_envb("POLYMARKET_BREGMAN_BLOCK_DUPLICATE_BUNDLES", True),
            bregman_block_overlapping_bundles=_envb(
                "POLYMARKET_BREGMAN_BLOCK_OVERLAPPING_BUNDLES", True),
            bregman_max_open_per_event=_envi("POLYMARKET_BREGMAN_MAX_OPEN_PER_EVENT", 1),
            bregman_max_cluster_exposure_usd=_envf(
                "POLYMARKET_BREGMAN_MAX_CLUSTER_EXPOSURE_USD", 100.0),
            active_learning_enabled=(_envb("POLYMARKET_ACTIVE_LEARNING_ENABLED", True)
                                     or _envb("ACTIVE_LEARNING_ENABLED", False)),
            exploration_split=_envf("POLYMARKET_EXPLORATION_SPLIT", 0.5),
            category_sample_target=_envi("POLYMARKET_CATEGORY_SAMPLE_TARGET", 50),
            max_explore_per_category=_envi("POLYMARKET_MAX_EXPLORE_PER_CATEGORY", 3),
            max_explore_per_event=_envi("POLYMARKET_MAX_EXPLORE_PER_EVENT", 1),
            chainlink_enabled=_envb("CHAINLINK_ENABLED", False),
            chainlink_history_limit=_envi("CHAINLINK_HISTORY_LIMIT", 30),
            bregman_enabled=_envb("POLYMARKET_BREGMAN_ENABLED", True),
            bregman_execution_enabled=_envb("POLYMARKET_BREGMAN_EXECUTION_ENABLED", True),
            directional_execution_enabled=_envb("POLYMARKET_DIRECTIONAL_EXECUTION_ENABLED", True),
            bregman_min_profit_usd=_envf("POLYMARKET_BREGMAN_MIN_PROFIT_USD", 0.001),
            bregman_target_capital_usd=_envf("POLYMARKET_BREGMAN_TARGET_CAPITAL_USD", 50.0),
            bregman_discovery_limit=_envi("POLYMARKET_BREGMAN_DISCOVERY_LIMIT", 1000),
            bregman_near_miss_store_cap=_envi("POLYMARKET_BREGMAN_NEAR_MISS_STORE_CAP", 1000),
            bregman_top_near_misses=_envi("POLYMARKET_BREGMAN_TOP_NEAR_MISSES", 10),
            bregman_max_bundles_per_tick=_envi("POLYMARKET_BREGMAN_MAX_BUNDLES_PER_TICK", 3),
            bregman_max_open_bundles=_envi("POLYMARKET_BREGMAN_MAX_OPEN_BUNDLES", 10),
            bregman_max_capital_per_tick_usd=_envf("POLYMARKET_BREGMAN_MAX_CAPITAL_PER_TICK", 100.0),
            bregman_min_roi=_envf("POLYMARKET_BREGMAN_MIN_ROI", 0.002),
            bregman_priority_enabled=_envb("POLYMARKET_BREGMAN_PRIORITY_ENABLED", True),
            bregman_reserve_open_slots=_envi("POLYMARKET_BREGMAN_RESERVE_OPEN_SLOTS", 3),
            bregman_reserve_capital_usd=_envf("POLYMARKET_BREGMAN_RESERVE_CAPITAL_USD", 100.0),
            directional_can_use_unused_bregman_slots=_envb(
                "POLYMARKET_DIRECTIONAL_CAN_USE_UNUSED_BREGMAN_SLOTS", True),
            directional_can_use_unused_bregman_capital=_envb(
                "POLYMARKET_DIRECTIONAL_CAN_USE_UNUSED_BREGMAN_CAPITAL", True),
            block_directional_on_bregman_markets=_envb(
                "POLYMARKET_BLOCK_DIRECTIONAL_ON_BREGMAN_MARKETS", True),
            block_directional_on_bregman_events=_envb(
                "POLYMARKET_BLOCK_DIRECTIONAL_ON_BREGMAN_EVENTS", True),
            exploration_can_use_bregman_reserved_capacity=_envb(
                "POLYMARKET_EXPLORATION_CAN_USE_BREGMAN_RESERVED_CAPACITY", False),
            profitability_first=_envb("POLYMARKET_PROFITABILITY_FIRST", True),
            require_profitability_annotation=_envb(
                "POLYMARKET_REQUIRE_PROFITABILITY_ANNOTATION", True),
            min_after_cost_edge=_envf("POLYMARKET_MIN_AFTER_COST_EDGE", 0.01),
            min_after_cost_roi=_envf("POLYMARKET_MIN_AFTER_COST_ROI", 0.002),
            min_expected_value_usd=_envf("POLYMARKET_MIN_EXPECTED_VALUE_USD", 0.01),
            profitability_sort_weight=_envf("POLYMARKET_PROFITABILITY_SORT_WEIGHT", 1.0),
            model_score_sort_weight=_envf("POLYMARKET_MODEL_SCORE_SORT_WEIGHT", 0.35),
            liquidity_sort_weight=_envf("POLYMARKET_LIQUIDITY_SORT_WEIGHT", 0.25),
            freshness_sort_weight=_envf("POLYMARKET_FRESHNESS_SORT_WEIGHT", 0.25),
            ambiguity_penalty_sort_weight=_envf("POLYMARKET_AMBIGUITY_PENALTY_WEIGHT", 0.50),
            execution_drag_penalty_weight=_envf("POLYMARKET_EXECUTION_DRAG_PENALTY_WEIGHT", 1.0),
            bregman_profitability_first=_envb("POLYMARKET_BREGMAN_PROFITABILITY_FIRST", True),
            bregman_min_after_cost_profit_usd=_envf(
                "POLYMARKET_BREGMAN_MIN_AFTER_COST_PROFIT_USD", 0.02),
            bregman_min_after_cost_roi=_envf("POLYMARKET_BREGMAN_MIN_AFTER_COST_ROI", 0.002),
            bregman_profit_sort_weight=_envf("POLYMARKET_BREGMAN_PROFIT_SORT_WEIGHT", 1.0),
            bregman_risk_penalty_weight=_envf("POLYMARKET_BREGMAN_RISK_PENALTY_WEIGHT", 0.5),
            max_event_exposure_usd=_envf("POLYMARKET_MAX_EVENT_EXPOSURE_USD", 20.0),
            max_category_exposure_usd=_envf("POLYMARKET_MAX_CATEGORY_EXPOSURE_USD", 40.0),
            max_bregman_bundle_exposure_usd=_envf("POLYMARKET_MAX_BREGMAN_BUNDLE_EXPOSURE_USD", 30.0),
            diversity_target=_envi("POLYMARKET_DIVERSITY_TARGET", 5),
            exploration_budget_usd=_envf("POLYMARKET_EXPLORATION_BUDGET_USD", 20.0),
            max_drawdown_usd=_envf("POLYMARKET_MAX_DRAWDOWN_USD", 50.0),
            cvar_alpha=_envf("POLYMARKET_CVAR_ALPHA", 0.95),
            kelly_max_fraction=_envf("POLYMARKET_KELLY_MAX_FRACTION", 0.05),
            leg_failure_haircut=_envf("POLYMARKET_LEG_FAILURE_HAIRCUT", 0.5),
            capital_allocation_enabled=_envb("POLYMARKET_CAPITAL_ALLOCATION_ENABLED", True),
            capital_min_after_cost_edge=_envf("POLYMARKET_CAPITAL_MIN_AFTER_COST_EDGE", 0.0),
            max_correlated_cluster_exposure_usd=_envf(
                "POLYMARKET_MAX_CORRELATED_CLUSTER_EXPOSURE_USD", 40.0),
            max_strategy_exposure_usd=_envf("POLYMARKET_MAX_STRATEGY_EXPOSURE_USD", 40.0),
            max_open_capital_lock_usd=_envf("POLYMARKET_MAX_OPEN_CAPITAL_LOCK_USD", 100.0),
            dd_governor_max_loss_streak=_envi("POLYMARKET_DD_GOVERNOR_MAX_LOSS_STREAK", 5),
            dd_governor_pause_loss_streak=_envi("POLYMARKET_DD_GOVERNOR_PAUSE_LOSS_STREAK", 10),
            dd_governor_soft_fraction=_envf("POLYMARKET_DD_GOVERNOR_SOFT_FRACTION", 0.5),
            dd_governor_calibration_limit=_envf("POLYMARKET_DD_GOVERNOR_CALIBRATION_LIMIT", 0.15),
            dd_governor_execution_floor=_envf("POLYMARKET_DD_GOVERNOR_EXECUTION_FLOOR", 0.5),
            chainlink_freshness_penalty_weight=_envf("POLYMARKET_CHAINLINK_FRESHNESS_PENALTY", 0.5),
            settlement_ambiguity_penalty_weight=_envf("POLYMARKET_AMBIGUITY_PENALTY", 0.5),
            research_high_confidence=_envf("POLYMARKET_RESEARCH_HIGH_CONFIDENCE", 0.8),
            research_confident_ambiguity_frac=_envf("POLYMARKET_RESEARCH_CONFIDENT_AMBIGUITY_FRAC", 0.6),
            feature_extraction_enabled=_envb("POLYMARKET_FEATURE_EXTRACTION_ENABLED", True),
            grouping_enabled=_envb("POLYMARKET_GROUPING_ENABLED", True),
            paper_decision_budget=_envi("POLYMARKET_DECISION_BUDGET",
                                        _envi("POLYMARKET_PAPER_DECISION_BUDGET", 30)),
            feedback_sample_target=_envi("POLYMARKET_FEEDBACK_SAMPLE_TARGET", 200),
            tiny_trade_min_liquidity=_envf("POLYMARKET_TINY_TRADE_MIN_LIQUIDITY", 100.0),
            walk_forward_enabled=_envb("POLYMARKET_WALK_FORWARD_ENABLED", False),
            walk_forward_train=_envi("POLYMARKET_WALK_FORWARD_TRAIN", 6),
            walk_forward_test=_envi("POLYMARKET_WALK_FORWARD_TEST", 3),
            oos_degrade_tolerance=_envf("POLYMARKET_OOS_DEGRADE_TOLERANCE", 0.2),
            min_param_stability=_envf("POLYMARKET_MIN_PARAM_STABILITY", 0.5),
            max_overfit_penalty=_envf("POLYMARKET_MAX_OVERFIT_PENALTY", 0.5),
            overfit_rollback_tolerance=_envf("POLYMARKET_OVERFIT_ROLLBACK_TOLERANCE", 0.05),
            aggressive_can_promote_params=_envb("POLYMARKET_AGGRESSIVE_CAN_PROMOTE_PARAMS", False),
            campaign_enabled=_envb("POLYMARKET_CAMPAIGN_ENABLED", False),
            campaign_name=os.getenv("POLYMARKET_CAMPAIGN_NAME", "institutional_paper_campaign"),
            algorithm_freeze_mode=_envb("POLYMARKET_ALGORITHM_FREEZE_MODE", False),
            campaign_start_ts=os.getenv("POLYMARKET_CAMPAIGN_START_TS") or None,
            campaign_target_min_days=_envi("POLYMARKET_CAMPAIGN_TARGET_MIN_DAYS", 14),
            campaign_target_min_decisions=_envi("POLYMARKET_CAMPAIGN_TARGET_MIN_DECISIONS", 1000),
            campaign_target_min_paper_trades=_envi(
                "POLYMARKET_CAMPAIGN_TARGET_MIN_PAPER_TRADES", 300),
            campaign_target_min_resolved_labels=_envi(
                "POLYMARKET_CAMPAIGN_TARGET_MIN_RESOLVED_LABELS", 100),
            campaign_target_min_bregman_candidates=_envi(
                "POLYMARKET_CAMPAIGN_TARGET_MIN_BREGMAN_CANDIDATES", 50),
            campaign_max_bregman_false_positives=_envi(
                "POLYMARKET_CAMPAIGN_MAX_BREGMAN_FALSE_POSITIVES", 0),
            campaign_safe_profile=_envb("POLYMARKET_CAMPAIGN_SAFE_PROFILE", False),
            clob_read_only=_envb("POLYMARKET_CLOB_READ_ONLY", True),
            chainlink_read_only=_envb("CHAINLINK_READ_ONLY", True),
            realistic_fill_enabled=_envb("POLYMARKET_REALISTIC_FILL_ENABLED", False),
            clean_label_guard=_envb("POLYMARKET_CLEAN_LABEL_GUARD", True),
            risk_engine_enabled=_envb("POLYMARKET_RISK_ENGINE_ENABLED", True),
            news_scanner_enabled=_envb("NEWS_SCANNER_ENABLED", False),
            news_provider_mode=(os.getenv("NEWS_PROVIDER_MODE") or "offline_cache").strip().lower(),
            news_live_read_only=_envb("NEWS_LIVE_READ_ONLY", True),
            news_max_queries_per_market=_envi("NEWS_MAX_QUERIES_PER_MARKET", 3),
            news_max_items_per_market=_envi("NEWS_MAX_ITEMS_PER_MARKET", 8),
            news_max_snippet_chars=_envi("NEWS_MAX_SNIPPET_CHARS", 500),
            news_cache_ttl_seconds=_envi("NEWS_CACHE_TTL_SECONDS", 1800),
            news_min_relevance_score=_envf("NEWS_MIN_RELEVANCE_SCORE", 0.2),
            news_min_source_credibility=_envf("NEWS_MIN_SOURCE_CREDIBILITY", 0.4),
            news_enable_grok_packet=_envb("NEWS_ENABLE_GROK_PACKET", True),
            news_replay_timestamp_safe=_envb("NEWS_REPLAY_TIMESTAMP_SAFE", True),
            btc_pulse_enabled=_envb("BTC_PULSE_ENABLED", False),
            btc_pulse_paper_only=_envb("BTC_PULSE_PAPER_ONLY", True),
            btc_pulse_isolated_learning=_envb("BTC_PULSE_ISOLATED_LEARNING", True),
            btc_pulse_allow_transfer_learning=_envb("BTC_PULSE_ALLOW_TRANSFER_LEARNING", False),
            btc_pulse_live_enabled=_envb("BTC_PULSE_LIVE_ENABLED", False),
            btc_pulse_legacy_autotrade_enabled=_envb("BTC_AUTOTRADE_ENABLED", False),
            btc_pulse_tick_seconds=_envi("BTC_PULSE_TICK_SECONDS", 30),
            btc_pulse_round_seconds=_envi("BTC_PULSE_ROUND_SECONDS", 300),
            btc_pulse_max_paper_notional_per_trade=_envf(
                "BTC_PULSE_MAX_PAPER_NOTIONAL_PER_TRADE", 5.0),
            btc_pulse_max_paper_trades_per_hour=_envi("BTC_PULSE_MAX_PAPER_TRADES_PER_HOUR", 60),
            btc_pulse_max_daily_paper_loss=_envf("BTC_PULSE_MAX_DAILY_PAPER_LOSS", 50.0),
            btc_pulse_min_ev_threshold=_envf("BTC_PULSE_MIN_EV_THRESHOLD", 0.0),
            btc_pulse_require_positive_ev=_envb("BTC_PULSE_REQUIRE_POSITIVE_EV", True),
            btc_pulse_shadow_gate_enabled=_envb("BTC_PULSE_SHADOW_GATE_ENABLED", True),
            btc_pulse_require_risk_gate=_envb("BTC_PULSE_REQUIRE_RISK_GATE", True),
            btc_pulse_require_realistic_fill=_envb("BTC_PULSE_REQUIRE_REALISTIC_FILL", True),
            btc_pulse_require_chainlink=_envb("BTC_PULSE_REQUIRE_CHAINLINK", False),
            btc_pulse_chainlink_heartbeat_seconds=_envi("CHAINLINK_BTC_USD_HEARTBEAT_SECONDS", 120),
            btc_pulse_chainlink_max_age_seconds=_envi("CHAINLINK_BTC_USD_MAX_AGE_SECONDS", 180),
            btc_pulse_oracle_debug_log=_envb("BTC_PULSE_ORACLE_DEBUG_LOG", False),
            btc_fast_price_enabled=_envb("BTC_FAST_PRICE_ENABLED", False),
            btc_fast_price_provider=(os.getenv("BTC_FAST_PRICE_PROVIDER")
                                     or "coinbase_readonly").strip(),
            btc_fast_price_symbol=(os.getenv("BTC_FAST_PRICE_SYMBOL") or "BTC-USD").strip(),
            btc_fast_price_max_age_seconds=_envi("BTC_FAST_PRICE_MAX_AGE_SECONDS", 10),
            btc_fast_price_timeout_seconds=_envf("BTC_FAST_PRICE_TIMEOUT_SECONDS", 5.0),
            btc_fast_price_max_retries=_envi("BTC_FAST_PRICE_MAX_RETRIES", 2),
            btc_fast_price_log_enabled=_envb("BTC_FAST_PRICE_LOG_ENABLED", False),
            btc_pulse_require_fast_price=_envb("BTC_PULSE_REQUIRE_FAST_PRICE", False),
            btc_pulse_max_oracle_disagreement_bps=_envf("BTC_PULSE_MAX_ORACLE_DISAGREEMENT_BPS", 50.0),
            btc_pulse_block_chop_regime=_envb("BTC_PULSE_BLOCK_CHOP_REGIME", False),
            btc_pulse_min_fill_realism_score=_envf("BTC_PULSE_MIN_FILL_REALISM_SCORE", 0.0),
            news_advisory_enabled=_envb("NEWS_ADVISORY_ENABLED", True),
            news_trade_gate_enabled=_envb("NEWS_TRADE_GATE_ENABLED", False),
            news_require_published_at=_envb("NEWS_REQUIRE_PUBLISHED_AT", False),
            news_reject_unclear_date=_envb("NEWS_REJECT_UNCLEAR_DATE", False),
            news_max_age_hours=_envf("NEWS_MAX_AGE_HOURS", 0.0),
            feedback_accelerator_enabled=_envb("FEEDBACK_ACCELERATOR_ENABLED", False),
            feedback_accelerator_target_multiplier=_envi("FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER", 10),
            exploration_tiny_size_enabled=_envb("EXPLORATION_TINY_SIZE_ENABLED", True),
            exploration_counts_for_readiness=_envb("EXPLORATION_COUNTS_FOR_READINESS", False),
            shadow_decision_logging_enabled=_envb("SHADOW_DECISION_LOGGING_ENABLED", True),
            no_trade_labeling_enabled=_envb("NO_TRADE_LABELING_ENABLED", True),
            btc_pulse_feedback_acceleration_enabled=_envb(
                "BTC_PULSE_FEEDBACK_ACCELERATION_ENABLED", True),
            polymarket_feedback_acceleration_enabled=_envb(
                "POLYMARKET_FEEDBACK_ACCELERATION_ENABLED", True),
            experiments_enabled=_envb("POLYMARKET_EXPERIMENTS_ENABLED", False),
            experiment_id=(os.getenv("POLYMARKET_EXPERIMENT_ID") or "exp_default").strip(),
            bregman_first_budget=_envb("POLYMARKET_BREGMAN_FIRST_BUDGET", True),
            kill_switch_enabled=_envb("POLYMARKET_KILL_SWITCH_ENABLED", True),
            kill_switch_auto_downgrade=_envb("POLYMARKET_KILL_SWITCH_AUTO_DOWNGRADE", True),
            ks_max_calibration_error=_envf("POLYMARKET_KS_MAX_CALIBRATION_ERROR", 0.20),
            ks_max_brier_trend=_envf("POLYMARKET_KS_MAX_BRIER_TREND", 0.05),
            ks_max_loss_streak=_envi("POLYMARKET_KS_MAX_LOSS_STREAK", 10),
            ks_max_label_suppression_rate=_envf("POLYMARKET_KS_MAX_LABEL_SUPPRESSION_RATE", 0.5),
            ks_max_ambiguous_rate=_envf("POLYMARKET_KS_MAX_AMBIGUOUS_RATE", 0.5),
            ks_max_stale_rejection_rate=_envf("POLYMARKET_KS_MAX_STALE_REJECTION_RATE", 0.5),
            ks_max_partial_fill_rate=_envf("POLYMARKET_KS_MAX_PARTIAL_FILL_RATE", 0.5),
            ks_max_bregman_fp_rate=_envf("POLYMARKET_KS_MAX_BREGMAN_FP_RATE", 0.10),
            ks_max_avg_spread=_envf("POLYMARKET_KS_MAX_AVG_SPREAD", 0.15),
            ks_max_learner_rollbacks=_envi("POLYMARKET_KS_MAX_LEARNER_ROLLBACKS", 3),
            ks_min_samples=_envi("POLYMARKET_KS_MIN_SAMPLES", 10),
            readiness_min_eval_samples=_envi("POLYMARKET_READINESS_MIN_EVAL_SAMPLES", 30),
            readiness_min_qualified_samples=_envi("POLYMARKET_READINESS_MIN_QUALIFIED_SAMPLES", 200),
            readiness_min_canary_samples=_envi("POLYMARKET_READINESS_MIN_CANARY_SAMPLES", 500),
            readiness_min_canary_full_samples=_envi("POLYMARKET_READINESS_MIN_CANARY_FULL_SAMPLES", 1000),
            readiness_max_drawdown_pct=_envf("POLYMARKET_READINESS_MAX_DRAWDOWN_PCT", 0.15),
            live_micro_canary_notional_usd=_envf("POLYMARKET_LIVE_MICRO_CANARY_NOTIONAL_USD", 5.0),
            live_canary_notional_usd=_envf("POLYMARKET_LIVE_CANARY_NOTIONAL_USD", 25.0),
            live_max_daily_loss_usd=_envf("POLYMARKET_LIVE_MAX_DAILY_LOSS_USD", 10.0),
            live_max_per_market_usd=_envf("POLYMARKET_LIVE_MAX_PER_MARKET_USD", 5.0),
            live_max_event_usd=_envf("POLYMARKET_LIVE_MAX_EVENT_USD", 5.0),
            signal_model=(os.getenv("POLYMARKET_TRAINING_SIGNAL_MODEL") or "research").lower(),
            starting_bankroll=_envf("HTE_STARTING_BALANCE", 500.0),
            universe=ucfg,
        )

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "universe"}
        d["markout_horizons_s"] = list(self.markout_horizons_s)
        d["universe"] = self.universe.as_dict() if self.universe else None
        return d

    @classmethod
    def aggressive_paper(cls, **overrides) -> "TrainingConfig":
        """Explicit AGGRESSIVE paper-training profile (PAPER ONLY).

        Turns on every available *non-live* learning feature and trades more
        often to generate more feedback-loop data:

        * wider market scan + larger shortlist/live-watch/trade-candidate limits,
        * lower minimum net edge + uncertainty (more candidates clear the gate),
        * higher (but still clamped) shrink so fair-value moves off the market,
        * wider spread tolerance + thinner depth floor (more eligible markets),
        * faster scan / subscription refresh / feedback cadence,
        * online learner + recursive feedback loop + Chainlink feature layer on,
        * bounded **controlled exploration** of near-miss candidates at a small
          exploratory size for extra training signal.

        SAFETY: ``mode='paper_train'`` (never live); hard paper risk caps
        (order/market/total notional, daily loss, max-open hard cap) are applied
        by ``__post_init__`` and CANNOT be exceeded by this profile. No live
        flags are touched.
        """
        base = dict(
            mode="paper_train", polymarket_only=True, disable_btc_pulse_trading=True,
            # wide scan + fast refresh
            scan_limit=2000, scan_interval_seconds=15.0, metadata_cache_ttl_s=30.0,
            shortlist_limit=200, live_watch_limit=120, trade_candidate_limit=60,
            subscription_refresh_s=30.0, max_subscription_churn=40,
            # lower edge thresholds -> more paper trades
            min_net_edge=0.005, base_uncertainty=0.005,
            base_shrink_factor=0.45, max_shrink_factor=0.80, min_shrink_factor=0.10,
            max_spread=0.12, min_depth_at_price=25.0,
            # looser eligibility (still filtered)
            min_evidence_score=0.30, min_liquidity=100.0, min_volume=250.0,
            max_time_to_close_days=180.0, min_time_to_close_s=1800.0,
            max_ambiguity_score=0.45,
            # smaller exploratory size; max concurrency at the hard cap
            fixed_notional_usd=3.0, max_open_trades=8, max_open_trades_hard_cap=8,
            # every non-live learning feature ON
            learner_enabled=True, feedback_enabled=True,
            feedback_interval_seconds=60.0, chainlink_enabled=True,
            feature_extraction_enabled=True, grouping_enabled=True,
            exploration_enabled=True, exploration_rate=0.25,
            exploration_notional_usd=2.0, exploration_min_edge=-0.01,
            # active learning ON: fill idle paper budget with highest-feedback-value
            # near-misses, balanced exploration/exploitation, diversified coverage.
            active_learning_enabled=True, exploration_split=0.5,
            category_sample_target=100, max_explore_per_category=4, max_explore_per_event=2,
            # higher paper decision budget + feedback target -> more trades/feedback
            paper_decision_budget=120, feedback_sample_target=500,
            tiny_trade_min_liquidity=50.0,
            signal_model="research",
            # aggressive = trade MORE OFTEN, not recklessly: smaller paper sizes,
            # more diversified candidates, TIGHTER per-event/category/bundle hard
            # caps, and an explicit exploration budget.
            max_event_exposure_usd=8.0, max_category_exposure_usd=20.0,
            max_bregman_bundle_exposure_usd=15.0, diversity_target=8,
            exploration_budget_usd=15.0, max_drawdown_usd=40.0,
            kelly_max_fraction=0.03, leg_failure_haircut=0.6,
            # anti-overfitting: walk-forward governance ON, but aggressive mode
            # CANNOT promote production-like params until walk-forward validation
            # passes (it may still learn fast online and roll back on degrade).
            walk_forward_enabled=True, aggressive_can_promote_params=False,
            # controlled strategy-variant experiments ON: spread the wider paper
            # budget across variants (Bregman first) instead of one blended policy.
            experiments_enabled=True, experiment_id="aggressive_experiment",
        )
        base.update(overrides)
        return cls(**base)

    @classmethod
    def institutional_campaign_defaults(cls, **overrides) -> "TrainingConfig":
        """Campaign-safe institutional profile (PAPER ONLY; fail-closed).

        Builds on :meth:`aggressive_paper` and turns on EVERY read-only learning +
        realism feature for the multi-day institutional campaign while keeping
        every live-money path disabled:

        ENABLED: aggressive paper learning, campaign mode, algorithm freeze
        (no parameter promotion), read-only Polymarket CLOB v2 feed, read-only
        Chainlink scanner/features, realistic-fill simulation (slippage + depth,
        NO fantasy reference-price fills, stale books rejected), the clean-label
        guard (only clean settled labels train), a mandatory RiskEngine, and
        Bregman certification monitoring + campaign evidence collection.

        DISABLED: live trading, micro-live, guarded-live, real order submission,
        wallet mutation, the legacy BTC pulse autotrade, and legacy cross-exchange
        arbitrage. ``__post_init__`` re-asserts every safety invariant so these
        can never be flipped off by an override. NOT a global production default.
        """
        base = dict(
            campaign_safe_profile=True,
            campaign_enabled=True,
            campaign_name="institutional_paper_campaign",
            algorithm_freeze_mode=True,
            # read-only realism features ON
            clob_enabled=True, clob_read_only=True, subscribe_trending=True,
            chainlink_enabled=True, chainlink_read_only=True,
            realistic_fill_enabled=True,
            allow_pm_reference_price_fills=False, reject_on_stale_book=True,
            # mandatory guards ON
            clean_label_guard=True, risk_engine_enabled=True,
            # legacy / live paths OFF
            disable_btc_pulse_trading=True,
        )
        base.update(overrides)
        return cls.aggressive_paper(**base)


def AggressivePaperTrainingConfig(**overrides) -> "TrainingConfig":
    """Factory for the aggressive PAPER-only training profile. See
    :meth:`TrainingConfig.aggressive_paper`."""
    return TrainingConfig.aggressive_paper(**overrides)
