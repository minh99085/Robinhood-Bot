# BTC 5-Minute Pulse — Full Report

_Generated 2026-06-22 17:13 UTC from live VPS `hermes-training` (PAPER ONLY)._

**Mode:** paper_only, live_trading_enabled=False, **global_reconciled=true** · ticks 1681.

## 1. Paper P&L (cumulative)

| Metric | Value |
|---|---|
| Trades / settled | 290 / 290 |
| Win rate | 52.8% |
| Realized PnL | $-76.8008 |
| Profit factor | 0.8165 |
| EV after costs (avg) | 0.107746 over 139 |

## 2. Accounting integrity
`global_reconciled=true`. lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted. Calibration Brier 0.232602 (n 290).

## 3. Candidate lifecycle
created 16329 · accepted 139 · rejected 15536 · skipped 635 · missing_data 19
**rejected_by_stage:** `{'directional': 14619, 'execution_gate': 0, 'selectivity_gate': 727, 'context_gate': 190}`

## 4. Learned Selectivity Gate (breakeven+confidence)
rule `confidently_below_breakeven` z=1.64 · accepted 1 · rejected 727 · explored 38.

**Currently blocked (confidently below own breakeven):**
| dim=bucket | n | WR | breakeven | upperCI | EV/trade |
|---|---|---|---|---|---|
| markov_state=stale_polymarket_up | 64 | 0.5312 | 0.6306 | 0.6302 | $-0.7884 |
| direction=down | 127 | 0.4961 | 0.5802 | 0.5681 | $-0.7244 |
| spread_bucket=<=0.01 | 210 | 0.5095 | 0.5666 | 0.5656 | $-0.5038 |

_`hurst_regime=trending` is NO LONGER blocked (coin-flip near breakeven, not confidently losing) — the overblocking fix is working._

counterfactual: baseline WR 0.5121 / $-121.3983 → rejects 203, avoids 100 losses.

## 5. Reward-to-risk floor
PULSE_MIN_REWARD_RISK=0.25 (skip price>~0.80 / win<~$1.25 per $5).

## 6. TradingView Context Gate (LIVE)
enabled=True · passed 284 · blocked 190 · explored 8 · block_reasons `{'tv_context_volume_spike': 111, 'tv_context_ttc_too_far': 58, 'tv_context_hurst_noise': 21}`

## 7. Late-window high-conviction (time-decay edge)
gate enabled=False (measuring) · verdict **insufficient_evidence** · LHC `{'n': 3, 'win_rate': 0.0, 'pnl_usd': -15.0, 'avg_pnl_usd': -5.0, 'avg_ev_after_cost': 0.077195}` · other `{'n': 9, 'win_rate': 0.6667, 'pnl_usd': 6.6119, 'avg_pnl_usd': 0.7347, 'avg_ev_after_cost': 0.110468}`

## 8. TV signal gate freshness
active=True · max_signal_age_s=600.0 (eased so a 5m alert covers the gap to the next).

## 9. Grok intel (learns bot patterns, $20 coverage)
budget $1.18/$20.0 (59 calls, 0 errors). Analyst A calls 30, learns_from `bot_growing_evidence_with_continuity`, history 4.

- A focus_next: ['accumulate samples specifically in DOWN_WEAK + active volume + mixed mtf + zscore -1..1 intersections', 'track EV stability and max drawdown per bucket as n grows past 30']

- Predictor B: 121 predicted, accuracy 0.4956, Brier 0.2555 (still ~coin-flip → observe-only).

## 10. TradingView learning (incl v4 order-flow/event)
received 123 · valid 123 · rejected 0 · settled_with_signal 43.

best: [('ttc_bucket', '<60s', 5, 0.0), ('htf_bias', 'bearish', 5, 0.4), ('vwap_state', 'below', 14, 0.5714), ('signal_level', 'DOWN_WEAK', 11, 0.7273)]
worst: [('vwap_state', 'reclaim', 3, 0.6667), ('range_state', 'range_top', 11, 0.2727), ('mtf_alignment', 'neutral', 7, 0.7143), ('zscore_bucket', '-2..-1', 9, 0.4444)]

## 11. Readiness
status `not_ready`, ready_to_claim_80pct=False. demotion_candidates `['edge_quality:medium', 'regime:trending', 'zscore_bucket:1..2', 'ttc_bucket:<60s']`.

