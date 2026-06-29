# Bot cycle summary (plain English)

_Updated: 2026-06-29 03:20 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 20 |
| **Checked at** | 2026-06-29 03:20 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-29 02:42 UTC |

**Issues flagged:** win_rate_below_target, up_side_bleed, cheap_down_bleed

**Fixes applied:**

- wr_tune_min_entry_0.48
- min_reward_risk_0.52

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $673.38 (34.68% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $51.81 |
| **Win rate** | 63.2% (136 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 65.5% |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 69 | 63.8% | $51.63 |
| **5m** | 19 | 63.2% | $-1.31 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **2005**
- 5-chart trend: **confirmed_up_3tf** (3/3 fresh)

## Quick verdict

**Good:** Making money on paper (+34.7%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
