# Hermes BTC Pulse — STATE (auto-generated snapshot)

_Updated each persist. Human-readable loop memory. PAPER ONLY._

- **ticks:** 26 · **last tick:** 2026-06-25 13:26 UTC

## Capital

- **starting:** $500.0 · **on-hand (directional):** $503.38 · **return:** 0.68%
- **arb realized:** $34.46 · **total on-hand:** $537.84 · **total return:** 7.57%
- **open exposure:** $0.0 (0 positions)

## Active strategies

- **arbitrage (primary):** enabled=True · halted=False · realized=$34.4564 · open=0
- **directional:** enabled · halted=False · settled=29 · WR=0.6897 · PF=1.0752 · PnL=$3.3833
- **grok decider:** mode=shadow · affects_trading=False
- **verifier (maker-checker):** enabled=False · approve_rate=None

## Verifiable stop conditions

- **directional:** halted=False · reasons=['insufficient_samples'] · metrics={'n': 29, 'wins': 20, 'win_rate': 0.6897, 'wilson_lower': 0.5378212488366853, 'breakeven_wr': 0.6515, 'profit_factor': 1.0752, 'pnl_usd': 3.3833, 'max_drawdown_usd': 23.6305, 'max_drawdown_pct': 4.73}
- **arbitrage:** halted=False · reasons=[] · metrics={'executed': 4, 'settled': 4, 'realized_profit_usd': 34.4564, 'guaranteed_booked_usd': 34.4564}

## Open positions (directional)

_none_


## Active lessons

_none_


## Gates (restrict-only)

- **context_gate:** enabled=True · blocked=480 · reasons={'tv_context_ttc_too_far': 407, 'tv_context_hurst_noise': 73}
- **selectivity_gate:** enabled=True · rejected=0
- **readiness:** not_ready

