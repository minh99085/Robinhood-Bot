# Reconciliation Report

_Generated 2026-06-22 14:09 UTC (PAPER ONLY)._

**global_reconciled: true**

- global_reconciled: True
- scope_note: lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- rejected_before_execution: 13016

## Lifecycle terminals
`{'accepted': 132, 'rejected': 13114, 'skipped': 527, 'expired': 0, 'missing_data': 17}`

## rejected_by_stage
`{'directional': 12472, 'execution_gate': 0, 'selectivity_gate': 557, 'context_gate': 85}`

## Execution gate rejects
`{'wide_spread': 0, 'insufficient_depth': 0, 'negative_ev_after_slippage': 0, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 0, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0}`

## Ledger
trades 283 · settled 283 · win_rate 0.5265 · realized_pnl $-72.7667 · settle_sources {'polymarket_resolution': 125, 'rtds_chainlink_proxy': 142} · proxy_official {'both': 124, 'agree': 118, 'disagree': 6}

