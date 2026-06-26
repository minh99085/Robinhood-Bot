# Bot cycle summary (plain English)

_Updated: 2026-06-26 21:22 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 15 |
| **Checked at** | 2026-06-26 19:25 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-26 20:27 UTC |

**Issues flagged:** up_side_bleed

**Fixes applied:**

- down_bias: block UP when htf_bias=bullish (40% WR, -10.65 PnL, n=10)
- down_bias: block UP when candle_pressure=bear_close_near_low (44% WR, -8.23 PnL, n=9)

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $577.98 (15.6% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $18.25 |
| **Win rate** | 65.3% (75 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 70.9% |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 9 | 88.9% | $20.31 |
| **5m** | 18 | 61.1% | $-3.55 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **70**
- 5-chart trend: **none** (—/5 fresh)

## Quick verdict

**Good:** Making money on paper (+15.6%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
