# BTC 5-Minute Pulse — Full Report

_Generated 2026-06-22 09:46 UTC from live VPS container `hermes-training` (PAPER ONLY)._

**Mode:** `paper_only=True`, `live_trading_enabled=False`, **`global_reconciled=True`** · ticks 3707 · oracle `chainlink_data_streams_refprice` (RTDS connected=True).

## 1. Paper P&L (cumulative)
| Metric | Value |
|---|---|
| Trades / settled | 272 / 272 |
| Win rate | 53.3% |
| Realized PnL | $-55.68 |
| Profit factor | 0.841 |
| Avg win / avg loss | $2.79 / $3.78 |
| Max drawdown | $117.63 |
| Open | 0 |

## 2. Accounting integrity (reconciliation)
`global_reconciled=true`, failed_checks: none. ledger_trades 272 = legacy 151 + accounted; settled 272 + open 0 = 272. Calibration Brier 0.2372 (samples 272).

## 3. Learned Selectivity Gate v1 (the new selectivity fix)
- enabled=True · **accepted=0** · **rejected=361** · **explored=21** (exploration capped 5%, tracked separately).
- reject reasons: `{"bad_bucket:ttc_bucket=>=240s": 3, "bad_bucket:confidence_tier=high": 7, "bad_bucket:zscore_bucket=-1..1": 8, "bad_bucket:confidence_tier=medium": 1, "bad_bucket:hurst_regime=trending": 338, "bad_bucket:ttc_bucket=120-240s": 4}`
- PnL by gate decision: `{"passed": {"n": 1, "win_rate": 0.0, "pnl_usd": -5.0}, "explored": {"n": 21, "win_rate": 0.7143, "pnl_usd": 5.1536}}`
- **Counterfactual** over 221 settled trades: would reject **221**, avoid **106** losses → counterfactual trades 0, PnL **$0** vs baseline win 0.5204 / PnL **$-99.7153**.

**Read:** the gate is now rejecting ~all new candidates (dominant bad bucket `hurst_regime=trending`), i.e. it has effectively paused the bleed; the only new trades are the 21 exploration probes (win 0.7143, pnl $5.1536). No profitable bucket exists in the current data → not trading beats trading.

## 4. TradingView intake (post-reset)
received 52 · valid 52 · rejected 0 · signal_learning settled 25 · webhook listening True.

## 5. Edge signal (CEX basket / stale divergence)
snapshots 6149 · settled 34 · CEX coverage ['binance_btcusdt', 'coinbase_btcusd', 'kraken_btcusd', 'bitstamp_btcusd'].
