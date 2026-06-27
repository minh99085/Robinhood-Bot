# Bot cycle summary (plain English)

_Updated: 2026-06-27 21:34 UTC_

## Last cycle

| | |
|---|---|
| **Cycle #** | 4 |
| **Checked at** | 2026-06-27 20:11 UTC |
| **Result** | **watch** |
| **What it means** | Result: watch |
| **Next check after** | 2026-06-27 21:12 UTC |

**Fixes applied:**

- require_high_edge: 1 → 0 (medium+ edge OK on 15m DOWN)
- require_strong_cex: 1 → 0 (moderate+ CEX OK)
- down_block_not_stale: 1 → 0
- down_block_bullish_mtf: 1 → 0
- tick_seconds: 30 → 15
- max_price: 0.65 → 0.70

## How the bot is doing now

| | |
|---|---|
| **Mode** | Paper only (fake money) |
| **Started with** | $500.00 |
| **Total now** | $540.26 (8.05% return) |
| **Arb profit** | $59.73 (7 trades) |
| **Directional profit** | $-19.47 |
| **Win rate** | 59.3% (91 settled trades) |
| **UP win rate** | 50.0% |
| **DOWN win rate** | 62.0% |
| **Bot stopped?** | Yes — directional=True, arbitrage=False |
| **Overall grade** | — (—/100) |

### 5m vs 15m (recent)

| Market | Trades | Win rate | PnL |
|--------|--------|----------|-----|
| **15m** | 24 | 50.0% | $-19.65 |
| **5m** | 19 | 63.2% | $-1.31 |

### TradingView (INDEX:BTCUSD)

- Alerts received: **615**
- 5-chart trend: **confirmed_up_3tf** (3/3 fresh)

## Quick verdict

**Good:** Making money on paper (+8.1%); Arbitrage is doing most of the work.

**Watch:** UP trades still weak (coin-flip or worse).

---

_Auto-generated after each `/pulse-babysit` cycle. Full report: `report.md` / `report.docx` in this folder._
