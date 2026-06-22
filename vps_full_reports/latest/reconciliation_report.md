# Reconciliation Report

_Generated 2026-06-22 17:13 UTC (PAPER ONLY)._

**global_reconciled: true**

- global_reconciled: True
- scope_note: lifecycle counts are cumulative since canonical accounting began; baseline counts are legacy ledger totals that predate it; ledger/gate totals == baseline + accounted.
- rejected_before_execution: 15273

## Lifecycle terminals
`{'accepted': 139, 'rejected': 15536, 'skipped': 635, 'expired': 0, 'missing_data': 19}`

## rejected_by_stage
`{'directional': 14619, 'execution_gate': 0, 'selectivity_gate': 727, 'context_gate': 190}`

## Execution gate rejects
`{'wide_spread': 0, 'insufficient_depth': 0, 'negative_ev_after_slippage': 0, 'too_close_to_resolution': 0, 'min_size_or_tick_violation': 0, 'partial_fill_risk': 0, 'missing_market_data': 0, 'stale_orderbook': 0}`

## Ledger
trades 290 · settled 290 · win_rate 0.5276 · realized_pnl $-76.8008 · settle_sources {'polymarket_resolution': 129, 'rtds_chainlink_proxy': 145} · proxy_official {'both': 128, 'agree': 122, 'disagree': 6}

