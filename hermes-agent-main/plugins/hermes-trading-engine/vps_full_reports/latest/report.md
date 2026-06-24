# BTC 5-Minute Pulse — FULL Performance Report

_PAPER ONLY. `live_trading_enabled=False` · `global_reconciled=True` · ticks 7487._


## 1. Capital & P&L

| metric | value |
|---|---|
| On-hand capital | $435.19 |
| Starting capital | $500.0 |
| Return | -12.96% |
| Open exposure | $0.0 (0 pos) |
| Trades / settled | 403 / 403 |
| Win rate | 0.536 |
| Win rate up / down | 0.5247 / 0.5436 |
| Realized PnL | $-64.8098 |
| Profit factor | 0.8866 |
| Avg win / avg loss | $3.0862 / $4.0207 |
| Max drawdown | $151.7258 |
| Avg PnL/trade | -0.1608 |
| Side counts | {'up': 162, 'down': 241} |
| Settle sources | {'polymarket_resolution': 194, 'rtds_chainlink_proxy': 193} |
| Proxy vs official | {'both': 193, 'agree': 187, 'disagree': 6} |
| EV before/after cost | 0.106489 / 0.100646 |

## 2. Accounting integrity (reconciliation)

- **global_reconciled:** True
- **scope_note:** lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- **rejected_before_execution:** 39886

## 3. Candidate lifecycle

created 51438 · terminals `{'accepted': 252, 'rejected': 49536, 'skipped': 1599, 'expired': 0, 'missing_data': 51}`

rejected_by_stage `{'directional': 38236, 'execution_gate': 245, 'selectivity_gate': 1211, 'context_gate': 1211, 'grok_decider': 7705, 'research_avoid': 183, 'directional_allowlist': 745}`

## 4. Execution gate & calibration

candidates 603 · accepted 358 · rejects `{'wide_spread': 55, 'insufficient_depth': 0, 'negative_ev_after_slippage': 89, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 6, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0, 'underdog_price_below_floor': 95}`

calibration `{'samples': 403, 'brier': 0.226026, 'log_loss': 0.645689, 'base_rate_up': 0.4839, 'baseline_brier_0_5': 0.25}`

## 5. PnL by bucket (all dimensions)


## 6. Learned selectivity gate

- **decision_rule:** confidently_below_breakeven
- **confidence_z:** 1.64
- **accepted:** 272
- **rejected:** 1211
- **explored:** 57
| dim | bucket | n | WR | breakeven | WR_upperCI | EV/trade | blocked |
|---|---|---|---|---|---|---|---|
| markov_state | stale_polymarket_up | 80 | 0.4875 | 0.5899 | 0.5781 | -0.7955 | True |
| zscore_bucket | 1..2 | 35 | 0.4 | 0.5194 | 0.5382 | -1.095 | False |
| hurst_regime | insufficient_data | 31 | 0.3871 | 0.486 | 0.534 | -0.9428 | False |
| zscore_bucket | na | 41 | 0.4146 | 0.519 | 0.5422 | -0.9139 | False |
| confidence_tier | high | 186 | 0.5161 | 0.5753 | 0.5756 | -0.4913 | False |
| ttc_bucket | 120-240s | 117 | 0.5556 | 0.5998 | 0.6288 | -0.3619 | False |
| direction | up | 138 | 0.5217 | 0.5612 | 0.5904 | -0.3491 | False |
| spread_bucket | <=0.01 | 305 | 0.5213 | 0.5575 | 0.5678 | -0.3124 | False |

counterfactual `{'replayed': 200, 'trades_rejected': 44, 'losses_avoided': 25, 'pnl_removed_by_rejects': -35.5863, 'counterfactual_trades': 156, 'counterfactual_win_rate': 0.5577, 'counterfactual_pnl_usd': -30.7149, 'baseline_trades': 200, 'baseline_win_rate': 0.53, 'baseline_pnl_usd': -66.3012, 'reject_reasons_by_bucket': {'bad_bucket:markov_state=stale_polymarket_up': 44}, 'note': 'in-sample replay using final accumulated bucket evidence (diagnostic estimate)'}`

## 7. Entry gates (context / late-window / reward-risk)

context_gate enabled=True · blocked 1211 · `{'tv_context_volume_spike': 642, 'tv_context_ttc_too_far': 397, 'tv_context_hurst_noise': 172}`

late_window gate=False · verdict insufficient_evidence · LHC `{'n': 17, 'win_rate': 0.5882, 'pnl_usd': -2.3881, 'avg_pnl_usd': -0.1405, 'avg_ev_after_cost': 0.12225}` · other `{'n': 108, 'win_rate': 0.5463, 'pnl_usd': 5.9912, 'avg_pnl_usd': 0.0555, 'avg_ev_after_cost': 0.088274}`

## 8. Grok Decision Engine (decides; bot executes)

- **mode:** shadow
- **affects_trading:** False
- **decided:** 525
- **errors:** 4
- **skipped_budget:** 0
- **avg_latency_s:** 6.036
- **graded_directional:** 2
- **direction_accuracy:** 0.5
- **brier:** 0.2644
- **views_graded:** 473
- **view_accuracy:** 0.5074
- **view_brier:** 0.2535
- **abstains:** 514
- **follow_fraction:** 1.0
- **explore_rate:** 0.5
- **adaptive_enabled:** True

by_action `{'no_trade': {'n': 514, 'direction_accuracy': None, 'pnl_usd': 0.0}, 'up': {'n': 2, 'direction_accuracy': 0.5, 'pnl_usd': 0.0}}`

adaptive_policy_counts `{'exploit': 0, 'explore': 0, 'avoid': 0}`

aggression `{'aggression': 0.55, 'min': 0.0, 'max': 1.0, 'step_up': 0.05, 'step_down': 0.1, 'recent_net_pnl': 25.5716, 'updates': 21, 'note': 'loosens (more explore/looser exploit/larger size) as acted trades profit; tightens on losses; circuit breaker is the hard floor.'}`

accuracy_by_context `{"hurst_regime": {"insufficient_data": {"n": 26, "accuracy": 0.4231}, "trending": {"n": 419, "accuracy": 0.5131}, "noise": {"n": 28, "accuracy": 0.5}}, "markov_state": {"stale_polymarket_up": {"n": 135, "accuracy": 0.5481}, "stale_polymarket_down": {"n": 153, "accuracy": 0.4902}, "chop_noise": {"n": 184, "accuracy": 0.4946}, "liquidity_danger": {"n": 1, "accuracy": 0.0}}, "ttc_bucket": {">=240s": {"n": 473, "accuracy": 0.5074}}, "conviction_bucket": {"coinflip": {"n": 472, "accuracy": 0.5064}, "lean": {"n": 1, "accuracy": 1.0}}}`

view_edge_candidates `[]`

circuit_breaker `{'tripped': False, 'reason': None, 'consecutive_losses': 0, 'daily_follow_loss_usd': 31.88, 'daily_loss_cap_usd': 30.0, 'trips': 48, 'cooldown_remaining_s': 0, 'max_consecutive_losses': 4, 'max_latency_s': 20.0}`

news_digest `{"enabled": true, "interval_s": 300.0, "calls": 514, "errors": 2, "skipped_budget": 0, "latest": {"sentiment": "neutral", "confidence": 0.7, "headlines": ["Bitcoin tests 2-week low near $62K amid tech sell-off and risk-off sentiment", "Live markets: bitcoin drops to $62,000 as gold falls below $4,000"], "event_risk": "low"}, "age_s": 83.4}`

recent_decisions `[{"action": "no_trade", "p_up": 0.505, "confidence": 0.0, "outcome_up": false, "view_correct": false, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_down", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.46, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_down", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.47, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "chop_noise", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.41, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_down", "ttc_bucket`

## 9. Grok signal intel (analyst + predictor + budget)

budget `{'daily_usd_cap': 20.0, 'est_usd_per_call': 0.02, 'spent_today_usd': 15.46, 'calls_today': 773, 'per_feature_hourly': {'predictor': 120, 'analyst': 6, 'overlay': 20, 'decider': 60, 'news': 30}}`

predictor_B `{'enabled': True, 'observe_only': True, 'affects_trading': False, 'off_hot_path': True, 'requested': 988, 'predicted': 982, 'errors': 4, 'skipped_budget': 0, 'scored': 936, 'accuracy': 0.5214, 'brier': 0.2525, 'pending': 0, 'note': 'observe-only Grok P(up) per signal; graded vs realized BTC move before it could ever be trusted; never places/sizes/bypasses a trade.'}`

analyst_A last_note `{"summary": "DOWN signals (esp. DOWN_STRONG, range_bottom, dead volume, sell_pressure CVD) show win-rates 0.60-0.79 with positive EV after cost on n>=14 samples while UP and short-hold (<60s) buckets remain negative-EV despite similar nominal win-rates. Overall book is still slightly negative (-12 USD) but avg_ev_after_cost stays positive (~0.10) across 145 trades, confirming selectivity is working yet edge is modest. No prior analysis supplied so all observations are baseline.", "working": ["DOWN_STRONG n=74 win=0.608 pnl=+29.6", "range_bottom n=42 win=0.786 pnl=+70.4", "dead volume n=58 win=0.638 pnl=+32.2", "sell_pressure CVD n=14 win=0.786 pnl=+23.1", "bearish_aligned MTF n=45 win=0.60 pnl=+30.3"], "failing": ["UP overall n=46 win=0.478 pnl=-40.2", "<60s TTC n=22 win=0.364 pnl=-39.0", "volume_spike n=14 win=0.286 pnl=-36.3", "range_top n=31 win=0.419 pnl=-37.1", "1..2 zscore n=9 win=0.222 pnl=-33.2"], "warnings": ["total sample still modest (145); many buckets near n=8-20 boundary", "observe_only mode means no live execution risk but also no real slippage data", "positive avg_ev but realized pnl negative implies cost or sizing assumptions may be optimistic"], "changes_since_las`

## 10. TradingView learning

- **tradingview_alerts_received:** 988
- **tradingview_alerts_valid:** 988
- **tradingview_alerts_rejected:** 0

settled_with_signal 145

best_buckets `[{"dimension": "zscore_bucket", "bucket": "<=-2", "n": 7, "win_rate": 0.4286, "pnl_usd": -6.5929, "avg_ev_after_cost": 0.152993, "all_reconciled": true}, {"dimension": "ttc_bucket", "bucket": "<60s", "n": 22, "win_rate": 0.3636, "pnl_usd": -38.9813, "avg_ev_after_cost": 0.133834, "all_reconciled": true}, {"dimension": "hurst_regime", "bucket": "insufficient_data", "n": 6, "win_rate": 0.5, "pnl_usd": 4.7503, "avg_ev_after_cost": 0.126917, "all_reconciled": true}, {"dimension": "zscore_bucket", "bucket": "1..2", "n": 9, "win_rate": 0.2222, "pnl_usd": -33.1764, "avg_ev_after_cost": 0.118428, "all_reconciled": true}, {"dimension": "indicator_name", "bucket": "Hermes BTC Pulse Composite v3 Loose", "n": 31, "win_rate": 0.5806, "pnl_usd": -20.8231, "avg_ev_after_cost": 0.117457, "all_reconciled": true}]`

worst_buckets `[{"dimension": "cvd_state", "bucket": "sell_pressure", "n": 14, "win_rate": 0.7857, "pnl_usd": 23.1349, "avg_ev_after_cost": 0.053807, "all_reconciled": true}, {"dimension": "mtf_alignment", "bucket": "bullish_aligned", "n": 15, "win_rate": 0.4, "pnl_usd": -20.3402, "avg_ev_after_cost": 0.07119, "all_reconciled": true}, {"dimension": "mtf_alignment", "bucket": "neutral", "n": 19, "win_rate": 0.5789, "pnl_usd": -12.8834, "avg_ev_after_cost": 0.074335, "all_reconciled": true}, {"dimension": "hurst_regime", "bucket": "noise", "n": 4, "win_rate": 0.25, "pnl_usd": -11.5254, "avg_ev_after_cost": 0.075067, "all_reconciled": true}, {"dimension": "liquidation_spike", "bucket": "True", "n": 3, "win_rate": 0.6667, "pnl_usd": 1.3405, "avg_ev_after_cost": 0.076747, "all_reconciled": true}]`

rsi_trend hit_rate 0.5178 (n 983) · pred_acc 0.493

## 11. Loop engineering (maker-checker / lessons / loops / research)

**Verifier (independent Claude maker-checker):** `{"enabled": true, "verified": 381, "approvals": 257, "vetoes": 124, "errors": 10, "approve_rate": 0.6745, "approved_acted_settled": {"n": 7, "win_rate": 0.5714, "pnl_usd": 11.1176}, "avg_latency_s": 4.726}`

**Research meta-loop:** `{"enabled": true, "calls": 65, "auto_apply": true, "lessons_added": 305}`

- research summary: 403 settled trades, 53.6% win rate, -$64.81 PnL, 0.89 profit factor. Loses more per loss than wins per win, bleeding slowly. TradingView DOWN signals show 61.6% hit rate (vs 42.3% baseline UP rate), suggesting directional edge for shorts, but overall execution costs and adverse selection erase it.

**Lessons (compounding rules):** count 300
- [`research`] Bucket 0.3-0.4 shows 28.2% empirical_up (n=39) vs 35% expected; model overestimates UP in low-confidence zone—recalibrate or avoid
- [`research`] DOWN trades show 61.6% TradingView signal hit vs 47.8% UP; favor DOWN in signal alignment scoring
- [`research`] 0.3-0.4 confidence bucket shows 28.2% empirical vs 35% expected; recalibrate or avoid this bucket
- [`research`] 245/603 (41%) execution candidates rejected; underdog_price_below_floor (95) and negative_ev (89) dominate—review pricing logic
- [`research`] Avg loss $4.02 vs win $3.09 (30% larger); tighten stops or raise entry bar to fix asymmetry
- [`research`] TradingView DOWN signals hit 61.6% vs 42.3% baseline; UP signals 47.8% (below baseline)—use directionally
- [`research`] TradingView DOWN signals hit 61.6% vs 42.3% base rate; exploit DOWN only, avoid UP
- [`research`] 11 no-signal trades: 36.4% win, -$4.94 PnL; never trade without TradingView signal
- [`research`] Brier 0.226 vs baseline 0.25, but profit factor 0.89; model directionally OK, execution poor
- [`research`] Execution gate correctly blocking 89 trades post-slippage; spread/depth model working

**Sub-loops:** arbitrage, data_ingestion, execution, heartbeat, lessons, news, research_meta, risk_monitor, signal_generation, verifier

## 12. Edge signal & readiness

edge_signal `{"enabled": true, "observe_only": true, "report_only": true, "affects_trading": false, "settled": 165, "by_stale_divergence": {"not_stale": {"n": 144, "win_rate": 0.5903, "pnl_usd": 21.5334, "avg_ev_after_cost": 0.090239, "all_reconciled": true}, "already_priced": {"n": 12, "win_rate": 0.3333, "pnl_usd": -20.3432, "avg_ev_after_cost": 0.166486, "all_reconciled": true}, "stale_polymarket_up": {"n": 4, "win_rate": 0.25, "pnl_usd": -13.6709, "avg_ev_after_cost": 0.136871, "all_reconciled": true}, "stale_polymarket_down": {"n": 5, "win_rate": 0.4, "pnl_usd": -10.0715, "avg_ev_after_cost": 0.103074, "all_reconciled": true}}, "by_ttc_bucket": {"240_300s": {"n": 35, "win_rate": 0.5143, "pnl_usd": 0`

**CEX-lead latency edge** (grades CEX-implied P(up) vs the MARKET price): mode `shadow` · affects_trading False · signals_seen 17850 · graded 292 · drove 0 · any_proven (beats market) **False**
| divergence | n | acc | brier_cex | brier_mkt | beats_mkt | avg_pnl/u | proven |
|---|---|---|---|---|---|---|---|
| >=0.30 | 275 | 0.4727 | 0.4856 | 0.247 | False | -0.0262 | False |
| ttc=>=0.30|240_300s | 219 | 0.4886 | 0.4809 | 0.2478 | False | -0.0094 | False |
| late=>=0.30|indecisive | 195 | 0.4923 | 0.4792 | 0.2494 | False | -0.0053 | False |
| conf=>=0.30|unconfirmed | 134 | 0.5 | 0.4702 | 0.2485 | False | 0.0026 | False |
| tv=>=0.30|unconfirmed | 112 | 0.4821 | 0.4949 | 0.248 | False | -0.0271 | False |
| news=>=0.30|against | 109 | 0.5321 | 0.4448 | 0.2494 | False | 0.0384 | False |
_promotion: n>=min AND wilson_lower(win_rate)>breakeven AND Brier_cex<Brier_market AND avg_pnl>0_

**Within-window risk-free arbitrage** (Roan dutch book `up_vwap+down_vwap<1`; P&L SEGREGATED from directional, never blended): detected_actionable 5 · sell_both_detected 2 · executed 5 · settled 5 · open 0 · realized_profit **$3.452** (risk-free)

readiness `{'report_only': True, 'status': 'not_ready', 'ready_to_claim_80pct': False, 'gates': {'accepted_ge_100': True, 'accepted_ge_500': False, 'accepted_ge_1000': False, 'win_rate_ge_80': False, 'positive_net_paper_pnl': False, 'profit_factor_ok': False, 'calibration_error_ok': True, 'max_drawdown_ok': False, 'loss_size_le_win_size': False, 'no_reconciliation_failures': True, 'no_missing_settlement_data': True, 'no_unmodeled_fill_assumptions': True, 'no_safety_bypass': True}, 'metrics': {'accepted': 403, 'win_rate': 0.536, 'net_pnl_usd': -64.8098, 'profit_factor': 0.8866, 'calibration_error': 0.0571, 'max_drawdown_usd': 151.7258, 'avg_win_usd': 3.0862, 'avg_loss_usd': 4.0207}}`

## 13. Recent paper positions

| window | side | entry_mode | entry | fair | outcome | won | pnl |
|---|---|---|---|---|---|---|---|
| , 1:00AM-1:05AM ET | up | standard | 0.63 | 0.7457857511299943 | up | ✓ | 2.936508 |
|  12:55AM-1:00AM ET | up | standard | 0.6 | 0.6844285624001554 | down | ✗ | -5.0 |
| 12:50AM-12:55AM ET | up | standard | 0.5 | 0.5942082714783411 | down | ✗ | -5.0 |
| 12:40AM-12:45AM ET | up | standard | 0.5 | 0.5804555027267575 | down | ✗ | -5.0 |
| 12:35AM-12:40AM ET | up | standard | 0.5 | 0.598560412850169 | down | ✗ | -5.0 |
| 12:30AM-12:35AM ET | up | late_window | 0.5 | 0.9999999999995102 | up | ✓ | 5.0 |
| 12:20AM-12:25AM ET | down | standard | 0.51 | 0.14763123529213062 | down | ✓ | 4.803922 |
| 12:15AM-12:20AM ET | down | standard | 0.5 | 0.4399333329251137 | down | ✓ | 5.0 |
| 12:00AM-12:05AM ET | down | standard | 0.64 | 0.19898100294924653 | down | ✓ | 2.8125 |
| 11:55PM-12:00AM ET | down | standard | 0.58 | 0.3407095357235647 | down | ✓ | 3.62069 |
| 11:45PM-11:50PM ET | up | standard | 0.66 | 0.8054421398523738 | down | ✗ | -5.0 |
| 11:40PM-11:45PM ET | up | standard | 0.5 | 0.6036015459023512 | down | ✗ | -5.0 |
| 11:35PM-11:40PM ET | up | standard | 0.69 | 0.8886431759841826 | up | ✓ | 2.246377 |
| 11:25PM-11:30PM ET | up | standard | 0.63 | 0.8019331704160089 | up | ✓ | 2.936508 |
| 10:45PM-10:50PM ET | down | standard | 0.57 | 0.2849508918695882 | up | ✗ | -5.0 |
