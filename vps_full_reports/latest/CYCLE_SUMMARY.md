# Bot cycle summary (plain English)

_Updated: 2026-06-27 03:22 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 19 |
| **Checked at** | 2026-06-27 02:22 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-27 02:39 UTC |

**Issues flagged:** profit_factor_low, up_side_bleed

**Fixes applied:**

- down_bias: enable block UP vs tf_confirm=confirmed_down (40% WR, -10.50 PnL, n=5)
- down_bias: block UP when cvd_state=buy_pressure (25% WR, -4.69 PnL, n=4)

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $561.01 (12.2% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $1.28 |
| **Win rate** | 62.4% (85 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 66.1% |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 18 | 61.1% | $1.09 |
| **5m** | 19 | 63.2% | $-1.31 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **132**
- 5-chart trend: **partial_up_3tf** (2/3 fresh)

## Quick verdict

**Good:** Making money on paper (+12.2%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
