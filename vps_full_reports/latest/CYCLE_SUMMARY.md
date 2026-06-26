# Bot cycle summary (plain English)

_Updated: 2026-06-26 19:25 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 14 |
| **Checked at** | 2026-06-26 18:22 UTC |
| **Result** | **issues** |
| **What it means** | Issues found — UP trades still lose money. More UP blocks may have been added. |
| **Next check after** | 2026-06-26 19:25 UTC |

**Issues flagged:** up_side_bleed

**Fixes applied:**

- down_bias: block UP when ttc>=240s (25% WR, -13.32 PnL, n=8)
- down_bias: block UP when ttc<120s (weak early-window UP entries)

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $578.26 (15.65% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $18.53 |
| **Win rate** | 65.7% (70 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 72.0% |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 8 | 87.5% | $17.37 |
| **5m** | 14 | 64.3% | $-0.34 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **67**
- 5-chart trend: **partial_up_5tf** (2/5 fresh)

## Quick verdict

**Good:** Making money on paper (+15.7%); Arbitrage is doing most of the work; DOWN trades work well; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse); Cycle flagged UP-side losses.

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
