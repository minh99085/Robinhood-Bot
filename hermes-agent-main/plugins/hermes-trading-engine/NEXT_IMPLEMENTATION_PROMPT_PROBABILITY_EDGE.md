# NEXT IMPLEMENTATION PROMPT — Probability Edge (Tier 1 only)

Paste the prompt below into a new Cursor session to implement **only the Tier-1**
probability/edge improvements from `PROBABILITY_METHOD_AUDIT.md`. It is scoped to
be safe: **paper / replay / shadow only.**

---

## PROMPT

> You are Cursor Opus 4.8 High acting as a senior quant developer and safety engineer.
>
> Work only in `hermes-agent-main/plugins/hermes-trading-engine/`. Implement the
> **Tier-1 probability/edge improvements** described in `PROBABILITY_METHOD_AUDIT.md`.
> This is an accuracy/selection upgrade — **do not make the bot more aggressive.**
>
> ### HARD SAFETY RULES (do not violate)
> - Do NOT enable live trading. Do NOT add real order submission or cancellation.
> - Do NOT enable Micro Live. Keep it disabled by default.
> - Do NOT enable production execution. Keep Production Review design-only.
> - Do NOT add a dashboard submit button or an API submit/order route.
> - Do NOT loosen the RiskEngine. New checks may only make it *more* selective.
> - Grok stays research-only (never places/cancels/sizes/approves orders).
> - Paper / replay / shadow code paths only.
>
> ### Implement (Tier 1)
> Prefer a new, well-tested, pure module `engine/quant/edge_model.py` (or extend
> `engine/research/ensemble.py` + `engine/campaigns/signal_models.py`) so existing
> behavior is unchanged unless explicitly opted in via config/env (default OFF).
>
> 1. **Market baseline + vig-adjusted fair price.** Add helpers:
>    `market_mid(bid,ask)`, `executable_prices(bid,ask)` (buy=ask, sell=bid),
>    and `vig_adjusted_fair(yes_price, no_price)` that removes the over-round.
> 2. **Edge after costs (incl. adverse selection).** Extend the campaign net-edge
>    to subtract an `adverse_selection` term and an `uncertainty_band` (formula 12
>    in the audit). Gate: `abs(p_final - executable_price) > min_edge_after_costs + uncertainty_band`.
> 3. **Conservative shrink-toward-market ensemble.** Add
>    `p_final = p_market + shrink_factor*(p_raw - p_market)` where `shrink_factor`
>    decreases with spread, low liquidity, weak evidence, poor calibration, short
>    time-to-resolution, high ambiguity. Make it the campaign's signal blend
>    (behind a config flag, default ON for the campaign only — never for live).
> 4. **Calibration by bucket AND category + full metrics.** Extend
>    `engine/calibration.py` (or add `engine/quant/calibration_metrics.py`) to
>    compute **Brier, log-loss, and ECE**, and to keep **per-category** reliability
>    curves for prediction markets. Expose read-only via `GET /api/calibration`.
> 5. **No-trade uncertainty band** (formula 11/12) wired into the campaign risk gate
>    and surfaced in the campaign report.
> 6. **Continuous ambiguity/stale penalties** in the edge (not just hard gates).
> 7. **Edge-bucket P&L + predicted-edge-vs-realized-markout** tracking in
>    `engine/replay/metrics.py` (add `pnl_by_edge_bucket`, `markout`, `adverse_selection`).
> 8. **Baseline comparison.** In replay/shadow + campaign reports, always report
>    the strategy vs **"do nothing"** and **"market midpoint"** baselines.
> 9. **Fractional Kelly (gated, OFF by default).** Add `fractional_kelly(p, price, kelly_fraction, max_fraction)`
>    (formula 5) but DO NOT switch sizing to it yet — only compute + log the
>    suggested size in paper/shadow, behind a `CAMPAIGN_KELLY_ENABLED=0` flag.
> 10. Keep all new behavior **config/env-gated and default-safe**; existing tests must still pass.
>
> ### Tests (add)
> - vig removal + executable prices + edge math
> - shrink-toward-market reduces deviation as spread/ambiguity rise
> - Brier / log-loss / ECE correctness on known inputs
> - per-category calibration keeps separate curves
> - no-trade band blocks thin-edge trades; allows clear-edge trades
> - fractional Kelly clamps to [0, max_fraction] and is OFF by default
> - edge-bucket P&L + markout aggregation
> - safety: no live/Micro-Live/production/submit path was added
>
> ### Report
> - Write `PROBABILITY_EDGE_TIER1_REPORT.md`: what changed, formulas added,
>   before/after on a replay or accelerated paper campaign (Brier/ECE/edge-bucket
>   P&L, vs do-nothing and market-mid baselines), and any remaining gaps.
>
> ### Validate
> ```
> python -m compileall -q engine __init__.py
> pytest -q
> ```
> If tests fail, fix the new code (do not rewrite the strategy). Re-run until green.
>
> ### Final answer must include
> 1. Files changed, 2. Tests added + results, 3. Brier/ECE before vs after,
> 4. Confirmation that live trading / Micro Live / production / submit routes were
>    NOT added and the RiskEngine was not loosened.
