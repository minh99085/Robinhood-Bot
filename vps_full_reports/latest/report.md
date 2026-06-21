# BTC 5-Minute Pulse — Full Report

_Generated 2026-06-21 21:11 UTC from live VPS container `hermes-training`._

**Mode:** PAPER ONLY — `live_trading_enabled=False`, `paper_only=True`. **`global_reconciled=True`** (schema `btc_pulse_light_report/1.1`).

## 1. Accounting integrity (reconciliation)
Failed checks: `none`
| Count | Value |
|---|---|
| raw candidates created | 1191 |
| rejected before execution | 1113 |
| sent to execution gate | 78 |
| execution-gate accepted | 78 |
| execution-gate rejected | 0 |
| paper fills created | 78 |
| ledger trades | 229 |
| settled / open | 229 / 0 |
| legacy trades (pre-accounting) | 151 |

## 2. Paper P&L (cumulative)
| Metric | Value |
|---|---|
| Trades / settled | 229 / 229 |
| Win rate | 52.4% |
| Realized PnL | $-30.93 |
| Profit factor | 0.868 |
| Avg win / avg loss | $2.82 / $3.58 |
| Max drawdown | $90.70 |
| Open positions | 0 |

## 3. Engine + oracle
- Ticks this run: 90 · price source `rtds_chainlink` (last 63529.71)
- Oracle `chainlink_data_streams_refprice` · RTDS connected=True (705 msgs)
- Settlement: official 99 + proxy 114; proxy/official recon {"both": 98, "agree": 94, "disagree": 4}
- Execution gate: 184 candidates, 184 accepted, 0 rejected
- Calibration: Brier 0.2419 vs 0.25 baseline · log-loss 0.6746

## 4. Closed-loop learning (the bot's own experience adjusting decisions)
- enabled=True · **active=False** · weight=0.0 · reason=`insufficient_samples`
- model labels=15 (needs 60 to start) · calibration_error=None
- _Cold-start: it records every settled trade and will begin nudging decisions once it has ~60 calibrated samples — then weight ramps up to 0.5._

## 5. TradingView TA (observe-only intake + directional gate)
- Intake (after residue cleanup): received=0, valid=0, by_symbol={}
- **Directional gate active=True** (restrict-only): a paper trade requires a fresh aligned TradingView signal.
- Edge vs 5-min outcome: verdict=`insufficient_evidence`, settled-with-signal=1, signal_hit_rate=0.0, baseline_up_rate=0.0
- RSI trend predictor: prediction_accuracy=None, scored=0

## 6. Readiness → **NOT_READY**
**Interpretation:** 229 settled paper trades, 52.4% win rate, **$-30.93** net with profit factor 0.87 (<1.0 — avg loss $3.58 > avg win $2.82). Calibration (0.242) marginally beats coinflip. No durable edge yet; learning is still in cold-start and the TradingView gate now restricts trading to fresh aligned RSI signals (sparse). Accounting fully reconciles (`global_reconciled=true`).
