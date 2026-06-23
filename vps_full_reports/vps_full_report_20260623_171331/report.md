# BTC 5-Minute Pulse — FULL Performance Report

_PAPER ONLY. `live_trading_enabled=False` · `global_reconciled=True` · ticks 165._


## 1. Capital & P&L

| metric | value |
|---|---|
| On-hand capital | $405.73 |
| Starting capital | $500.0 |
| Return | -18.85% |
| Open exposure | $0.0 (0 pos) |
| Trades / settled | 373 / 373 |
| Win rate | 0.5228 |
| Win rate up / down | 0.5137 / 0.5286 |
| Realized PnL | $-94.2675 |
| Profit factor | 0.8377 |
| Avg win / avg loss | $3.0368 / $3.9712 |
| Max drawdown | $151.7258 |
| Avg PnL/trade | -0.2527 |
| Side counts | {'up': 146, 'down': 227} |
| Settle sources | {'polymarket_resolution': 177, 'rtds_chainlink_proxy': 180} |
| Proxy vs official | {'both': 176, 'agree': 170, 'disagree': 6} |
| EV before/after cost | 0.113086 / 0.107354 |

## 2. Accounting integrity (reconciliation)

- **global_reconciled:** True
- **scope_note:** lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- **rejected_before_execution:** 24553

## 3. Candidate lifecycle

created 34109 · terminals `{'accepted': 222, 'rejected': 32619, 'skipped': 1230, 'expired': 0, 'missing_data': 38}`

rejected_by_stage `{'directional': 23285, 'execution_gate': 66, 'selectivity_gate': 1020, 'context_gate': 543, 'grok_decider': 7705}`

## 4. Execution gate & calibration

candidates 394 · accepted 328 · rejects `{'wide_spread': 16, 'insufficient_depth': 0, 'negative_ev_after_slippage': 50, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 0, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0, 'underdog_price_below_floor': 0}`

calibration `{'samples': 373, 'brier': 0.229875, 'log_loss': 0.655492, 'base_rate_up': 0.4879, 'baseline_brier_0_5': 0.25}`

## 5. PnL by bucket (all dimensions)


## 6. Learned selectivity gate

- **decision_rule:** confidently_below_breakeven
- **confidence_z:** 1.64
- **accepted:** 66
- **rejected:** 1020
- **explored:** 54
| dim | bucket | n | WR | breakeven | WR_upperCI | EV/trade | blocked |
|---|---|---|---|---|---|---|---|
| markov_state | stale_polymarket_up | 80 | 0.4875 | 0.5899 | 0.5781 | -0.7955 | True |
| spread_bucket | <=0.01 | 281 | 0.5053 | 0.5541 | 0.554 | -0.4228 | True |
| zscore_bucket | 1..2 | 35 | 0.4 | 0.5194 | 0.5382 | -1.095 | False |
| hurst_regime | insufficient_data | 31 | 0.3871 | 0.486 | 0.534 | -0.9428 | False |
| zscore_bucket | na | 41 | 0.4146 | 0.519 | 0.5422 | -0.9139 | False |
| confidence_tier | high | 186 | 0.5161 | 0.5753 | 0.5756 | -0.4913 | False |
| ttc_bucket | 120-240s | 96 | 0.5417 | 0.6014 | 0.6228 | -0.4863 | False |
| zscore_bucket | -1..1 | 162 | 0.5062 | 0.5586 | 0.57 | -0.4576 | False |

counterfactual `{'replayed': 200, 'trades_rejected': 188, 'losses_avoided': 94, 'pnl_removed_by_rejects': -91.5001, 'counterfactual_trades': 12, 'counterfactual_win_rate': 0.5833, 'counterfactual_pnl_usd': -7.7199, 'baseline_trades': 200, 'baseline_win_rate': 0.505, 'baseline_pnl_usd': -99.22, 'reject_reasons_by_bucket': {'bad_bucket:spread_bucket=<=0.01': 185, 'bad_bucket:markov_state=stale_polymarket_up': 3}, 'note': 'in-sample replay using final accumulated bucket evidence (diagnostic estimate)'}`

## 7. Entry gates (context / late-window / reward-risk)

context_gate enabled=True · blocked 543 · `{'tv_context_volume_spike': 266, 'tv_context_ttc_too_far': 205, 'tv_context_hurst_noise': 72}`

late_window gate=False · verdict insufficient_evidence · LHC `{'n': 12, 'win_rate': 0.4167, 'pnl_usd': -19.5519, 'avg_pnl_usd': -1.6293, 'avg_ev_after_cost': 0.137604}` · other `{'n': 83, 'win_rate': 0.5181, 'pnl_usd': -6.3028, 'avg_pnl_usd': -0.0759, 'avg_ev_after_cost': 0.101571}`

## 8. Grok Decision Engine (decides; bot executes)

- **mode:** follow
- **affects_trading:** True
- **decided:** 276
- **errors:** 4
- **skipped_budget:** 0
- **avg_latency_s:** 5.963
- **graded_directional:** 0
- **direction_accuracy:** None
- **brier:** None
- **views_graded:** 225
- **view_accuracy:** 0.5156
- **view_brier:** 0.2528
- **abstains:** 268
- **follow_fraction:** 1.0
- **explore_rate:** 0.5
- **adaptive_enabled:** True

by_action `{'no_trade': {'n': 268, 'direction_accuracy': None, 'pnl_usd': 0.0}}`

adaptive_policy_counts `{'exploit': 0, 'explore': 0, 'avoid': 0}`

aggression `{'aggression': 0.55, 'min': 0.0, 'max': 1.0, 'step_up': 0.05, 'step_down': 0.1, 'recent_net_pnl': 25.5716, 'updates': 21, 'note': 'loosens (more explore/looser exploit/larger size) as acted trades profit; tightens on losses; circuit breaker is the hard floor.'}`

accuracy_by_context `{"hurst_regime": {"insufficient_data": {"n": 17, "accuracy": 0.3529}, "trending": {"n": 194, "accuracy": 0.5309}, "noise": {"n": 14, "accuracy": 0.5}}, "markov_state": {"stale_polymarket_up": {"n": 65, "accuracy": 0.5385}, "stale_polymarket_down": {"n": 72, "accuracy": 0.5278}, "chop_noise": {"n": 88, "accuracy": 0.4886}}, "ttc_bucket": {">=240s": {"n": 225, "accuracy": 0.5156}}, "conviction_bucket": {"coinflip": {"n": 224, "accuracy": 0.5134}, "lean": {"n": 1, "accuracy": 1.0}}}`

view_edge_candidates `[]`

circuit_breaker `{'tripped': True, 'reason': 'daily_loss_cap', 'consecutive_losses': 0, 'daily_follow_loss_usd': 31.88, 'daily_loss_cap_usd': 30.0, 'trips': 24, 'cooldown_remaining_s': 1311.4, 'max_consecutive_losses': 4, 'max_latency_s': 20.0}`

news_digest `{"enabled": true, "interval_s": 300.0, "calls": 271, "errors": 2, "skipped_budget": 0, "latest": {"sentiment": "bearish", "confidence": 0.55, "headlines": ["BTC drops to ~$62k amid $717M+ liquidations and Nasdaq/tech selloff", "Bitcoin ETFs see net outflows (~$68M reported) despite some Ark/Fidelity inflows", "Ongoing leverage flush and macro risk-off pressure (Fed hawkish, ETF redemptions)"], "event_risk": "low"}, "age_s": 4.4}`

recent_decisions `[{"action": "no_trade", "p_up": 0.48, "confidence": 0.0, "outcome_up": true, "view_correct": false, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_up", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.485, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_up", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.47, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.47, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "co`

## 9. Grok signal intel (analyst + predictor + budget)

budget `{'daily_usd_cap': 20.0, 'est_usd_per_call': 0.02, 'spent_today_usd': 0.24, 'calls_today': 12, 'per_feature_hourly': {'predictor': 120, 'analyst': 6, 'overlay': 20, 'decider': 60, 'news': 30}}`

predictor_B `{'enabled': True, 'observe_only': True, 'affects_trading': False, 'off_hot_path': True, 'requested': 360, 'predicted': 357, 'errors': 3, 'skipped_budget': 0, 'scored': 331, 'accuracy': 0.5227, 'brier': 0.2523, 'pending': 0, 'note': 'observe-only Grok P(up) per signal; graded vs realized BTC move before it could ever be trusted; never places/sizes/bypasses a trade.'}`

analyst_A last_note `{"summary": "DOWN_STRONG signals (n=64, wr=0.5938) and range_bottom entries (n=33, wr=0.7879) show confirmed positive EV and pnl after costs with n>=8; lower_wick_rejection and dead-volume states also appear profitable while UP and short-ttc buckets remain loss-making. Overall sample pnl is still negative due to UP and <60s trades dragging results despite positive avg_ev_after_cost across most buckets. Sample size remains modest so selectivity on DOWN + favorable range/candle states is the clearest edge.", "working": ["DOWN_STRONG (n=64, wr=0.5938, +17.7 pnl)", "range_bottom (n=33, wr=0.7879, +54.4 pnl)", "lower_wick_rejection (n=17, wr=0.7059, +24.7 pnl)", ">=240s ttc (n=20, wr=0.6, +12.2 pnl)", "dead volume (n=47, wr=0.5957, +10.4 pnl)", "bearish_aligned mtf (n=43, wr=0.5814, +21.2 pnl)"], "failing": ["UP signals overall (n=30, wr=0.4, -45.4 pnl)", "<60s ttc (n=18, wr=0.222, -52.2 pnl)", "volume spike (n=14, wr=0.2857, -36.3 pnl)", "range_top (n=20, wr=0.3, -45 pnl)", "1..2 zscore (n=9, wr=0.222, -33.2 pnl)"], "warnings": ["Total settled n=115 still modest; many buckets near n=8-20 threshold so overfitting risk high", "UP direction and short-hold trades consistently destroy pnl",`

## 10. TradingView learning

- **tradingview_alerts_received:** 360
- **tradingview_alerts_valid:** 360
- **tradingview_alerts_rejected:** 0

settled_with_signal 115

best_buckets `[{"dimension": "cvd_state", "bucket": "buy_pressure", "n": 5, "win_rate": 0.2, "pnl_usd": -10.2941, "avg_ev_after_cost": 0.1854, "all_reconciled": true}, {"dimension": "bb_state", "bucket": "expansion_up", "n": 9, "win_rate": 0.3333, "pnl_usd": -8.4759, "avg_ev_after_cost": 0.155793, "all_reconciled": true}, {"dimension": "zscore_bucket", "bucket": "<=-2", "n": 7, "win_rate": 0.4286, "pnl_usd": -6.5929, "avg_ev_after_cost": 0.152993, "all_reconciled": true}, {"dimension": "ttc_bucket", "bucket": "<60s", "n": 18, "win_rate": 0.2222, "pnl_usd": -52.2164, "avg_ev_after_cost": 0.144925, "all_reconciled": true}, {"dimension": "spread_bucket", "bucket": "0.01-0.03", "n": 9, "win_rate": 0.5556, "pnl_usd": -4.3291, "avg_ev_after_cost": 0.134029, "all_reconciled": true}]`

worst_buckets `[{"dimension": "liquidation_spike", "bucket": "True", "n": 3, "win_rate": 0.6667, "pnl_usd": 1.3405, "avg_ev_after_cost": 0.076747, "all_reconciled": true}, {"dimension": "hurst_regime", "bucket": "noise", "n": 3, "win_rate": 0.3333, "pnl_usd": -6.5254, "avg_ev_after_cost": 0.07809, "all_reconciled": true}, {"dimension": "vwap_state", "bucket": "reclaim", "n": 3, "win_rate": 0.6667, "pnl_usd": -1.6643, "avg_ev_after_cost": 0.078272, "all_reconciled": true}, {"dimension": "zscore_bucket", "bucket": "-2..-1", "n": 17, "win_rate": 0.5294, "pnl_usd": -11.1695, "avg_ev_after_cost": 0.088252, "all_reconciled": true}, {"dimension": "candle_pressure", "bucket": "upper_wick_rejection", "n": 9, "win_rate": 0.6667, "pnl_usd": 1.188, "avg_ev_after_cost": 0.089353, "all_reconciled": true}]`

rsi_trend hit_rate 0.5125 (n 359) · pred_acc 0.4695

## 11. Loop engineering (maker-checker / lessons / loops / research)

**Verifier (independent Claude maker-checker):** `{"enabled": true, "verified": 134, "approvals": 127, "vetoes": 7, "errors": 10, "approve_rate": 0.9478, "approved_acted_settled": {"n": 7, "win_rate": 0.5714, "pnl_usd": 11.1176}, "avg_latency_s": 4.66}`

**Research meta-loop:** `{"enabled": true, "calls": 23, "auto_apply": true, "lessons_added": 131}`

- research summary: 373 settled trades, 52.3% win rate, -$94 PnL, profit factor 0.84. No directional edge: TradingView signal hit rate 53.9% vs 42.1% baseline is noise (n=115). DOWN trades (58.8% win, n=85) slightly better than UP (40% win, n=30), but losses exceed wins ($3.97 avg loss vs $3.04 avg win). Execution gate rejecting 48 negative-EV trades shows gate is working, but not enough.

**Lessons (compounding rules):** count 139
- [`research`] ttc <60s and 120-240s both lose similarly. Time decay not a differentiator.
- [`research`] conviction <0.2 won (n=2, +$4.7), >=0.8 lost (n=1, -$5). Noise, not signal—wait for n>20.
- [`research`] Brier 0.2299 vs baseline 0.25, edge_model buckets show no monotonic relationship to empirical_up. Model uncalibrated.
- [`research`] 48 trades rejected for negative_ev_after_slippage. Gate working, but upstream model still generates bad candidates.
- [`research`] chop_noise, trend_down, resolution_danger, stale_polymarket_down all negative PnL. No state has edge.
- [`research`] 373 settled but only 8-9 per context bucket. Need 500+ settled, stratified, before claiming any edge.
- [`research`] UP trades: 40% win rate (n=30) vs DOWN 58.8% (n=85); avoid UP until sample-backed edge appears
- [`research`] volume_state=dead in latest signal; correlate volume_state with outcomes, likely poor execution environment
- [`research`] avg_loss ($3.97) > avg_win ($3.03) erodes edge; enforce stop-loss or position-sizing discipline
- [`research`] TradingView signal hit rate 53.9% vs 42.1% baseline (n=115) is not statistically significant; do not rely on signal direction alone

**Sub-loops:** data_ingestion, execution, heartbeat, lessons, news, research_meta, risk_monitor, signal_generation, verifier

## 12. Edge signal & readiness

edge_signal `{"enabled": true, "observe_only": true, "report_only": true, "affects_trading": false, "settled": 135, "by_stale_divergence": {"not_stale": {"n": 114, "win_rate": 0.5614, "pnl_usd": -7.9243, "avg_ev_after_cost": 0.100563, "all_reconciled": true}, "already_priced": {"n": 12, "win_rate": 0.3333, "pnl_usd": -20.3432, "avg_ev_after_cost": 0.166486, "all_reconciled": true}, "stale_polymarket_up": {"n": 4, "win_rate": 0.25, "pnl_usd": -13.6709, "avg_ev_after_cost": 0.136871, "all_reconciled": true}, "stale_polymarket_down": {"n": 5, "win_rate": 0.4, "pnl_usd": -10.0715, "avg_ev_after_cost": 0.103074, "all_reconciled": true}}, "by_ttc_bucket": {"240_300s": {"n": 35, "win_rate": 0.5143, "pnl_usd": 0`

**CEX-lead latency edge** (grades CEX-implied P(up) vs the MARKET price): mode `shadow` · affects_trading False · signals_seen 2687 · graded 44 · drove 0 · any_proven (beats market) **False**
| divergence | n | acc | brier_cex | brier_mkt | beats_mkt | avg_pnl/u | proven |
|---|---|---|---|---|---|---|---|
| >=0.30 | 36 | 0.4444 | 0.4378 | 0.2433 | False | -0.0474 | False |
| 0.15-0.30 | 8 | 0.75 | 0.2373 | 0.2795 | True | 0.2406 | False |
_promotion: n>=min AND wilson_lower(win_rate)>breakeven AND Brier_cex<Brier_market AND avg_pnl>0_

readiness `{'report_only': True, 'status': 'not_ready', 'ready_to_claim_80pct': False, 'gates': {'accepted_ge_100': True, 'accepted_ge_500': False, 'accepted_ge_1000': False, 'win_rate_ge_80': False, 'positive_net_paper_pnl': False, 'profit_factor_ok': False, 'calibration_error_ok': False, 'max_drawdown_ok': False, 'loss_size_le_win_size': False, 'no_reconciliation_failures': True, 'no_missing_settlement_data': True, 'no_unmodeled_fill_assumptions': True, 'no_safety_bypass': True}, 'metrics': {'accepted': 373, 'win_rate': 0.5228, 'net_pnl_usd': -94.2675, 'profit_factor': 0.8377, 'calibration_error': 0.229875, 'max_drawdown_usd': 151.7258, 'avg_win_usd': 3.0368, 'avg_loss_usd': 3.9712}}`

## 13. Recent paper positions

| window | side | entry_mode | entry | fair | outcome | won | pnl |
|---|---|---|---|---|---|---|---|
| 11:15AM-11:20AM ET | down | standard | 0.6100000000000001 | 0.19427170985707087 | down | ✓ | 3.196721 |
| 11:10AM-11:15AM ET | down | standard | 0.44 | 0.49678851666380386 | up | ✗ | -5.0 |
| 10:40AM-10:45AM ET | up | standard | 0.22 | 0.35756196939836615 | down | ✗ | -5.0 |
| 10:35AM-10:40AM ET | up | standard | 0.54 | 0.6104270006032282 | down | ✗ | -5.0 |
| 10:30AM-10:35AM ET | up | standard | 0.34 | 0.43437352793537876 | up | ✓ | 9.705882 |
| 10:25AM-10:30AM ET | up | late_window | 0.04 | 0.12460505685686929 | down | ✗ | -5.0 |
| 10:20AM-10:25AM ET | up | standard | 0.029999999999999995 | 0.09264004805165031 | down | ✗ | -5.0 |
| , 9:45AM-9:50AM ET | up | standard | 0.5679553607014746 | 0.6629422116481204 | down | ✗ | -5.0 |
| , 9:25AM-9:30AM ET | down | standard | 0.7 | 0.11973152022954285 | down | ✓ | 2.142857 |
| , 9:20AM-9:25AM ET | down | standard | 0.47000000000000003 | 0.46509388252257666 | down | ✓ | 5.638298 |
| , 9:15AM-9:20AM ET | down | standard | 0.77 | 0.1563527223246346 | down | ✓ | 1.493506 |
| , 9:10AM-9:15AM ET | down | standard | 0.7 | 0.2304289547323075 | up | ✗ | -5.0 |
| , 9:05AM-9:10AM ET | down | standard | 0.39 | 0.545432240039216 | down | ✓ | 7.820513 |
| , 8:45AM-8:50AM ET | down | standard | 0.7 | 0.18577194133862882 | down | ✓ | 2.142857 |
| , 8:25AM-8:30AM ET | down | standard | 0.65 | 0.2550272848360117 | up | ✗ | -5.0 |
