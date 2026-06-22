# TradingView Composite — profit-oriented config (A) + bot Context Gate (B)

This is the "A + B" defense-in-depth setup for the BTC 5-minute Polymarket pulse:

- **A (this doc):** how to configure the TradingView Composite indicator so it only *emits* signals
  in the entry contexts that have historically paid, and suppresses the ones that bleed money.
- **B (`engine/pulse/context_gate.py`):** a hard, code-enforced **restrict-only** gate that blocks
  the same proven-losing contexts *immediately* — before the Learned Selectivity Gate has enough
  samples — and counts every block in the report. B is the safety floor for A.

> **Paper-only / safety:** Nothing here can place, size, force, or bypass a trade. A only changes
> which alerts are *sent*; B can only *prevent* trades. The strict execution-quality gate remains
> the sole trade authority.

## Why these settings (evidence)

From the live signal-learning report (small samples — directional, not conclusive):

- **Winning contexts:** `volume_state=active`/`dead` (WR ~0.73–0.80, +PnL), `supertrend=bullish`
  (n=16, WR 0.75, +PnL), `range_middle`, and `ttc 60–120s`.
- **Losing contexts:** `volume_state=spike` (WR ~0.13, large −PnL), `hurst=noise`, `z-score`
  extremes, and `ttc ≥ 240s` (WR ~0.2).
- The **strength score is not the edge** — context/regime alignment is. So the levers are the
  *confirmation filters*, not the score threshold.

## A — recommended TradingView Composite settings

| Setting | Recommend | Why |
|---|---|---|
| Use SuperTrend filter | **ON** (ATR 10, factor 3) | `supertrend=bullish` is the best bucket. |
| Use ADX trend strength filter | **ON** (weak<15, strong>22) | Removes the chop/`noise` regime. |
| Use candle pressure filter | **ON** (vol 1.6, wick 0.45) | Filters liquidation/whipsaw bars (the spike losses). |
| Relative-volume / spike handling | **suppress spikes** | `volume_state=spike` is the biggest leak. |
| Minimum score gap | **2** (from 1) | Avoid near-tie coin-flip entries (`z-score −1..1`). |
| Strong signal min score | **8** (from 7) | Slightly higher conviction. |
| Send weak signals | keep **ON** | Don't starve samples; let the filters gate quality. |
| Event blackout (high-impact) | **ON** | Avoids `noise`-regime event losses. |
| VWAP filter | keep **ON** | Alignment buckets are net-positive. |
| Use 15m EMA bias | keep **ON** | HTF alignment is in the winning set. |
| Binance lead confirmation (1m) | keep **ON** | Cross-feed confirmation reduces single-feed noise. |

Make sure the alert JSON keeps sending the Composite v2/v3 fields the bot learns from, especially
`volume_state`, `supertrend_direction`, `adx_state`, `range_state`, `htf_bias`, and a fresh
`bar_time` (use the bar **close** time so it passes freshness).

## B — bot Context Gate knobs (env)

B encodes the same rules so profitability doesn't depend on getting every Pine toggle exactly
right. Defaults are conservative (gate OFF in the repo); enable per-deployment:

```
PULSE_TV_CONTEXT_GATE=1                 # turn the gate on (default 0 = off)
PULSE_TV_CONTEXT_BLOCK_VOLUME=spike     # comma-separated volume_state values to block
PULSE_TV_CONTEXT_BLOCK_HURST=noise      # comma-separated hurst regimes to block
PULSE_TV_CONTEXT_MAX_TTC_S=240          # block entries with seconds-to-close >= this (0/blank=off)
PULSE_TV_CONTEXT_EXPLORATION_RATE=0.05  # hard-capped <=5%: lets a few blocked candidates through
```

When active it reports under `tradingview.context_gate` (`blocked`, `passed`, `explored`,
`block_reasons`) and every block is also counted in `decision_lifecycle.rejected_by_stage.context_gate`
so reconciliation still holds. The small exploration carve-out keeps a tagged trickle of "bad"
contexts flowing so the bot keeps confirming they remain bad instead of going blind.
