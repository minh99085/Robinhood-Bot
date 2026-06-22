# BTC 5-Minute Pulse — Full Report

_Generated 2026-06-22 14:09 UTC from live VPS container `hermes-training` (PAPER ONLY)._

**Mode:** `paper_only=True`, `live_trading_enabled=False`, **`global_reconciled=true`** · ticks 29.

## 1. Paper P&L (cumulative)

| Metric | Value |
|---|---|
| Trades / settled | 283 / 283 |
| Win rate | 52.6% |
| Realized PnL | $-72.7667 |
| Wins / losses | 149 / 134 |
| EV after costs (avg) | 0.107694 |

## 2. Accounting integrity
`global_reconciled=true`. lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.

## 3. Candidate lifecycle
created 13790 · accepted 132 · rejected 13114 · skipped 527 · missing_data 17

**rejected_by_stage:** `{'directional': 12472, 'execution_gate': 0, 'selectivity_gate': 557, 'context_gate': 85}`

## 4. Learned Selectivity Gate (FIXED: breakeven + confidence)

rule `confidently_below_breakeven` (confidence_z 1.64). accepted 0 · rejected 557 · explored 32.

Auditable bucket evidence (blocked iff confidently below its own breakeven):

| dim | bucket | n | WR | breakeven | WR_upperCI | EV/trade | blocked |
|---|---|---|---|---|---|---|---|
| ttc_bucket | 120-240s | 50 | 0.52 | 0.6401 | 0.6319 | -0.9382 | True |
| markov_state | stale_polymarket_up | 64 | 0.5312 | 0.6306 | 0.6302 | -0.7884 | True |
| direction | down | 121 | 0.4876 | 0.5762 | 0.5616 | -0.7687 | True |
| markov_state | chop_noise | 78 | 0.4231 | 0.4729 | 0.5159 | -0.5263 | False |
| spread_bucket | <=0.01 | 203 | 0.5074 | 0.5639 | 0.5645 | -0.501 | False |
| confidence_tier | high | 160 | 0.525 | 0.5827 | 0.5888 | -0.4953 | False |
| zscore_bucket | -1..1 | 115 | 0.5043 | 0.5596 | 0.5798 | -0.494 | False |
| edge_quality_bucket | high | 213 | 0.5117 | 0.5622 | 0.5674 | -0.4488 | False |

_Note: `hurst_regime=trending` is no longer hard-vetoed — it is a near-breakeven coin-flip, not confidently losing. Only confidently-losing buckets (e.g. `direction=down`, far-ttc) are blocked._

counterfactual: baseline WR 0.51 / PnL $-117.3642 → would reject 169, avoid 84 losses.

## 5. Reward-to-risk floor
PULSE_MIN_REWARD_RISK=0.25 (skip entries priced > ~0.80 / win < ~$1.25 per $5). reason `reward_risk_too_low`.

## 6. TradingView Context Gate (restrict-only, LIVE)
enabled=True · passed 110 · blocked 85 · explored 5 · block_reasons `{'tv_context_volume_spike': 67, 'tv_context_ttc_too_far': 18}`

## 7. Late-window high-conviction entry (time-decay edge)
gate enabled=False (measuring) · verdict **insufficient_evidence** · cohort_late_high_conviction `{'n': 1, 'win_rate': 0.0, 'pnl_usd': -5.0, 'avg_pnl_usd': -5.0, 'avg_ev_after_cost': 0.093194}` · cohort_other `{'n': 4, 'win_rate': 0.5, 'pnl_usd': 0.646, 'avg_pnl_usd': 0.1615, 'avg_ev_after_cost': 0.092874}`

## 8. Grok intel (full $20 coverage)
budget $0.04/$20.0 today (2 calls, 0 errors). Predictor B accuracy 0.5056 Brier 0.2556 (n 89). Analyst A calls 24.

## 9. TradingView learning (incl. v4 order-flow/event schema)
received 94 · valid 94 · rejected 0 · settled_with_signal 36.

observe-only v4 fields tracked (awaiting alert data): {'cvd_state': 'n=5', 'funding_state': 'n=5', 'liquidation_spike': 'n=5', 'event_blackout': 'n=5'}

## 10. Readiness
status `not_ready`, ready_to_claim_80pct=False.

