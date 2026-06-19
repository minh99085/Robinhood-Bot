# Hermes Polymarket Paper-Training — Bot Inspection Report

_Generated: 2026-06-19T04:46:04.104595+00:00 · PAPER ONLY · inspection/reporting only_

## 0. Algorithmic Edge Audit (MANDATORY)

**Audit status: PASS** (`complete`; stale=False)

- Bregman edge engine enabled: **True**
- Readiness cap: **100** (raw 79.0 → capped **79.0**)

### 1. Strategy Attribution

| Field | Value |
|---|---|
| trades_by_strategy | None |
| gross_pnl | 0.0 |
| after_cost_pnl | 0.0 |
| win_rate | 0.0 |
| win_rate_sample_count | 0 |
| avg_edge_at_entry | None |
| avg_realized_edge | None |
| rejected_trades | None |
| open_exposure | 1.0 |
| realized_pnl | 0.0 |
| unrealized_pnl | 0.0 |

### 2. Bregman Arbitrage Diagnostics

| Field | Value |
|---|---|
| constraint_groups_scanned | 497.0 |
| raw_groups_discovered | 497.0 |
| incoherent_groups | 0.0 |
| candidate_arbitrages | 0.0 |
| certified_arbitrages | 0.0 |
| executable_depth_certified | 0.0 |
| rejected_fees_spread_depth_slippage | 1977 |
| expected_min_profit | 0.0 |
| worst_case_payoff | 0.0 |
| execution_atomicity_risk | False |
| opportunity_decay_s | 300.0 |
| canonical_source | metrics/bregman_funnel.json |

### 3. BTC Pulse Diagnostics

| Field | Value |
|---|---|
| chainlink_anchor_price | 62487.667 |
| fast_btc_price | 62530.005 |
| feed_disagreement_bps | None |
| market_stale_time_s | 556.044 |
| volatility_regime | None |
| trend_persistence | None |
| trade_trigger_reason | None |
| rejected_trigger_reason | None |
| after_cost_expectancy | None |

### 4. Calibration Diagnostics

| Field | Value |
|---|---|
| brier | 0.0 |
| ece | 0.0 |
| calibration_drift | None |
| isotonic_logistic_status | None |
| probability_rollback_status | None |
| confidence_bucket_performance | None |
| no_trade_bucket_performance | None |

### 5. Fill Realism

| Field | Value |
|---|---|
| fantasy_fills_rejected | 0.0 |
| spread_paid | None |
| estimated_slippage | None |
| partial_fill_assumptions | None |
| available_depth_at_decision | None |
| fee_adjusted_ev | None |
| clob_v2_executable | True |
| fill_realism_rejection_rate | 0.0 |

### 6. Risk Metrics

| Field | Value |
|---|---|
| sharpe | None |
| sortino | None |
| calmar | None |
| max_drawdown | 0.0 |
| exposure_by_market | None |
| exposure_by_event | None |
| exposure_by_strategy | None |
| cvar | None |
| kelly_fraction | None |
| risk_throttles_activated | None |
| kill_switch_triggers | None |

### 7. Training / Readiness

| Field | Value |
|---|---|
| exploration_pnl | None |
| validation_pnl | None |
| paper_only | True |
| production_readiness_score | 79.0 |
| production_ready | None |
| raw_readiness_score | 79.0 |
| readiness_cap | 100 |

### Top 5 Algorithmic Blockers

- None detected.

### Top 5 Next Recommended Code Changes

- None.

## 0b. Validation Contract (proves improvement, not completion)

**Contract: PASS** | **Production ready: False**

| Condition | Pass | Detail |
|---|---|---|
| pytest_green | OK | tests_passing=True |
| bregman_paper_enabled | OK | bregman_enabled=True |
| groups_scanned_positive | OK | constraint_groups_scanned=497.0 |
| fill_realism_enabled | OK | fill_realism_enabled=True |
| ledger_reconciled | OK | reconciliation_ok=True |
| after_cost_pnl_populated | OK | after_cost_pnl=0 |
| btc_pulse_gated_when_negative | OK | btc_after_cost=None gate_enabled=False |

- After-cost expectancy bootstrap: point=None CI=[None, None] credible_positive=**False** (n=0)
- Readiness blockers: `no_credible_positive_after_cost_expectancy`
> Production readiness is withheld unless an executable strategy shows statistically credible positive after-cost expectancy under a passing contract.

## 1. Executive Summary

**Classification: FAIL_NOT_RUN_READY**

- Bot health score: **79.0/100**
- Safety: WARN · live_detected=no
- Paper training running: yes · runtime: 21.25 min
- Tests: present=yes passing=yes
- Trend vs baseline: no baseline provided (current-state scorecard only)
- Missing/weak features: 0

## 2. Bot Health Scorecard

| Component | Score | Max | Why |
|---|---:|---:|---|
| safety | 15.0 | 25 | safety audit = WARN |
| tests | 15.0 | 15 | tests present and passing |
| runtime | 15.0 | 15 | paper-training status collected |
| feature_completeness | 15.0 | 20 | 6/8 key features active |
| performance_trend | 9.0 | 15 | no baseline (neutral) |
| observability | 10.0 | 10 | 3/3 observability sources present |
| **Total** | **79.0** | **100** | |

## 3. Safety / Live-Execution Audit

- Status: **WARN** · engine_mode=paper_train
- Forbidden live flags enabled: none
- Live credential material present: none

Findings:
- [INFO] `HTE_AUTOTRADE` = 1 — paper-simulation autotrade flag enabled (PAPER only — not a live failure).

## 4. Runtime Health

- Paper status collected: yes (source: runtime_data/polymarket_training.json)
- Docker available: yes
- preflight_ok: yes · scanned=2000 kept=1867

## 5. Performance Improvement / Regression Analysis

No baseline provided — current-state key metrics:

- equity: 500.0
- total_pnl: 0.0
- after_cost_pnl: 0
- closed_positions: 0
- paper_trades: 0
- win_rate_traded_only: None
- brier: 0.0
- ece: 0.0
- sharpe: None
- sortino: None
- calmar: None
- max_drawdown: 0.0
- btc_pulse_after_cost_pnl: None
- bregman_certified_profit: 0.0
- news_quality_ratio: 0.4217

## 6. Chainlink / Oracle Health

- chainlink_enabled: yes
- chainlink_valid: yes
- chainlink_stale: no
- chainlink_age_seconds: 556.044
- chainlink_price: 62487.667
- chainlink_stale_reason: none

## 7. BTC Fast Price Feed Health

- btc_fast_price_enabled: yes
- btc_fast_price_valid: yes
- btc_fast_price_age_seconds: 0.0
- btc_fast_price_disagreement_bps: 6.775
- btc_fast_price_disabled_reason: none

## 8. BTC Pulse Status

- btc_pulse_enabled: no
- btc_pulse_frozen: yes
- btc_pulse_oracle_gate_active: unknown
- btc_pulse_paper_trades: unknown
- btc_pulse_after_cost_pnl: unknown
- btc_pulse_regime: unknown
- btc_pulse_rejection_reasons: unknown

## 9. News Scanner Quality

- news_scanner_enabled: yes
- news_provider_mode: live_read_only
- news_items_fetched: 460
- news_items_used: 194
- news_rejected_stale: unknown
- news_rejected_unclear_date: unknown
- news_rejected_low_credibility: unknown
- news_quality_ratio: 0.4217

## 10. Grok / Research Status

- grok_enabled: yes
- grok_has_api_key: <REDACTED>
- grok_with_news_count: 2
- grok_cache_hits: 0

### 10a. Grok Advisory Scheduler (research-only)

- grok_advisory_enabled: True
- grok_brain_ready: True
- grok_brain_blocker: None
- xai_api_key_source: <REDACTED>
- grok_calls_total: 2
- grok_calls_with_news: 2
- grok_proof_calls_total: 0
- grok_scheduler_calls_total: 2
- grok_total_calls_reconciled: True
- grok_scheduled_calls: 2
- grok_scheduler_eligible_targets: 523
- grok_scheduler_targets_selected: 2
- grok_scheduler_targets_skipped: 6
- grok_scheduler_skip_reasons: {'not_due_yet': 6}
- grok_advisory_only_count: 2
- grok_evidence_records_written: 2
- grok_advisory_max_calls_per_hour: 4
- grok_advisory_calls_per_hour: 2
- grok_market_groups_analyzed: 1
- grok_bregman_near_misses_analyzed: 1
- grok_bregman_incomplete_groups_analyzed: 1
- grok_bregman_malformed_groups_analyzed: 0
- grok_news_linked_markets_analyzed: 2
- grok_learning_features_written: 2
- grok_best_bregman_group_analyzed: True
- grok_best_bregman_group_skip_reason: None
- grok_contributed_learning_features: True
- grok_advisory_only_invariant: True
- grok_no_execution_override: True

## 11. Bregman Paper Scanner Status

- bregman_paper_enabled: yes
- bregman_candidates_found: 0
- bregman_certified_count: unknown
- bregman_certified_profit: 0.0
- bregman_false_positive_rate: 0.0

### 11.0 ABCAS Certifier Funnel Diagnostics (read-only)

- constraint_groups_scanned: 711
- candidate_arbitrages: 0
- certified_arbitrages: 0
- best_projected_profit_per_set: 0.0
- max_bregman_distance: 0.0
- mean_cost_per_set: 1.012952
- expected_min_profit: 0.0
- near_miss_count: 10
- stage_rejections: {'adapter_failed': 1533, 'certifier_no_positive_profit': 711, 'realism_fees_spread_depth': 0, 'other': 0}
  - near_miss(certifier_reached): legs=['<REDACTED>', '<REDACTED>'] D(mu*||theta)=0.0 projected_profit/set=0.0 cost/set=1.0 reason=no_positive_worst_case_profit tradeable=False
  - near_miss(certifier_reached): legs=['<REDACTED>', '<REDACTED>'] D(mu*||theta)=0.0 projected_profit/set=0.0 cost/set=1.0 reason=no_positive_worst_case_profit tradeable=False
  - near_miss(certifier_reached): legs=['<REDACTED>', '<REDACTED>'] D(mu*||theta)=0.0 projected_profit/set=0.0 cost/set=1.0 reason=no_positive_worst_case_profit tradeable=False
  - near_miss(certifier_reached): legs=['<REDACTED>', '<REDACTED>'] D(mu*||theta)=0.0 projected_profit/set=0.0 cost/set=1.0 reason=no_positive_worst_case_profit tradeable=False
  - near_miss(certifier_reached): legs=['<REDACTED>', '<REDACTED>'] D(mu*||theta)=0.0 projected_profit/set=0.0 cost/set=1.0 reason=no_positive_worst_case_profit tradeable=False

### 11a. Bregman Near-Miss Diagnostics (read-only)

- bregman_near_misses_total: 518
- near_miss_one_fix_away_count: 123
- near_miss_depth_only_count: 118
- near_miss_not_exhaustive_count: 252
- near_miss_stale_refresh_failed_count: 0
- near_miss_by_rejection_reason: {'depth_too_thin': 95, 'no_executable_price': 1, 'no_positive_edge': 155, 'not_exhaustive': 252, 'spread_too_wide': 3, 'stale_book': 12}
- near_miss_learning_priority_counts (high/med/low): {'high': 94, 'medium': 279, 'low': 145}
- near_miss_shadow_label_candidate_count: 94
- near_miss_learning_label_counts: {'needs_multiple_fixes': 269, 'no_positive_after_cost_edge': 126, 'would_certify_if_book_fresh': 3, 'would_certify_if_complete_set': 1, 'would_certify_if_depth_sufficient': 118, 'would_certify_if_spread_tightens': 1}
  - learn: binary:event:108639 priority=high(1.0) label=would_certify_if_depth_sufficient shadow_candidate=True would_trade_if=worst-leg depth $6.6642 reaches required $25.0 (thin legs=1)
  - learn: binary:event:46844 priority=high(1.0) label=would_certify_if_depth_sufficient shadow_candidate=True would_trade_if=worst-leg depth $13.4075 reaches required $25.0 (thin legs=1)
  - learn: binary:event:107711 priority=high(1.0) label=would_certify_if_depth_sufficient shadow_candidate=True would_trade_if=worst-leg depth $14.7565 reaches required $25.0 (thin legs=1)
  - learn: binary:event:279977 priority=high(1.0) label=would_certify_if_depth_sufficient shadow_candidate=True would_trade_if=worst-leg depth $5.0567 reaches required $25.0 (thin legs=1)
  - learn: binary:event:73196 priority=high(1.0) label=would_certify_if_depth_sufficient shadow_candidate=True would_trade_if=worst-leg depth $13.4237 reaches required $25.0 (thin legs=1)

### 11b. Bregman Price/Outcome Parsing + Depth Census (read-only)

- non_numeric_price_count: 0
- insufficient_outcomes_count: 0
- malformed_group_count: 316005
- parsed_price_success_rate: 1.0
- bregman_depth_sufficient_groups: 107
- bregman_depth_insufficient_groups: 390
- bregman_high_liquidity_groups_scanned: 69
- bregman_all_groups_thin: False
- complete_set_count (certified): 0
- incomplete_set_count (not_exhaustive near-misses): 252
- bregman_promising_groups_refreshed: 0
- bregman_refresh_success: 0 failed: 0 stale_after: 5
- refresh_not_attempted_reason: no_refresher_configured
- example[malformed_group]: market=<REDACTED> detail=0 usable outcomes in cluster
- no_bundle_blocker: incomplete_event_families (groups reached the certifier but every one was rejected by a STRICT gate (not loosened); dominant reason above)

Top Bregman near-misses (diagnostic only — NOT executed):

  - binary:event:261273 reason=no_positive_edge score=0.9 market_ids=['1559394'] token_ids=<REDACTED> '<REDACTED>'] labels=['YES', 'NO'] one_fix_away=False tradeable=False blockers=[]
  - binary:event:107726 reason=no_positive_edge score=0.9 market_ids=['956590'] token_ids=<REDACTED> '<REDACTED>'] labels=['YES', 'NO'] one_fix_away=False tradeable=False blockers=[]
  - binary:event:108634 reason=no_positive_edge score=0.9 market_ids=['958443'] token_ids=<REDACTED> '<REDACTED>'] labels=['YES', 'NO'] one_fix_away=False tradeable=False blockers=[]
  - binary:event:574642 reason=no_positive_edge score=0.9 market_ids=['2475212'] token_ids=<REDACTED> '<REDACTED>'] labels=['YES', 'NO'] one_fix_away=False tradeable=False blockers=[]
  - binary:event:211827 reason=no_positive_edge score=0.9 market_ids=['1385754'] token_ids=<REDACTED> '<REDACTED>'] labels=['YES', 'NO'] one_fix_away=False tradeable=False blockers=[]

### 11c. Bregman Certifier / Candidate Health (read-only)

- bregman_groups_entered_certifier: 497
- candidates_generated (certified): 0
- realistic_executable: 0
- bundles_opened: 0
- bregman_real_market_zero_candidate_reason: no_positive_after_cost_lower_bound_among_depth_sufficient_groups
- bregman_real_market_zero_candidate_reason_counts: {'depth_too_thin': 95, 'no_executable_price': 1, 'no_positive_edge': 130, 'not_exhaustive': 246, 'spread_too_wide': 3, 'stale_book': 22}
- bregman_depth_sufficient_groups: 107
- bregman_depth_sufficient_but_negative_edge_count: 106
- bregman_best_depth_sufficient_group_lower_bound: 0.534
- bregman_best_depth_sufficient_group_reject_reason: not_exhaustive
- best_real_group: event:event:551781 depth_sufficient=True min_leg_depth=$1389.2524 (required $25.0) reject=not_exhaustive lower_bound=0.534 market_ids=['2419341', '2419342', '2419348', '2419350', '2419352', '2419355'] labels=['YES', 'YES', 'YES', 'YES', 'YES', 'YES']
  - sample: group=event:event:551781 reason=not_exhaustive depth_sufficient=True market_ids=['2419341', '2419342', '2419348', '2419350', '2419352', '2419355'] token_ids=<REDACTED> '<REDACTED>', '<REDACTED>', '<REDACTED>', '<REDACTED>', '<REDACTED>'] labels=['YES', 'YES', 'YES', 'YES', 'YES', 'YES']
  - sample: group=event:event:548813 reason=not_exhaustive depth_sufficient=True market_ids=['2412401', '2412402', '2412403', '2412404'] token_ids=<REDACTED> '<REDACTED>', '<REDACTED>', '<REDACTED>'] labels=['YES', 'YES', 'YES', 'YES']
  - sample: group=binary:negrisk:<REDACTED> reason=depth_too_thin depth_sufficient=False market_ids=['2406025'] token_ids=<REDACTED> '<REDACTED>'] labels=['YES', 'NO']
- best_one_fix_away_reason: depth
- all_top_near_misses_negative_lower_bound: True

### 11d. Malformed-Group Reconciliation (summary vs tail)

- malformed_group_count (reconciled): 316005
- bregman_malformed_group_reported_count (trainer certifier): 0
- bregman_malformed_group_runtime_count (ABCAS scanner): 488
- bregman_malformed_group_tail_count (diagnostics tail): 316005
- bregman_malformed_group_legacy_or_tail_only_count: 315517
- source: abcas_scanner_path_real_rejects

### 11d-stage. Trainer Certifier Per-Stage Census (read-only)

- bregman_rejection_stage_counts: {'edge': 130, 'realism': 121, 'validate_simplex': 246}
- bregman_max_divergence_gap (D(mu*||theta)): 10.449921
- bregman_best_projected_lower_bound: 0.999
- bregman_positive_projected_but_rejected_count: 104
- bregman_positive_projected_rejected_by_stage: {'realism': 9, 'validate_simplex': 95}
- WHY certified=0: dominant stage=validate_simplex: groups are structurally INCOMPLETE (exhaustive=False / not a provable complete set) — buying a partial set is not a guaranteed hedge, so it is correctly NOT certified (completeness is never fabricated); NOTE: 104 group(s) had POSITIVE raw projected profit (best=0.999) but were still rejected (realism=9, validate_simplex=95) — the raw mispricing is real, but the set is not a certifiable complete hedge
- profit_lower_bound (min/mean/max): -14.601 / -0.176127 / 0.999
- groups by lower_bound sign (neg/zero/pos): 388 / 5 / 104
  - group: event:event:551781 exhaustive=False settlement_consistent=False profit_lower_bound=0.534 divergence_gap=0.047526 reason=not_exhaustive
  - group: event:event:548813 exhaustive=False settlement_consistent=False profit_lower_bound=-1.01 divergence_gap=0.2587 reason=not_exhaustive
  - group: binary:negrisk:<REDACTED> exhaustive=True settlement_consistent=True profit_lower_bound=-0.04 divergence_gap=0.0008 reason=depth_too_thin
  - group: event:event:36173 exhaustive=False settlement_consistent=False profit_lower_bound=0.938 divergence_gap=0.439922 reason=not_exhaustive
  - group: binary:event:609722 exhaustive=True settlement_consistent=True profit_lower_bound=-0.001 divergence_gap=5e-07 reason=stale_book
  - near_miss: binary:event:261273 stage=edge exhaustive=True settlement_consistent=True divergence_gap=5e-07 projected_lb=-0.001 reason=no_positive_edge
  - near_miss: binary:event:107726 stage=edge exhaustive=True settlement_consistent=True divergence_gap=5e-07 projected_lb=-0.001 reason=no_positive_edge
  - near_miss: binary:event:108634 stage=edge exhaustive=True settlement_consistent=True divergence_gap=5e-07 projected_lb=-0.001 reason=no_positive_edge

### 11e. Bregman Synthetic Fixture Proof (isolated, default gates)

- bregman_synthetic_fixture_passed: True
- synthetic_binary_candidate_generated: True
- synthetic_multiway_candidate_generated: True
- synthetic_invalid_cases_rejected: True
- synthetic_invalid_case_results: {'overpriced': True, 'thin_depth': True, 'duplicate_legs': True, 'stale_book': True}
- synthetic_fixture_gate_loosening: False
- synthetic_fixture_required_depth_usd: 50.0
- synthetic_fixture_live_trading_enabled: False
- synthetic_fixture_contaminated_real_metrics: False

### 11f. Profit-Discovery Learning (shadow labels + queue + bandit)

- bregman_shadow_label_candidates: 94
- bregman_shadow_labels_written: 174
- bregman_shadow_label_write_rate: 1.8511
- shadow_records_written: 27143
- shadow_labels_tail_nonempty: True
- shadow_label_write_rejection_reasons: {'already_written': 525}
- profit_discovery_queue_items: 518
- profit_discovery_queue_by_priority: {'2': 118, '3': 95, '5': 305}
- profit_learning_status: shadow_data_only
- profit_data_sufficiency: building
- bandit_router_enabled: True
- bandit_action_counts: {'bregman_depth_watchlist': 2, 'bregman_not_exhaustive_completer': 2, 'bregman_rebalancing_watchlist': 2, 'grok_news_linked_near_miss': 1, 'active_learning_shadow': 1}
- bandit_action_rewards: {'bregman_depth_watchlist': 6.0, 'bregman_not_exhaustive_completer': 6.0, 'bregman_rebalancing_watchlist': 6.0, 'grok_news_linked_near_miss': -1.0, 'active_learning_shadow': 2.0}
- bandit_selected_action: bregman_rebalancing_watchlist
- bandit_no_gate_override: True

### 11g. Targeted Market-Scan Prioritization (never a trade gate)

- targeted_market_scan_enabled: True
- targeted_markets_scanned_total: 1938
- targeted_scan_field_source: bregman_normalized_groups+raw_records
- targeted_scan_bregman_groups_seen: 518
- targeted_scan_binary_groups_seen: 263
- targeted_scan_yes_no_pairs_seen: 263
- targeted_scan_binary_group_matches: 254 raw_market_matches=1930
- targeted_scan_bregman_categories: {'complete_yes_no_tight_spread': 244, 'high_liquidity_binary': 130, 'negative_risk_complete': 3, 'complete_event_family': 3}
- targeted_scan_raw_market_categories: {'negative_risk_complete': 72, 'short_resolution': 1126, 'btc_eth_chainlink': 145, 'fed_macro_reference': 27, 'high_volume_news_linked': 1, 'complete_event_family': 72}
- targeted_scan_normalized_reject_reasons: {'not_exhaustive': 252, 'depth_too_thin': 95, 'no_positive_edge': 155, 'stale_book': 12, 'no_executable_price': 1, 'spread_too_wide': 3}
- bregman_clob_hydration_enabled: True
- bregman_clob_hydration_attempted: 250 success=234 failed=16
- bregman_real_yes_no_books_seen: 487
- bregman_certifier_used_real_clob_books: True
- bregman_synthetic_no_diagnostic_only_count: 8
- bregman_hydration_failure_reasons: {'no_book_or_no_ask': 22}
- bregman_clob_hydration_eligible_groups: 497 selected=250 coverage_rate=0.503
- paper_trade_pressure_enabled: True
- paper_micro_exploration_enabled: True
- paper_micro_exploration_candidates: 6 trades=0
- hydrated_positive_after_cost_candidates: 0
- realistic_trade_goal_met_11h: False
- paper_micro_exploration_reject_reasons: {'bregman_leg_stale_book': 6}
- zero_trade_blocker_if_any: positive_candidates_found_but_unfilled: positive=6; dominant_reject_reason=negative_after_cost_edge; reject_reasons={'stale_book': 833, 'negative_after_cost_edge': 928, 'depth_insufficient_for_1usd': 119, 'bregman_leg_stale_book': 6}; best_reject={'group_id': 'binary:event:149812', 'group_type': 'binary_yes_no', 'after_cost_edge': -0.003502, 'reject_reason': 'negative_after_cost_edge', 'depth_for_1usd': 1.0278, 'n_legs': 2}
- paper_relaxed_exploration_enabled: True (max_notional=1.0 per_hour=3 per_day=30)
- paper_relaxed_candidates_seen: 6 trades_opened=0
- paper_relaxed_after_cost_positive_seen: 6 real_clob_book_seen=1886
- paper_relaxed_readiness_pnl_excluded: True
- paper_relaxed_reject_reasons: {'bregman_leg_stale_book': 6}
- paper_relaxed_pipeline_scanned: 3954
- paper_relaxed_real_book_candidates_seen: 1886 positive=6
- paper_relaxed_candidate_source_counts: {'mutually_exclusive': 16, 'binary_yes_no': 1870}
- paper_relaxed_candidates_blocked_by_reason: {'stale_book': 833, 'negative_after_cost_edge': 928, 'depth_insufficient_for_1usd': 119}
- paper_relaxed_best_real_book_candidate: {'group_id': 'binary:event:603494', 'group_type': 'binary_yes_no', 'after_cost_edge': 0.25815, 'reject_reason': '', 'depth_for_1usd': 3.36, 'n_legs': 2}
- paper_relaxed_best_reject_example: {'group_id': 'binary:event:149812', 'group_type': 'binary_yes_no', 'after_cost_edge': -0.003502, 'reject_reason': 'negative_after_cost_edge', 'depth_for_1usd': 1.0278, 'n_legs': 2}
- bregman_false_incomplete_family_count: 0 near_miss_promoted=0
- bregman_incomplete_family_examples: [{'group_id': 'event:event:551781', 'n_legs_scanned': 6, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:548813', 'n_legs_scanned': 4, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:36173', 'n_legs_scanned': 2, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:591091', 'n_legs_scanned': 2, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:386812', 'n_legs_scanned': 2, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:386788', 'n_legs_scanned': 2, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:34051', 'n_legs_scanned': 2, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}, {'group_id': 'event:event:255195', 'n_legs_scanned': 2, 'declared_outcome_count': None, 'has_complete_marker': False, 'missing_outcome_count': None}]
- bregman_missing_outcome_examples: [{'group_id': 'event:event:548786', 'present_outcomes_sample': ['Will Donald Trump publicly insult someone on June 16, 2026?', 'Will Donald Trump publicly insult someone on June 19, 2026?', 'Will Donald Trump publicly insult someone on June 2, 2026?', 'Will Donald Trump publicly insult someone on June 29, 2026?', 'Will Donald Trump publicly insult someone on June 3, 2026?', 'Will Donald Trump publicly insult someone on June 4, 2026?'], 'missing_outcome_count': 19}, {'group_id': 'event:event:577376', 'present_outcomes_sample': ['Will Iran close its airspace by June 30?', 'Will Iran close its airspace by July 31?', 'Will Iran close its airspace by December 31?', 'Will Iran close its airspace by June 10?', 'Will Iran close its airspace by June 12?', 'Will Iran close its airspace by June 11?'], 'missing_outcome_count': 1}, {'group_id': 'event:negrisk:<REDACTED>', 'present_outcomes_sample': ['Will Geraldo Alckmin win the 2026 Brazilian presidential election?', 'Will Michelle Bolsonaro win the 2026 Brazilian presidential election?', 'Will Renan Santos win the 2026 Brazilian presidential election?', 'Will Ronaldo Caiado win the 2026 Brazilian presidential election?', 'Will Flávio Bolsonaro win the 2026 Brazilian presidential election?', 'Will Aldo Rebelo win the 2026 Brazilian presidential election?'], 'missing_outcome_count': 7}, {'group_id': 'event:negrisk:<REDACTED>', 'present_outcomes_sample': ["Will Valtteri Bottas be the 2026 F1 Drivers' Champion?", "Will Alexander Albon be the 2026 F1 Drivers' Champion?", "Will Franco Colapinto be the 2026 F1 Drivers' Champion?", "Will Isack Hadjar be the 2026 F1 Drivers' Champion?", "Will Lance Stroll be the 2026 F1 Drivers' Champion?", "Will Oliver Bearman be the 2026 F1 Drivers' Champion?"], 'missing_outcome_count': 3}, {'group_id': 'event:negrisk:<REDACTED>', 'present_outcomes_sample': ['Will Thomas Massie win the 2028 Republican presidential nomination?', 'Will Ted Cruz win the 2028 Republican presidential nomination?', 'Will J.D. Vance win the 2028 Republican presidential nomination?', 'Will John Thune win the 2028 Republican presidential nomination?', 'Will Pete Hegseth win the 2028 Republican presidential nomination?', 'Will Kristi Noem win the 2028 Republican presidential nomination?'], 'missing_outcome_count': 85}, {'group_id': 'event:negrisk:<REDACTED>', 'present_outcomes_sample': ['Will Jalen Brunson win the 2028 US Presidential Election?', 'Will Zohran Mamdani win the 2028 US Presidential Election?', 'Will LeBron James win the 2028 US Presidential Election?', 'Will Andy Beshear win the 2028 US Presidential Election?', 'Will Gretchen Whitmer win the 2028 US Presidential Election?', 'Will JD Vance win the 2028 US Presidential Election?'], 'missing_outcome_count': 83}, {'group_id': 'event:negrisk:<REDACTED>', 'present_outcomes_sample': ['Will Graham Platner win the 2028 Democratic presidential nomination?', 'Will Ruben Gallego win the 2028 Democratic presidential nomination?', 'Will Beto O’Rourke win the 2028 Democratic presidential nomination?', 'Will Jon Stewart win the 2028 Democratic presidential nomination?', 'Will Raphael Warnock win the 2028 Democratic presidential nomination?', 'Will Wes Moore win the 2028 Democratic presidential nomination?'], 'missing_outcome_count': 75}, {'group_id': 'event:negrisk:<REDACTED>', 'present_outcomes_sample': ['Will Turkiye win the 2026 FIFA World Cup?', 'Will Czechia win the 2026 FIFA World Cup?', 'Will Bosnia-Herzegovina win the 2026 FIFA World Cup?', 'Will Iraq win the 2026 FIFA World Cup?', 'Will Congo DR win the 2026 FIFA World Cup?', 'Will Sweden win the 2026 FIFA World Cup?'], 'missing_outcome_count': 4}]
- accelerated_discovery_enabled: True
- markets_scanned_per_tick: 1938 candidates_evaluated_per_tick=497 shadow_labels_per_tick=150 no_trade_labels_per_tick=497
- near_miss_records_written: 518 bregman_diagnostics_records_written=3954
- top_near_miss_edges_after_cost: [0.999, 0.999, 0.999, 0.999, 0.999, 0.999, 0.999, 0.998, 0.998, 0.998]
- top_bregman_rejection_reasons: [{'reason': 'not_exhaustive', 'count': 1969}, {'reason': 'stale_book', 'count': 903}, {'reason': 'no_positive_edge', 'count': 622}, {'reason': 'depth_too_thin', 'count': 434}, {'reason': 'spread_too_wide', 'count': 18}, {'reason': 'bregman_leg_stale_book', 'count': 9}, {'reason': 'no_executable_price', 'count': 5}]
- report_buckets: {'realistic_executable_trades': 0, 'bregman_certified_bundles': 0, 'directional_exploit_trades': 0, 'shadow_exploration': 0, 'no_trade_labels': 497, 'near_miss_rejects': 518, 'paper_relaxed_exploration_trades': 0}
- accelerated_discovery_knobs: {'bregman_discovery_limit': 3000, 'bregman_shadow_labels_per_tick': 150, 'bregman_top_near_misses': 50, 'bregman_near_miss_store_cap': 5000, 'bregman_clob_hydration_max_groups': 250, 'shortlist_limit': 400, 'scan_interval_seconds': 15.0}
- market_quality_tier_counts: {'gold': 0, 'silver': 0, 'bronze': 676, 'watch': 905, 'reject_or_diagnostic': 357}
- market_quality_score_distribution: {'0.8+': 0, '0.6-0.8': 0, '0.4-0.6': 684, '0.2-0.4': 1136, '<0.2': 118}
- targeted_scan_budget_by_category: {'negative_risk_complete': 4, 'short_resolution': 883, 'btc_eth_chainlink': 129, 'fed_macro_reference': 27, 'high_volume_news_linked': 1, 'complete_event_family': 4, 'broad_exploration': 646}
- targeted_scan_markets_by_category: {'negative_risk_complete': 72, 'short_resolution': 1126, 'btc_eth_chainlink': 145, 'fed_macro_reference': 27, 'high_volume_news_linked': 1, 'complete_event_family': 72, 'broad_exploration': 711}
- high_liquidity_binary_markets_scanned: 130
- complete_yes_no_tight_spread_markets_scanned: 244
- negative_risk_complete_events_scanned: 75
- short_resolution_markets_scanned: 1126
- btc_eth_chainlink_markets_scanned: 145
- fed_macro_reference_markets_scanned: 27
- high_volume_news_linked_markets_scanned: 1
- complete_event_families_scanned: 75
- thin_depth_scan_waste_count (KNOWN-thin only): 1928
- stale_book_scan_waste_count (KNOWN-stale only): 1937
- targeted_scan_missing_data_counts (NOT waste): {'missing_book_timestamp': 0, 'missing_depth': 11, 'missing_volume': 1}
- scan_deprioritized_groups: 20 cooldown_active=1978 reasons={'stale_book': 20, 'thin_depth': 18}
- not_exhaustive_high_quality_groups: 0 (sibling=0 grok=0 shadow_only=0)
- targeted_scan_noop_reasons: {'thin_depth_deprioritized': '0/1938 markets matched thin_depth_deprioritized (binaries seen=263)', 'stale_book_refresh': '0/1938 markets matched stale_book_refresh (binaries seen=263)'}
  - best: 573655 tier=bronze score=0.524429 categories=['short_resolution', 'btc_eth_chainlink', 'high_volume_news_linked']
  - best: 2412401 tier=bronze score=0.517589 categories=['short_resolution']
  - best: 2518247 tier=bronze score=0.510929 categories=['short_resolution', 'btc_eth_chainlink']

## 12. Paper Training Metrics

- equity: 500.0
- total_pnl: 0.0
- after_cost_pnl: 0
- open_positions: 1
- closed_positions: 0
- paper_trades: 0
- win_rate_traded_only: unknown

## 13. Strategy Attribution

- paper_attribution_enabled: yes
- exploration_validation_separated: yes

## 14. Fill Realism

- fill_realism_enabled: yes
- fantasy_fill_rejections: 0

### 14a. Paper Realism (Pass 3)

- total_candidates_considered: 10
- realistic_trade_count: 1
- shadow_trade_count: 0
- hard_reject_count: 0
- reference_fill_attempts: 0
- reference_fills_allowed: 0
- reference_fills_blocked: 0
- stale_book_rejection_count: 9
- missing_ask_rejection_count: 0
- thin_depth_rejection_count: 0
- wide_spread_rejection_count: 0
- ambiguity_rejection_count: 0
- offline_stub_rejection_count: 0
- avg_spread_executed: unknown
- avg_depth_executed: unknown
- avg_book_age_executed: unknown

PnL separation (only realistic_executable counts toward readiness):
- bregman_realistic_pnl: 0
- directional_realistic_pnl: 0
- exploration_pnl: 0
- shadow_theoretical_pnl: 0
- reference_fill_theoretical_pnl: 0
- realistic_pnl: 0
- readiness_pnl: 0

Realism posture:
- reference_price_fills_allowed_for_exploit: no
- missing_ask_fallback_allowed: no
- stale_book_fills_allowed: no
- offline_stub_fills_count_as_real: no
- bregman_requires_all_executable_legs: yes

### 14a-2. 100X Feedback Accelerator + Paper Trade Acceleration

- aggressive_paper_training_enabled: yes
- feedback_accelerator_enabled: yes
- feedback_accelerator_target_multiplier: 100
- feedback_accelerator_requested_multiplier: 100
- feedback_accelerator_effective_capacity_multiplier: 100
- feedback_accelerator_effective_capacity_cap: 100
- paper_profit_discovery_profile_enabled: yes
- active_learning_enabled: yes
- exploration_enabled: yes
- accelerated_discovery_enabled: yes
- real_execution_possible: no
- live_flags_forced_off: yes

Tiny paper-learning lanes (exploration PnL excluded from readiness):
- active_learning_tiny_trades_selected: 1
- active_learning_tiny_trades_opened: 1
- relaxed_bregman_trades_opened: 0
- btc_pulse_paper_trades_opened: 0
- exploration_pnl: 0.0
- readiness_pnl_excludes_exploration: yes
- active_learning_tiny_trades_blocked_by_reason: {}

Lane-specific zero-trade blockers (empty == lane opened >=1 paper trade):
- bregman_blocker: no_certified_bregman_opportunity: dominant_reject=not_exhaustive(2214)
- relaxed_bregman_blocker: positive_candidates_found_but_unfilled: positive=6; dominant_reject_reason=negative_after_cost_edge; reject_reasons={'stale_book': 855, 'negative_after_cost_edge': 1113, 'depth_insufficient_for_1usd': 144, 'bregman_leg_stale_book': 6}; best_reject={'group_id': 'binary:event:149812', 'group_type': 'binary_yes_no', 'after_cost_edge': -0.003502, 'reject_reason': 'negative_after_cost_edge', 'depth_for_1usd': 1.0278, 'n_legs': 2}
- tiny_directional_blocker: (none)
- btc_pulse_blocker: btc_pulse_disabled
- paper_trade_acceleration_blocker_if_any: (none)

### 14b. Strategy Priority (Pass 4)

- Bregman evaluated before directional: yes
- Directional consumed capacity before Bregman: no (should be false)
- Bregman groups discovered: 493
- Bregman certified (realistic executable): 0
- Bregman bundles opened before directional: 0
  - Why zero opened: no certified-realistic Bregman opportunity this tick (see metrics/bregman_execution.json rejected_by_reason)
- bregman_reserved_slots: 0
- bregman_reserved_capital_usd: 0.0
- directional_slots_before_bregman: 8
- directional_slots_after_bregman: 7
- directional_trades_blocked_by_bregman_reservation: 0
- directional_trades_blocked_by_bregman_market_collision: 0
- directional_trades_blocked_by_bregman_event_collision: 0
- unused_bregman_slots_released_to_directional: 3
- unused_bregman_capital_released_to_directional: 100.0
- exploration_blocked_from_reserved_bregman_capacity: 0
- Exploration consumed reserved Bregman capacity: no (blocked by default)

### 14c. Profitability Ranking (Pass 5)

- Profitability-first enabled: yes
- Annotation before truncation: yes
- Bregman-first priority preserved: yes (should be true)
- Execution without annotation: 0 (should be 0)
- candidates_annotated: 1
- candidates_missing_profitability_data: 0
- directional_after_cost_positive: 0
- bregman_after_cost_positive: 0
- candidates_rejected_negative_after_cost: 0
- candidates_shadow_theoretical_only: 0
- profitability_governor_hard_rejects: 0
- avg_after_cost_edge_executed: 0.0
- avg_after_cost_roi_executed: 0.0
- total_expected_value_usd_executed: 0
- top_ranked_candidate_reason: no after-cost-positive executable candidate this run
- profitability_buckets: {'exploration_feedback_positive': 1}

### 14d. Active Learning (Pass 6)

- Active learning enabled: yes
- Active learning runtime enabled: yes
- Active learning config source: aggressive_paper_profile
- Config mismatch (declared vs effective): no (should be false)
- Tiny evaluator called: 1
- Tiny candidates evaluated: 1
- Tiny trades selected: 1
- Tiny trades opened: 1
- Selected-but-not-evaluated (must be 0): 0
- Tiny blocked by reason: {}
- Random exploration enabled: no (should be false)
- Random/hash exploration opened trades: 0 (should be 0)
- Legacy random exploration blocked: 77
- Exploration counted toward readiness: no (should be false)
- Exploration consumes Bregman reserved capacity: no (should be false)
- active_learning_candidates_considered: 1080
- active_learning_candidates_selected: 1080
- exploration_trades_opened: 1
- exploration_shadow_only: 324
- exploration_rejected_by_realism: 34
- exploration_rejected_by_budget: 0
- exploration_rejected_by_collision: 0
- exploration_rejected_by_diversity: 0
- exploration_budget_used_usd: 1.75
- exploration_expected_loss_usd: 0.0105
- exploration_pnl: 0
- avg_active_learning_score_selected: 0.350137
- avg_execution_quality_selected: 0.189968
- top_learning_buckets: ['calibration_gap_bucket', 'chainlink_disagreement_case', 'model_uncertain_high_liquidity']
- category_coverage: {'uncategorized': 1}
- pending_feedback_count: 1
- completed_feedback_count: 0

### 14e. Correlation Risk (Pass 7)

- Correlation gate enabled: yes
- Unknown clusters become shadow-only: yes (default)
- Real trade without cluster metadata: 0 (should be 0)
- candidates_with_cluster_id: 1
- candidates_missing_cluster_id: 0
- open_clusters_count: 0
- open_events_count: 0
- open_correlation_groups_count: 0
- blocked_same_market: 0
- blocked_same_condition: 0
- blocked_same_event: 0
- blocked_same_cluster: 0
- blocked_bregman_market_collision: 0
- blocked_bregman_event_collision: 0
- blocked_exploration_cluster_collision: 0
- size_capped_by_cluster_exposure: 0
- shadowed_unknown_cluster: 0
- directional_trades_blocked_by_correlation: 0
- exploration_trades_blocked_by_correlation: 0
- bregman_bundles_blocked_as_duplicates: 0
- bregman_bundles_blocked_as_overlapping: 0
- max_cluster_exposure_usd: 0.0
- max_event_exposure_usd: 0.0
- top_open_clusters: []

## 15. Calibration Metrics

- brier: 0.0
- ece: 0.0
- sharpe: unknown
- sortino: unknown
- calmar: unknown
- max_drawdown: 0.0

## 16. Test Results

| Suite | exit | summary |
|---|---|---|
| full | 0 | — |
| chainlink | 0 | — |
| btc_pulse | 0 | — |
| fast_price | 0 | — |
| news | 0 | — |
| bregman | 0 | — |
| paper_attribution | 0 | — |
| inspection | 0 | — |

## 17. Docker Logs / Errors

- hermes-training logs collected: yes (see `logs/hermes-training_tail1000.log`)
- hermes-trading-engine logs collected: yes (see `logs/hermes-trading-engine_tail500.log`)

## 18. API Snapshot Summary

| Endpoint | ok | status | note |
|---|---|---|---|
| health | yes | 200 |  |
| state | yes | 200 |  |
| venues_status | yes | 200 |  |
| chainlink_status | yes | 200 |  |
| news_status | yes | 200 |  |
| research_status | yes | 200 |  |
| micro_live_status | yes | 200 |  |
| guarded_live_status | yes | 200 |  |
| production_review_status | yes | 200 |  |

## 19. Artifacts Included

- metrics: present (not copied) (91431066 bytes)
- reports: copied (13653 bytes)
- training: present (not copied) (9577148333 bytes)
- micro_live_artifacts: copied (7331784 bytes)
- Missing (recorded, not fatal): data, paper_artifacts, training_artifacts, shadow_artifacts, post_canary_artifacts, replay_artifacts, production_review_artifacts, guarded_live_artifacts

## 20. Missing Features / Missing Evidence

- None detected.

## 21. Key Problems Found

- [WARN] safety audit raised warnings
- [WARN] NOT RUN-READY: stale_or_mixed_training_tail_samples: decision_records.jsonl: last_run_id=pmtrain-1781843034 != events run_id=pmtrain-1781844354; no_trade_labels.jsonl: last_run_id=pmtrain-1781843034 != events run_id=pmtrain-1781844354; pending_labels.jsonl: last_run_id=pmtrain-1781843034 != events run_id=pmtrain-1781844354
- [WARN] No certified Bregman opportunities found yet; continue paper training.

## 22. Recommended Next Fixes

- None — bot looks healthy.

## 23. Algorithmic Benchmarks

Summary: pass=10 warn=0 fail=0 missing=12

| Benchmark | Value | Target | Dir | Status | Description |
|---|---|---|---|---|---|
| after_cost_pnl | 0 | 0.0 | higher | PASS | After-cost paper PnL/expectancy (net of fees+slippage). |
| bregman_certified_profit | 0.0 | 0.0 | higher | PASS | Certified Bregman opportunity profit (paper). |
| bregman_false_positive_rate | 0.0 | 0.2 | lower | PASS | Bregman false-positive rate (incoherent but not certifiable). |
| btc_pulse_after_cost_pnl | unknown | 0.0 | higher | MISSING | BTC Pulse after-cost paper PnL. |
| win_rate_traded_only | unknown | 0.5 | higher | MISSING | Win rate over traded-only paper decisions. |
| sharpe | unknown | 1.0 | higher | MISSING | Sharpe ratio (paper equity curve). |
| sortino | unknown | 1.5 | higher | MISSING | Sortino ratio (downside-only). |
| calmar | unknown | 1.0 | higher | MISSING | Calmar ratio (return / max drawdown). |
| max_drawdown | 0.0 | 0.15 | lower | PASS | Max drawdown (fraction of equity). |
| cvar | unknown | -0.1 | higher | MISSING | Conditional VaR / Expected Shortfall of paper returns (closer to 0 is better). |
| brier | 0.0 | 0.25 | lower | PASS | Brier score (probability calibration). |
| ece | 0.0 | 0.05 | lower | PASS | Expected calibration error. |
| ece_cal | unknown | 0.05 | lower | MISSING | Calibrated ECE (post-calibration). |
| calibration_improved | unknown | True | bool | MISSING | Calibrated ECE beats raw ECE. |
| fill_realism_rejection_rate | 0.0 | 0.5 | lower | PASS | Realistic-fill (fantasy-fill) rejection rate; very high => feed/book problem. |
| exploration_validation_separated | yes | True | bool | PASS | Exploration trades are tracked separately from validation evidence. |
| paper_attribution_enabled | yes | True | bool | PASS | Per-strategy paper attribution is available. |
| fill_realism_enabled | yes | True | bool | PASS | Realistic-fill modeling is enabled. |
| bregman_executable_depth_ok | unknown | True | bool | MISSING | Certified Bregman legs pass executable-depth proof before sizing up. |
| significance_passed | unknown | True | bool | MISSING | Sharpe/Sortino/Calmar improvement clears required significance thresholds. |
| walkforward_passed | unknown | True | bool | MISSING | Walk-forward / purged-CV validation passed (not a single-slice artifact). |
| production_ready | unknown | True | bool | MISSING | Production-readiness gate passed (validation-only; exploration excluded). |

## 24. Cross-Surface Consistency

- No inconsistencies detected (dashboard vs paper-training equity, live-detected flags, cost accounting).

## 25. Quant Responsibilities

| Domain | Owner | Coverage | Responsibilities |
|---|---|---|---|
| data_ingestion | Data / market-data engineering | covered | Ingest Polymarket gamma/CLOB market data (read-only); Read Chainlink BTC/USD anchor + Coinbase fast spot feed; Fetch market-news headlines (read-only) |
| preprocessing_features | Feature engineering | covered | Normalize/timestamp/dedupe inputs; build short-horizon returns; Score + sanitize news evidence; cap feature nudges; Apply the market-scan universe limits |
| statistical_modeling | Quant research / modeling | covered | Probability estimation + calibration (isotonic/Platt); Track Brier/ECE; guard against overfitting |
| bregman_signals | Quant research (convex/Bregman) | covered | Group markets; certify Bregman arbitrage-free opportunities (paper); Track false-positive rate + certified profit |
| risk_portfolio | Risk / portfolio | covered | Deterministic RiskEngine gate on every paper order; Exposure/daily-loss caps; correlated + per-event exposure; CVaR + drawdown throttles; fractional-Kelly sizing; Prefer guaranteed after-cost arbitrage over probabilistic edge |
| backtest_simulation | Simulation / backtest | covered | Paper OMS + realistic fills; after-cost expectancy; Resolve labels; record closed trades |
| robustness | Quant validation | covered | Exploration-vs-validation-vs-production separation; regime/stress; Walk-forward + combinatorial purged CV; bootstrap CIs; ablations; Risk-adjusted performance (Sharpe/Sortino/Calmar) significance gates |
| clobv2_execution | Execution (CLOB v2, paper) | covered | Read-only CLOB v2 book freshness; realistic-fill modeling; Reject fantasy fills; available-depth + spread/slippage/fee modeling; Certified arbs size up only when every leg passes executable depth; Never submit real orders (paper) |
| monitoring | MLOps / monitoring | covered | Health/benchmark reporting; test suite green; Uptime + drift/kill-switch monitoring |
| compliance_security_ops | Compliance / security / ops | covered | PAPER-only enforcement; no live/wallet/order paths; Secret redaction; forbidden-live-flag audit |

## 26. Final Validation (Execution & Readiness)

- validation_ready: **False** (exploration excluded from the verdict)

| Check | Value |
|---|---|
| after_cost_pnl | 0.0 |
| bregman_opportunity_decay | None |
| bregman_certified_profit | 0.0 |
| bregman_executable_depth_ok | None |
| rejected_bad_fills | 0.0 |
| fill_realism_rejection_rate | 0.0 |
| calibration_rollbacks | None |
| walkforward_passed | None |
| significance_passed | None |
| production_ready | None |
| live_detected | False |

## 27. Files Included In Bundle

- algorithmic_edge_audit.json
- api/chainlink_status.json
- api/guarded_live_status.json
- api/health.json
- api/micro_live_status.json
- api/news_status.json
- api/production_review_status.json
- api/research_status.json
- api/state.json
- api/venues_status.json
- artifact_paths.json
- changed_files.txt
- consistency.json
- data/training/learning_state.json
- docker_compose_config.txt
- docker_compose_ps.txt
- docker_compose_redacted.yml
- docker_images.txt
- docker_volumes.txt
- dockerfile_snapshot.txt
- env_example_redacted.txt
- env_redacted.txt
- feature_health.json
- final_validation.json
- git_branch.txt
- git_diff_stat.txt
- git_log_recent.txt
- git_status.txt
- hermes_training_status.txt
- improvement_trend.json
- ledger_reconciliation.json
- logs/hermes-trading-engine_tail500.log
- logs/hermes-training_tail1000.log
- metrics/active_learning.json
- metrics/benchmarks.json
- metrics/bregman.json
- metrics/bregman_execution.json
- metrics/bregman_funnel.json
- metrics/bregman_source_reconciliation.json
- metrics/btc_pulse.json
- metrics/calibration.json
- metrics/chainlink.json
- metrics/closed_loop_artifacts_manifest.json
- metrics/closed_loop_learning.json
- metrics/correlation_risk.json
- metrics/exploration_vs_validation.json
- metrics/fast_btc_price.json
- metrics/feeds_health.json
- metrics/fill_realism.json
- metrics/grok_news_evidence.json
- metrics/grok_research.json
- metrics/inspection_summary.json
- metrics/learning_feedback.json
- metrics/market_scan.json
- metrics/news_quality.json
- metrics/paper_realism.json
- metrics/paper_training_metrics.json
- metrics/pnl_by_strategy.json
- metrics/profitability_ranking.json
- metrics/risk_and_safety.json
- metrics/run_ready.json
- metrics/strategy_attribution.json
- metrics/strategy_priority.json
- metrics/training_reconciliation.json
- missing_features.json
- performance_summary.json
- quant_responsibilities.json
- recommendations.json
- report.json
- reports/closed_loop_learning_audit.md
- reports/paper_training_inspection.md
- requirements_dev_snapshot.txt
- requirements_snapshot.txt
- safety/forbidden_live_flags.json
- safety/redaction_audit.json
- safety/safety_audit.json
- samples/completed_labels_tail_500.jsonl
- samples/decision_records_tail_500.jsonl
- samples/diagnostics_tail_500.jsonl
- samples/event_file_stats.json
- samples/events_tail_500.jsonl
- samples/no_trade_labels_tail_500.jsonl
- samples/pending_labels_tail_500.jsonl
- samples/shadow_labels_tail_500.jsonl
- test_results_bregman.txt
- test_results_btc_pulse.txt
- test_results_chainlink.txt
- test_results_fast_price.txt
- test_results_full.txt
- test_results_inspection.txt
- test_results_news.txt
- test_results_paper_attribution.txt
- validation_contract.json
