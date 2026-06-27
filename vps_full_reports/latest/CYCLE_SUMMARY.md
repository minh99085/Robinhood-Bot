# Bot cycle summary (plain English)

_Updated: 2026-06-27 12:33 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 25 |
| **Checked at** | 2026-06-27 10:35 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-27 11:35 UTC |

**Issues flagged:** win_rate_below_target, profit_factor_low, up_side_bleed

**Fixes applied:**

- down_bias: block UP when volume_state=active (37.5pct WR, -10.58 PnL, n=8)
- baseline_down: block DOWN when tf_confirm=single_tf (53.3pct WR, -10.02 PnL, n=15)

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

- Alerts received: **342**
- 5-chart trend: **single_tf** (1/3 fresh)

## Quick verdict

**Good:** Making money on paper (+11.2%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
