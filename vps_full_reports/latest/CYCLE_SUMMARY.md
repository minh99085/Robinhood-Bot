# Bot cycle summary (plain English)

_Updated: 2026-06-27 08:33 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 23 |
| **Checked at** | 2026-06-27 06:36 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-27 07:36 UTC |

**Issues flagged:** win_rate_below_target, profit_factor_low, up_side_bleed

**Fixes applied:**

- baseline_down: block not_stale divergence (54.5pct WR, -37 PnL, n=66)
- baseline_down: block mid entry band 0.55-0.60 (36.8pct WR, -31 PnL, n=19)

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

- Alerts received: **251**
- 5-chart trend: **partial_up_3tf** (2/3 fresh)

## Quick verdict

**Good:** Making money on paper (+11.2%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
