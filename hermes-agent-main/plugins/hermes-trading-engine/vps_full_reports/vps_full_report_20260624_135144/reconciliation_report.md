# Reconciliation report (PAPER ONLY)

- global_reconciled: True
- counts: {
  "raw_candidates_created": 51438,
  "rejected_before_execution": 39886,
  "sent_to_execution_gate": 497,
  "execution_gate_accepted": 252,
  "execution_gate_rejected": 245,
  "paper_fills_created": 252,
  "ledger_trades": 403,
  "settled_trades": 403,
  "open_positions": 0,
  "legacy_trades_before_accounting": 151,
  "legacy_exec_candidates_before_accounting": 106
}
- execution_gate reconciled: True candidates 603 accepted 358 rejected 245
- ledger trades 403 settled 403 win_rate 0.536 pnl -64.8098 PF 0.8866
- arbitrage (SEGREGATED): {"strategy": "within_window_arbitrage", "paper_only": true, "risk_free": true, "segregated_from_directional": true, "detected_actionable": 5, "sell_both_detected": 2, "executed": 5, "settled": 5, "open": 0, "realized_profit_usd": 3.452, "guaranteed_booked_usd": 3.452, "note": "risk-free dutch book (up_vwap+down_vwap<1-fees-eps); deterministic P&L; NEVER blended into directional win-rate/profit-factor. PAPER ONLY."}
