# Bot improvement roadmap (3AI-informed)

Saved 2026-06-26. Tier 1–2 implemented; execute Tier 3–4 when metrics justify.

## Done (Tier 1–2)

- **Tier 1:** `baseline_cohort_gate` — 180–240s TTC, high edge_score, strong CEX; UP needs TV UP_STRONG
- **Tier 2:** Selectivity min_samples=50, PF<0.85 + Wilson + BH-FDR; `live_block_audit` in report

## Tier 3 — Evidence pipeline (when Tier 1–2 stable ~30+ trades)

1. Verifier counterfactual to n≥30 — decide if veto saves PnL
2. Walk-forward edge cohorts (train/validate split) before any promotion
3. Version Grok/Claude prompts + models on every grade
4. Monitor `trade_decision_history` for Grok learning (target >55% in proven contexts)

## Tier 4 — Champion–challenger (only after Tier 3 passes)

1. 10–20% paper canary: 180–240s + high edge + DOWN cohort only
2. Regime tags on all buckets (hurst/vol) to prevent selectivity whipsaw
3. Research meta-loop 15m or event-triggered (keep auto_apply=False)
4. **Never without scorecard:** Grok follow, TV signal gate, research auto_apply, lessons as hard blocks

## Promotion scorecard (all modules)

- min n ≥ 50 rolling
- Wilson LB > breakeven OR veto counterfactual PF > 1
- PF ≥ 1.2, EV after cost > 0
- BH-FDR q=0.10 across parallel buckets
- Walk-forward validation window
- Max correlation to baseline < 0.7

## Abort / hold criteria

- Directional WR < 60% on rolling 20 after Tier 1
- Trade count < 2 per hour for 4h (over-starved) — relax CEX requirement first, not edge
- Selectivity blocks with negative counterfactual PnL removed