# Bot cycle summary (plain English)

_Updated: 2026-06-27 23:39 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 5 |
| **Checked at** | 2026-06-27 21:34 UTC |
| **Result** | **blocked** |
| **What it means** | Stopped — serious problem found. Check issues below. |
| **Next check after** | 2026-06-27 23:37 UTC |

**Fixes applied:**

- stop_min_samples: 40 → 60 (unhalt directional; rolling n=50 in warmup)
- down_block_not_stale: 0 → 1 (block momentum-against DOWN entries)

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $549.70 (9.94% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $-10.03 |
| **Win rate** | 60.2% (93 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 63.0% |
| **Bot stopped?** | No — bot is running |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 26 | 53.8% | $-10.22 |
| **5m** | 19 | 63.2% | $-1.31 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **694**
- 5-chart trend: **confirmed_down_3tf** (3/3 fresh)

## Quick verdict

**Good:** Making money on paper (+9.9%); Arbitrage is doing most of the work; Bot is running normally.

**Watch:** UP trades still weak (coin-flip or worse).

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
