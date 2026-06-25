# BTC 5-Minute Pulse — Performance Report

_PAPER ONLY · `global_reconciled=True` · ticks 22_


## 1. Trading Performance

| metric | value |
|---|---|
| Total on-hand | $537.84 |
| Directional on-hand | $503.38 |
| Starting capital | $500.0 |
| Total return | 7.57% |
| Directional PnL | $3.38 |
| Arb PnL (segregated) | $34.46 |
| Total PnL | $37.84 |
| Trades / settled | 29 / 29 |
| Win rate | 0.6897 |
| Win rate up / down | 0.5 / 0.7895 |
| Profit factor | 1.0752 |
| Avg win / avg loss | $2.4192 / $5.0 |
| Max drawdown | $23.6305 |
| Avg PnL/trade | 0.1167 |
| EV before/after cost | 0.166438 / 0.160784 |

### Risk-free arbitrage

- **executed:** 4
- **settled:** 4
- **open:** 0
- **realized_profit_usd:** 34.4564
- **detected_actionable:** 4
- **segregated_from_directional:** True

### Accounting integrity

- **global_reconciled:** True
- **scope_note:** lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- **rejected_before_execution:** 4622

### Execution gate & calibration

candidates 43 · accepted 29 · rejects `{'wide_spread': 1, 'insufficient_depth': 0, 'negative_ev_after_slippage': 0, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 0, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0, 'underdog_price_below_floor': 13}`

calibration `{'samples': 29, 'brier': 0.191124, 'log_loss': 0.549514, 'base_rate_up': 0.3103, 'baseline_brier_0_5': 0.25}`

### PnL by bucket

_no bucket PnL yet_

### Selectivity impact on performance

counterfactual `{'replayed': 29, 'trades_rejected': 0, 'losses_avoided': 0, 'pnl_removed_by_rejects': 0.0, 'counterfactual_trades': 29, 'counterfactual_win_rate': 0.6897, 'counterfactual_pnl_usd': 3.3833, 'baseline_trades': 29, 'baseline_win_rate': 0.6897, 'baseline_pnl_usd': 3.3833, 'reject_reasons_by_bucket': {}, 'note': 'in-sample replay using final accumulated bucket evidence (diagnostic estimate)'}`

### Recent positions

| window | side | entry_mode | entry | fair | outcome | won | pnl |
|---|---|---|---|---|---|---|---|
| , 8:05AM-8:10AM ET | up | late_window | 0.72 | 0.8328718228109104 | down | ✗ | -5.0 |
| , 8:00AM-8:05AM ET | down | standard | 0.72 | 0.18240622115358818 | down | ✓ | 1.944444 |
| , 7:50AM-7:55AM ET | up | standard | 0.7 | 0.9030710586046534 | up | ✓ | 2.142857 |
| , 7:45AM-7:50AM ET | down | standard | 0.62 | 0.2251145883614551 | down | ✓ | 3.064516 |
| , 7:30AM-7:35AM ET | up | standard | 0.64 | 0.7472949242068506 | up | ✓ | 2.8125 |
| , 7:05AM-7:10AM ET | down | standard | 0.59 | 0.29264451498117605 | down | ✓ | 3.474576 |
| , 7:00AM-7:05AM ET | down | late_window | 0.72 | 0.1619836960000252 | down | ✓ | 1.944444 |
| , 6:55AM-7:00AM ET | down | standard | 0.6 | 0.16445391935970327 | down | ✓ | 3.333333 |
| , 6:50AM-6:55AM ET | up | late_window | 0.68 | 0.8403378609050882 | up | ✓ | 2.352941 |
| , 6:45AM-6:50AM ET | down | late_window | 0.77 | 0.04894328884259325 | down | ✓ | 1.493506 |
| , 6:35AM-6:40AM ET | down | standard | 0.62 | 0.29680232882896285 | down | ✓ | 3.064516 |
| , 6:30AM-6:35AM ET | up | standard | 0.53 | 0.620297710082628 | down | ✗ | -5.0 |
| , 6:25AM-6:30AM ET | up | standard | 0.51 | 0.6842232752648798 | down | ✗ | -5.0 |
| , 6:10AM-6:15AM ET | down | standard | 0.59 | 0.1263368085175739 | up | ✗ | -5.0 |
| , 6:00AM-6:05AM ET | up | standard | 0.57 | 0.7509556645610314 | down | ✗ | -5.0 |

## 2. Operation


### Engine health

- **ticks:** 22
- **global_reconciled:** True
- **paper_only:** True
- **live_trading_enabled:** False
- **sample_sizes:** `{"accepted": 29, "settled": 29, "candidates": 7016, "edge_model_labeled": 29}`
- **status:** not_ready
- **reason:** None
- **checks:** None

### Candidate lifecycle

created 7016 · terminals `{'accepted': 29, 'rejected': 6380, 'skipped': 590, 'expired': 0, 'missing_data': 17}`

rejected_by_stage `{'directional': 4015, 'execution_gate': 14, 'context_gate': 480, 'directional_allowlist': 1324, 'down_bias_gate': 501, 'mtf_gate': 46}`

### Looping engine (sub-loops)

| loop | role | trigger | interval_s | stop | status |
|---|---|---|---|---|---|
| arbitrage | risk_free_arb | per_window | None | ok | — |
| data_ingestion | data | tick | None | None | True |
| directional | strategy | per_window | None | warming_up(n<30) | True |
| execution | execute | per_decision | None | fill or reject | — |
| heartbeat | automation | tick | 4.0 | process running | — |
| lessons | memory | per_settlement | None | None | — |
| news | context | interval | 300.0 | None | — |
| research_meta | research(/goal) | interval | 1800.0 | verifiable metric improvement | — |
| risk_monitor | risk | per_settlement | None | None | — |
| signal_generation | signal | per_window | None | None | True |
| verifier | verify(maker-checker) | per_decision | None | approve/veto verdict | — |

### Maker-checker verifier

- **enabled:** False
- **verified:** None
- **approvals:** None
- **vetoes:** None
- **errors:** None
- **approve_rate:** None
- **avg_latency_s:** None

### Research meta-loop

- **enabled:** False
- **calls:** None
- **auto_apply:** None
- **lessons_added:** None

### Compounding lessons

count 0

### Internal gates & allowlist

- **decision_rule:** confidently_below_breakeven
- **accepted:** 43
- **rejected:** 0
- **explored:** 0
- **block_reasons:** None
- **enabled:** True
- **explore_rate:** 0.0
- **explored:** 43
- **blocked:** 1324
- **enabled:** False
- **active:** False
- **weight:** 0.0
- **reason:** disabled
- **enabled:** None
- **halted_directional:** None
- **halted_arbitrage:** None
- **rolling_profit_factor:** None
- **rolling_win_rate:** None

### Grok decider (operations)

- **mode:** shadow
- **affects_trading:** False
- **decided:** 87
- **errors:** 2
- **avg_latency_s:** 6.6
- **abstains:** 60
- **circuit_breaker:** `{"tripped": false, "reason": null, "consecutive_losses": 0, "daily_follow_loss_usd": 0.0, "daily_loss_cap_usd": 30.0, "trips": 0, "cooldown_remaining_s": 0, "max_consecutive_losses": 4, "max_latency_s": 20.0}`

## 3. External Signals


### Signal impact on trading performance

| signal | value |
|---|---|
| TV aligned bot WR | 0.8889 |
| TV opposed bot WR | 0.5556 |
| TV signal hit-rate | 0.6667 |
| TV settled w/ signal | 18 |
| TV edge verdict | insufficient_evidence |
| Grok direction accuracy | 0.56 |
| Grok view accuracy | 0.5059 |
| CEX-lead proven edge | False |

### TradingView

- **tradingview_alerts_received:** 235
- **tradingview_alerts_valid:** 224
- **tradingview_alerts_rejected:** 11
- **tradingview_mtf_confirmation:** `{"symbol": "BTCUSDT", "tf_1m_dir": "DOWN", "tf_5m_dir": null, "tf_1m_age_s": 259.5, "tf_5m_age_s": 672.2, "confirm": "single_tf", "direction": "DOWN"}`

settled_with_signal 18

best_buckets `[{"dimension": "cvd_state", "bucket": "buy_pressure", "n": 4, "win_rate": 0.5, "pnl_usd": -5.2431, "avg_ev_after_cost": 0.21195, "all_reconciled": true}, {"dimension": "direction", "bucket": "UP", "n": 3, "win_rate": 0.6667, "pnl_usd": 0.2231, "avg_ev_after_cost": 0.20738, "all_reconciled": true}, {"dimension": "supertrend_direction", "bucket": "bullish", "n": 3, "win_rate": 0.6667, "pnl_usd": 0.2231, "avg_ev_after_cost": 0.20738, "all_reconciled": true}, {"dimension": "mtf_alignment", "bucket": "mixed", "n": 8, "win_rate": 0.625, "pnl_usd": -2.8679, "avg_ev_after_cost": 0.206063, "all_reconciled": true}, {"dimension": "ttc_bucket", "bucket": "120-240s", "n": 9, "win_rate": 0.8889, "pnl_usd": 17.78, "avg_ev_after_cost": 0.204727, "all_reconciled": true}]`

worst_buckets `[{"dimension": "htf_bias", "bucket": "bearish", "n": 8, "win_rate": 0.875, "pnl_usd": 13.7167, "avg_ev_after_cost": 0.143338, "all_reconciled": true}, {"dimension": "vwap_state", "bucket": "below", "n": 8, "win_rate": 0.875, "pnl_usd": 13.7167, "avg_ev_after_cost": 0.143338, "all_reconciled": true}, {"dimension": "range_state", "bucket": "range_middle", "n": 5, "win_rate": 0.8, "pnl_usd": 3.9478, "avg_ev_after_cost": 0.1455, "all_reconciled": true}, {"dimension": "mtf_alignment", "bucket": "bearish_aligned", "n": 7, "win_rate": 1.0, "pnl_usd": 18.7167, "avg_ev_after_cost": 0.147686, "all_reconciled": true}, {"dimension": "cvd_state", "bucket": "neutral", "n": 6, "win_rate": 0.5, "pnl_usd": -8.4635, "avg_ev_after_cost": 0.153917, "all_reconciled": true}]`

rsi_trend hit_rate 0.4888 (n 223)

**context_gate:** enabled=True blocked=480 explored=27 `{'tv_context_ttc_too_far': 407, 'tv_context_hurst_noise': 73}`

**down_bias_gate:** enabled=True blocked=501 explored=15 `{'tv_down_bias_up_without_bearish': 501, 'tv_down_bias_bullish_aligned_up': 228}`

**mtf_gate:** enabled=True blocked=46 explored=0 `{'tv_mtf_opposes_side': 17, 'tv_mtf_single_tf_only': 29}`

**signal_gate:** enabled=False blocked=None explored=None `None`

### Grok Decision Engine (signal quality)

- **mode:** shadow
- **affects_trading:** False
- **direction_accuracy:** 0.56
- **brier:** 0.2411
- **view_accuracy:** 0.5059
- **view_brier:** 0.2499
- **views_graded:** 85
- **view_edge_candidates:** `[]`

accuracy_by_context `{"hurst_regime": {"insufficient_data": {"n": 9, "accuracy": 0.6667}, "trending": {"n": 73, "accuracy": 0.4795}, "noise": {"n": 3, "accuracy": 0.6667}}, "markov_state": {"stale_polymarket_up": {"n": 25, "accuracy": 0.52}, "stale_polymarket_down": {"n": 26, "accuracy": 0.5385}, "chop_noise": {"n": 34, "accuracy": 0.4706}}, "ttc_bucket": {">=240s": {"n": 85, "accuracy": 0.5059}}, "conviction_bucket": {"coinflip": {"n": 85, "accuracy": 0.5059}}}`

recent_decisions `[{"action": "no_trade", "p_up": 0.47, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_down", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.4, "confidence": 0.0, "outcome_up": true, "view_correct": false, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_down", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.47, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "trending", "markov_state": "stale_polymarket_down", "ttc_bucket": ">=240s", "conviction_bucket": "coinflip"}}, {"action": "no_trade", "p_up": 0.49, "confidence": 0.0, "outcome_up": false, "view_correct": true, "context": {"hurst_regime": "insufficient_data", "markov_state": "chop_noise", "ttc_`

### Grok signal intel (analyst + predictor)

budget `{'daily_usd_cap': 5.0, 'est_usd_per_call': 0.02, 'spent_today_usd': 0.06, 'calls_today': 3, 'per_feature_hourly': {'predictor': 30, 'analyst': 4, 'overlay': 20, 'decider': 60, 'news': 30}}`

predictor_B `{'enabled': True, 'observe_only': True, 'affects_trading': False, 'off_hot_path': True, 'requested': 224, 'predicted': 140, 'errors': 0, 'skipped_budget': 84, 'scored': 131, 'accuracy': 0.4962, 'brier': 0.2518, 'pending': 0, 'note': 'observe-only Grok P(up) per signal; graded vs realized BTC move before it could ever be trusted; never places/sizes/bypasses a trade.'}`

analyst_A last_note `{"summary": "With only 18 settled trades (all DOWN-heavy), overall win-rate 0.722 and positive EV after costs, but n<8 in most sub-buckets means almost nothing is yet confirmed; the few n>=8 slices that clear the bar (DOWN, 120-240s TTC, below VWAP, normal BB, sell-pressure CVD, strong ADX trend, bearish HTF) show positive realized PnL and EV. No prior analysis exists so all patterns are new.", "working": ["direction=DOWN (n=15, WR=0.733, +8.9 PnL)", "ttc=120-240s (n=9, WR=0.889, +17.8 PnL)", "vwap_state=below (n=8, WR=0.875, +13.7 PnL)", "bb_state=normal (n=9, WR=1.0, +21.2 PnL)", "cvd_state=sell_pressure (n=8, WR=1.0, +22.8 PnL)", "adx_state=strong_trend (n=13, WR=0.769, +11.0 PnL)"], "failing": ["zscore=-1..1 (n=13, negative PnL)", "vwap_state=above (n=10, negative PnL)", "mtf_alignment=mixed (n=8, negative PnL)", "bb_state=expansion_down/expansion_up (negative PnL)"], "warnings": ["total n=18 is tiny; every bucket except the top-level aggregates has n<8 and must be ignored", "observe-only mode, no promotion possible", "all trades in trending regime and neutral funding only"], "changes_since_last": ["first analysis; no prior baseline"], "focus_next": ["accumulate samples specifi`

### CEX-lead latency edge

- **mode:** shadow
- **affects_trading:** False
- **signals_seen:** 5942
- **graded:** 108
- **drove_entries:** 0
- **any_proven:** False
| divergence | n | acc | beats_mkt | avg_pnl/u | proven |
|---|---|---|---|---|---|
| >=0.30 | 107 | 0.4673 | False | -0.0256 | False |
| ttc=>=0.30|240_300s | 107 | 0.4673 | False | -0.0256 | False |
| news=>=0.30|neutral | 107 | 0.4673 | False | -0.0256 | False |
| late=>=0.30|indecisive | 107 | 0.4673 | False | -0.0256 | False |
| tv=>=0.30|unconfirmed | 79 | 0.5063 | False | 0.0101 | False |
| conf=>=0.30|unconfirmed | 62 | 0.4516 | False | -0.0467 | False |

### Pulse edge signal

`{"enabled": true, "observe_only": true, "report_only": true, "affects_trading": false, "settled": 29, "by_stale_divergence": {"not_stale": {"n": 27, "win_rate": 0.6667, "pnl_usd": 0.0405, "avg_ev_after_cost": 0.161824, "all_reconciled": true}, "already_priced": {"n": 1, "win_rate": 1.0, "pnl_usd": 1.8493, "avg_ev_after_cost": 0.1124, "all_reconciled": true}, "stale_polymarket_up": {"n": 1, "win_rate": 1.0, "pnl_usd": 1.4935, "avg_ev_after_cost": 0.1811, "all_reconciled": true}}, "by_ttc_bucket": {"180_240s": {"n": 10, "win_rate": 0.9, "pnl_usd": 19.8638, "avg_ev_after_cost": 0.1817, "all_reconciled": true}, "90_180s": {"n": 11, "win_rate": 0.5455, "pnl_usd": -12.8972, "avg_ev_after_cost": 0.166622, "all_reconciled": true}, "30_90s": {"n": 6, "win_rate": 0.6667, "pnl_usd": -2.0579, "avg_ev_`

### DOWN stack grader

`{"observe_only": true, "affects_trading": false, "min_samples": 30, "edge_margin": 0.04, "buckets": [{"bucket": "bearish_only", "n": 7, "win_rate": 1.0, "wilson_lower": 0.7224, "avg_entry": 0.6557, "breakeven_wr": 0.6557, "pnl_usd": 18.7167, "proven": false}, {"bucket": "other", "n": 22, "win_rate": 0.5909, "wilson_lower": 0.4184, "avg_entry": 0.6502, "breakeven_wr": 0.6502, "pnl_usd": -15.3333, "proven": false}], "any_proven": false, "proven_buckets": [], "promotion_rule": "n>=30 AND wilson_lower>avg_entry+0.04 AND pnl>0"}`
