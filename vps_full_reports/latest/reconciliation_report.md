# BTC 5-Minute Pulse — Reconciliation Verification

_Generated 2026-06-21 14:24 UTC from live VPS container `hermes-training` (PAPER ONLY)._

## Global reconciliation: **TRUE**  (schema `btc_pulse_light_report/1.1`)
Failed checks: `none`

### Explicit count taxonomy
| Count | Value |
|---|---|
| raw candidates created | 10 |
| rejected before execution | 9 |
| sent to execution gate | 1 |
| execution-gate accepted | 1 |
| execution-gate rejected | 0 |
| paper fills created | 1 |
| ledger trades | 152 |
| settled trades | 151 |
| open positions | 1 |
| legacy trades before accounting | 151 |
| legacy exec candidates before accounting | 106 |

### Identity checks
- PASS `lifecycle_internal`: created(10) == sum(terminals)(10) and reported(10) == created(10)
- PASS `accepted_equals_fills`: paper_fills(1) == execution_gate_accepted(1)
- PASS `gate_internal`: gate_candidates(107) == accepted(107)+rejected(0) and fills(107)==accepted(107)
- PASS `gate_flow_matches_ledger`: ledger gate_candidates(107) == baseline(106)+sent_to_gate(1); gate_accepted(107) == baseline(106)+accepted(1)
- PASS `ledger_trades_explained`: ledger_trades(152) == legacy(151) + paper_fills(1)
- PASS `positions_balance`: settled(151) + open(1) == ledger_trades(152)

### Zero-reject diagnostic: active=True
- execution gate rejected 0 of 107 candidates that reached it — verify this is liquidity reality, not a disabled gate
- thresholds: `{"size_usd": 5.0, "max_spread": 0.06, "min_depth_usd": 1.0, "min_order_usd": 1.0, "max_depth_consume_frac": 0.5, "min_ev_after_slippage": 0.0, "min_seconds_to_close": 4.0, "max_book_age_s": 30.0}`
- observed ranges: `{"spread": {"min": 0.01, "max": 0.01, "mean": 0.01, "n": 1}, "ask_depth_usd": {"min": 33275.88, "max": 33275.88, "mean": 33275.88, "n": 1}, "slippage": {"min": 0.000784, "max": 0.000784, "mean": 0.000784, "n": 1}, "ev_after_slippage": {"min": 0.068367, "max": 0.068367, "mean": 0.068367, "n": 1}, "ttc_s": {"min": 261.318686, "max": 261.318686, "mean": 261.318686, "n": 1}}`
- Polymarket BTC 5m books are tight + deep, so spread/depth/partial-fill checks pass
- VWAP slippage over the top of book is tiny at $5.0 size, so EV-after-cost stays positive
- the directional stage already rejected 9 candidates before the gate, so only execution-feasible candidates reached it
