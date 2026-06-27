# Bot cycle summary (plain English)

_Updated: 2026-06-27 05:30 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 21 |
| **Checked at** | 2026-06-27 05:22 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-27 06:30 UTC |

**Issues flagged:** profit_factor_low, up_side_bleed

**Fixes applied:**

- baseline_down: block DOWN when volume_state=active (33.3% WR, -13.38 PnL, n=6 on 15m late)
- baseline_down: block DOWN when UP_STRONG+range_top on non-bullish MTF (mixed loss cluster)

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $556.01 (11.2% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $-3.72 |
| **Win rate** | 61.6% (86 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 65.1% |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 19 | 57.9% | $-3.91 |
| **5m** | 19 | 63.2% | $-1.31 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **150**
- 5-chart trend: **partial_down_3tf** (2/3 fresh)

## Quick verdict

**Good:** Making money on paper (+11.2%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
