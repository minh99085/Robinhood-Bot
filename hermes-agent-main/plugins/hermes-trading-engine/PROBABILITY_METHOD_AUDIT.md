# Probability-Method Audit — Hermes Trading Engine

**Scope:** every place the agent calculates, estimates, blends, calibrates, or
gates on probability. Analysis only — no strategy was changed, no live trading,
no Micro Live, no production. All figures below are read from the actual source.

**Methods found: 32** (across 8 categories A–H).

Conventions: `p` = probability of the YES/UP outcome in [0,1]; "market price" =
the executable price of a binary contract (also a probability in [0,1]).

---

## Part A — Every current probability method

### A. Market-implied probability

**A1. Polymarket Gamma implied YES price**
1. Name: Gamma `outcomePrices` implied probability
2. File/fn: `engine/feeds/polymarket.py::get_trending_markets` / `_parse_clob_token_ids`; normalized in `engine/markets/universe_manager.py::MarketRecord.from_raw`
3. Formula: `p_yes = float(outcomePrices[0])` (falls back to `lastTradePrice`)
4. Inputs: Gamma market record
5. Output: `yes_price` ∈ [0,1]
6. Used: pulse/Polymarket paper pricing, universe scoring, campaign signal `mid`
7. Affects trading? **Yes** (it's the market baseline the bot trades against)
8. Affects risk? Indirectly (spread/price feed into gates)
9. Calibrated? It IS the market's own calibration; not re-calibrated
10. Tested? Indirectly via universe/campaign tests
11. Weakness: uses **last/あlisted** outcome price, not a vig-removed mid; no liquidity weighting
12. Improve: compute vig-adjusted mid from best bid/ask (see E/Part E)

**A2. Order-book midpoint**
1. Name: Midpoint `p_mid`
2. File/fn: `engine/execution/*` order book `midpoint`; `engine/engine.py::_oms_price_provider`; `signal_models._market_mid`
3. Formula: `p_mid = (best_bid + best_ask) / 2`
4. Inputs: best bid/ask from CLOB book (when CLOB enabled)
5. Output: mid price ∈ [0,1]
6. Used: paper fill reference price, mark price, campaign `mid`
7. Affects trading? **Yes**
8. Affects risk? Yes (spread = ask−bid feeds `EXCESSIVE_SPREAD`)
9. Calibrated? No
10. Tested? `tests/test_orderbook.py`, `test_paper_broker.py`
11. Weakness: simple mid ignores depth imbalance and quote staleness
12. Improve: depth/imbalance-weighted micro-price; staleness decay

**A3. Synthetic BTC-pulse market price** — `engine/engine.py` pulse loop. A 5-min
binary built from live BTC price; `delta = current − start`. Drives the pulse bet.
Trading: yes. Risk: via standard gates. Calibrated: no. Weakness: synthetic, not a
real tradeable book.

**A4. Polymarket paper resolution at implied odds** — `engine/engine.py::_eval_polymarket`:
`p_win = min(0.97, max(0.03, trade.price))`, resolved by `random() < p_win`
(fair binary, EV ≈ 0). Trading: yes (closes paper bets). Calibrated: n/a.
Weakness: deliberately zero-edge — a *sanity* model, not alpha.

### B. Model probability

**B1. Online logistic feature model (BTC pulse predictor)**
1. Name: `OnlineLogistic.predict_proba`
2. File/fn: `engine/features.py::OnlineLogistic`
3. Formula: `z_i = clamp((x_i − mean_i)/std_i, −6, 6)`; `s = b + Σ w_i·z_i`; `p = 1/(1+e^{−s})` (sigmoid). SGD update: `w_i −= lr·(err·z_i + l2·w_i)`, running mean/var via Welford.
4. Inputs: 5 pulse features (B2)
5. Output: P(up) ∈ (0,1)
6. Used: pulse signal; `_feature_active` decides whether it's trusted
7. Affects trading? Yes (when active & beating baseline)
8. Affects risk? No
9. Calibrated? Indirectly (compared by Brier to baseline; not output-calibrated)
10. Tested? Pulse/engine tests exercise the path
11. Weakness: trained on tiny, noisy 5-min momentum; no regularization tuning; standardization drifts
12. Improve: per-regime/category calibration; feature selection; freeze + calibrate output

**B2. Pulse features** — `engine/features.py::pulse_features`: `[clv, ewma, mom_z, range_z, vol_imb]` (close-location value, EWMA return, z-scored momentum, z-scored range, signed-volume imbalance). Inputs to B1.

**B3. Markov 3-state P(up)**
1. Name: 3-state regime Markov
2. File/fn: `engine/quant/markov.py::fit`
3. Formula: classify returns → BULL/BEAR/SIDE; transition counts with **Laplace smoothing** (`counts=ones`); `matrix = counts/rowsum`; next-state dist from last state; `p_up = next[BULL] + 0.5·next[SIDE]`, clamped [0.01, 0.99]
4. Output: `p_up`, matrix, stationary dist
5. Used: pulse direction; dashboard regime panel
6. Trading? Yes. Risk? No. Calibrated? No (Laplace prior only)
7. Weakness: fixed 3-state discretization; side-band heuristic; no calibration to realized up-rate
8. Improve: calibrate `p_up`; HMM with learned emissions

**B4. Monte-Carlo GBM P(up)**
1. Name: GBM path simulation
2. File/fn: `engine/quant/montecarlo.py::simulate`
3. Formula: `mu=mean(rets)`, `σ=std(rets)`, `drift=mu−0.5σ²`; `price_paths = spot·exp(cumsum(drift+σ·Z))`; `p_up = mean(terminal > spot)` over 500 paths
4. Output: `p_up`, quantile fan, terminal hist
5. Trading? Yes (pulse). Risk? No. Calibrated? No
6. Weakness: assumes GBM/normal returns (fat tails ignored); short window
7. Improve: bootstrap/empirical resampling; calibrate

**B5. Pattern heuristics** — `engine/quant/patterns.py` (BOS/CHoCH/liquidity sweep). Deterministic signals, **not** probabilities; flavor the pulse decision.

### C. LLM / Grok probability

**C1. Grok raw fair probability** — `engine/research/schemas.py::GrokProbabilityOutput.fair_probability` (+ `confidence`), produced by `engine/research/grok_client.py::GrokResearchClient.research`. Research-only. Trading: only via the ensemble (C/E). Calibrated: by C5/E. Weakness: LLM probabilities are systematically over-confident & uncalibrated.

**C2. Grok confidence** — `output.confidence` ∈ [0,1]; shrinks the LLM ensemble weight (E1).

**C3. Evidence score** — `engine/research/probability.py::evidence_score_of`: `score = Σ_i (quality_i · max(0.05, weight_i))`, clamped [0,1]. Used to shrink LLM weight and as a risk gate (`research_min_evidence=0.35`).

**C4. Settlement-ambiguity score** — `engine/research/ambiguity.py::AmbiguityScorer.score`: keyword/category heuristic, `score = min(1, 0.18·#categories) (+0.15 single-source boost)`. Used to shrink LLM weight and as risk gate (`research_max_ambiguity=0.35`).

**C5. Legacy GrokBrain direction/confidence** — `engine/brain.py::GrokBrain` (separate from the research engine): emits UP/DOWN/HOLD + confidence + rationale; gated on `RESEARCH_MODE`/key and the dashboard on/off toggle. Research-only. Trading: feeds pulse `grok_dir`. Calibrated: tracked via `grok_accuracy` only.

### D. Calibration

**D1. Pulse bucket calibration (with shrinkage)**
1. File/fn: `engine/calibration.py::Calibrator`
2. Formula: bin `p` into `bins=20`; empirical up-rate `ups/n` in that bin; `cal = (ups + shrink·p)/(n + shrink)` (shrink=25 pseudo-counts), clamped [0.02,0.98]; min_samples=40
3. Reports: `brier_raw`, `brier_cal` = `mean((p−y)²)`
4. Used: pulse model probability calibration + dashboard `calibration`
5. Trading? Yes (calibrates pulse). Tested? engine tests
6. Strength: this is a **real, data-driven** bucket calibrator (beta-binomial-style shrinkage)
7. Weakness: **global** (not per-category/per-regime); 20 fixed bins; no ECE/log-loss reported
8. Improve: per-category curves; report ECE + log-loss; isotonic option

**D2. Research calibration adapter (shrink toward 0.5)**
1. File/fn: `engine/research/calibration_adapter.py::CalibrationAdapter.apply`
2. Formula: `p_cal = 0.5 + (p_raw − 0.5)·(1 − shrink)`
3. Used: `p_calibrated` in the research bundle
4. Weakness: **not fitted to outcomes** — a fixed shrink toward 0.5, not Platt/isotonic; constant regardless of observed reliability
5. Improve: replace/augment with fitted Platt (logistic) or isotonic on resolved research outcomes

**D3. Feature-vs-baseline Brier gate** — `engine/engine.py::_brier` + `_feature_active`: `brier = mean((p−y)²)`; the feature model is only "active" when its Brier beats the baseline's by a margin over ≥60 samples. Good guardrail (model must *prove* it beats baseline). Weakness: binary on/off, no continuous weighting.

**D4. Campaign feedback calibrator** — `engine/campaigns/signal_models.py::FeedbackCalibrator`: rolling window of (predicted_prob, win, edge); `hit_rate`, `brier = mean((p−win)²)`, `edge_adjustment = clamp(0.5 + hit_rate, floor, cap)` scales next-cycle edge. Recursive/self-tuning. Weakness: hit-rate→multiplier is heuristic, not a true calibration map; global not per-category.

### E. Ensemble probability

**E1. Forecast ensemble (market/LLM/model blend)**
1. File/fn: `engine/research/ensemble.py::ForecastEnsemble.combine` (orchestrated by `engine/research/probability.py::ProbabilityEstimator.estimate`)
2. Formula:
   - `llm_quality = confidence · (0.4 + 0.6·evidence) · (1 − ambiguity)`
   - weights: `w_market=0.50`, `w_llm=0.30·llm_quality`, `w_model=0.20` (each →0 if input missing)
   - `p_ensemble = Σ w·p / Σ w`
   - clamp to [0.05, 0.95] **unless** `evidence≥0.8 and confidence≥0.8`
3. Inputs: `p_market`, `p_llm` (=`p_calibrated`), `p_model`, confidence, evidence, ambiguity
4. Output bundle: `p_market_mid, p_llm_raw, p_model, p_calibrated, p_ensemble, confidence, ambiguity_score, evidence_score, source_count, no_trade_reason`
5. Affects trading? Only when the research path is consumed by a strategy (currently research-only/advisory + campaign signal); affects risk gates
6. Calibrated? `p_llm` is shrunk (D2); blend is **not** outcome-fitted
7. Tested? `tests/test_research_phase5.py`
8. Strength: already **shrinks LLM by evidence/ambiguity/confidence** and **anchors 50% on market**
9. Weakness: static weights (not learned from replay/shadow); no per-category weighting; clamp is symmetric and ad-hoc; no explicit "shrink toward market by uncertainty" term
10. Improve: learn weights from replay/shadow; spread/liquidity/time-to-close-aware shrink (Part E formulas)

### F. Edge / EV methods

**F1. Campaign net edge** — `engine/campaigns/paper_campaign.py::generate_signals_and_trade`:
`gross = (fair − ask)` for BUY, `(bid − fair)` for SELL; `slip = min(max_slip, coeff·(size/depth))`; `net_edge_raw = gross − spread − slip − fee`; `net_edge = net_edge_raw · feedback.edge_adjustment`. Gate requires `net_edge ≥ min_net_edge (2.5%)`. Trading: yes (paper). **Strength:** subtracts spread+slippage+fees. **Weakness:** no adverse-selection term; slippage is a crude linear model; no Kelly sizing (fixed size).

**F2. Risk edge-after-costs gate** — `engine/risk.py` `min_edge_after_costs` (default 0.0): a proposal must carry non-negative `edge_after_costs`. Affects risk: **yes**. Weakness: default 0.0 only blocks negative edge; no uncertainty band.

**F3. Arbitrage net edge** — `engine/engine.py::assess_arb_proposal`: `edge_after_costs = executionNetPct/100`. Risk-gated. Weakness: relies on upstream arb calc.

**F4. Pulse bet EV / agreement** — `engine/engine.py` pulse: combines `markov_dir`, `grok_dir`, `montecarlo p_up`, feature `predict_proba`; flags `disagree` and computes an `ev` before opening. **Weakness:** ad-hoc agreement logic; not a calibrated probability → EV; **no Kelly**, fixed `max_stake_fraction` sizing.

> **Sizing today:** `stake = cap_frac(max_stake_fraction) · sizing_base` (a fixed fraction of a capped bankroll). **No Kelly / fractional Kelly anywhere.**

### G. Replay / shadow / post-trade metrics

`engine/replay/metrics.py`: **G1** `max_drawdown` (abs+pct), **G2** `sharpe = mean(ret)/std·√n`, **G3** `sortino` (downside dev), **G4** `volatility`, **G5** `fill_ratio`/`partial_fill_ratio`, **G6** `fee_total/avg`, **G7** `rejection_reasons` (count by code), **G8** `pnl_by(bucket)` (P&L grouped by a key), and **G9** a `calibration` dict (Brier from resolved research outcomes via `scripts/evaluate_research_calibration.py`). Replay/shadow only; not trading. **Missing here:** ECE, log-loss, **markout / adverse-selection**, predicted-edge-vs-realized, and P&L-by-edge-bucket.

**G10. MarketQualityScore** — `engine/markets/universe_manager.py::score_market`: weighted blend of liquidity/volume/velocity/spread/depth/time-to-resolution/category-diversity minus penalties. A *selection* score (not a probability) but it's the closest thing to a liquidity/depth/spread-aware quality model.

### H. Risk probability usage

`engine/risk.py` (RiskEngine — the only approver of even simulated orders) gates on:
- **H1** `RESEARCH_MISSING / RESEARCH_INVALID` — no/invalid estimate.
- **H2** `RESEARCH_NO_TRADE` — estimator's `no_trade_reason`.
- **H3** `RESEARCH_STALE` — estimate older than max age.
- **H4** `RESEARCH_LOW_EVIDENCE` — `evidence < research_min_evidence (0.35)`.
- **H5** `RESEARCH_INSUFFICIENT_SOURCES` — `source_count < 2`.
- **H6** `RESEARCH_HIGH_AMBIGUITY` — `ambiguity > 0.35`.
- **H7** `RESEARCH_PROBABILITY_CONFLICT` — `|p_llm − p_market| > 0.30` at `confidence ≥ 0.40`.
- **H8** spread (`EXCESSIVE_SPREAD`, max 0.10), **H9** stale data (`max_data_age_s=60`), **H10** settlement ambiguity (venue), **H11** `min_edge_after_costs`.
All affect risk: **yes**. **Strength:** a genuine multi-gate uncertainty filter already exists. **Weakness:** thresholds are fixed/hand-set; no single combined "uncertainty band"; conflict gate uses raw LLM vs market, not the calibrated ensemble.

---

## Part B — Summary table

| # | Method | Type | File | Formula / logic | Trade? | Weakness | Upgrade |
|---|--------|------|------|-----------------|--------|----------|---------|
| 1 | Gamma implied YES | Market | feeds/polymarket.py | `outcomePrices[0]` | Y | last price, has vig | vig-removed mid |
| 2 | Order-book mid | Market | execution/*, engine.py | `(bid+ask)/2` | Y | ignores depth/staleness | micro-price |
| 3 | Synthetic pulse price | Market | engine.py | BTC `delta` over 5m | Y | synthetic | n/a |
| 4 | PM paper resolution | Market | engine.py `_eval_polymarket` | `p=clamp(price,.03,.97)` | Y | zero-edge by design | n/a |
| 5 | Online logistic | Model | features.py | `sigmoid(b+Σw·z)` | Y | noisy, uncalibrated out | calibrate output |
| 6 | Pulse features | Model | features.py | z-scored mom/vol/flow | Y | thin signal | feature selection |
| 7 | Markov P(up) | Model | quant/markov.py | transition→`next[B]+0.5·next[S]` | Y | uncalibrated | calibrate/HMM |
| 8 | Monte-Carlo P(up) | Model | quant/montecarlo.py | GBM `mean(term>spot)` | Y | normal returns | empirical resample |
| 9 | Patterns | Model | quant/patterns.py | BOS/CHoCH heuristics | Y | not probabilistic | — |
| 10 | Grok fair_probability | LLM | research/schemas,grok_client | raw LLM prob | via ens. | overconfident | calibrate (Platt) |
| 11 | Grok confidence | LLM | research/schemas | [0,1] | weight | self-reported | down-weight |
| 12 | Evidence score | LLM | research/probability.py | `Σ q·max(.05,w)` | gate/weight | heuristic | source reliability |
| 13 | Ambiguity score | LLM | research/ambiguity.py | `min(1,.18·#cats)+.15` | gate/weight | keyword-only | NLP rules model |
| 14 | Legacy GrokBrain | LLM | brain.py | UP/DOWN + conf | Y (pulse) | uncalibrated | calibrate |
| 15 | Pulse bucket calib | Calib | calibration.py | `(ups+s·p)/(n+s)` | Y | global, no ECE | per-category + ECE |
| 16 | Research shrink-to-.5 | Calib | research/calibration_adapter.py | `.5+(p-.5)(1-shrink)` | via ens. | not fitted | Platt/isotonic |
| 17 | Feature Brier gate | Calib | engine.py `_brier` | `mean((p-y)²)` | Y | binary on/off | continuous weight |
| 18 | Campaign feedback | Calib | campaigns/signal_models.py | hit-rate→edge mult | Y (camp) | heuristic | true calibration |
| 19 | Forecast ensemble | Ensemble | research/ensemble.py | wt blend + shrink + clamp | via ens. | static weights | learn weights |
| 20 | Probability bundle | Ensemble | research/probability.py | assembles all p_* | via ens. | — | — |
| 21 | Campaign net edge | Edge | campaigns/paper_campaign.py | `gross-spread-slip-fee` | Y (camp) | no adverse-sel | + markout |
| 22 | Risk edge-after-costs | Edge | risk.py | `≥ min_edge_after_costs` | risk | default 0 | + uncertainty band |
| 23 | Arb net edge | Edge | engine.py | `execNetPct/100` | Y | upstream dep | — |
| 24 | Pulse EV/agreement | Edge | engine.py | dir agreement + ev | Y | ad-hoc, no Kelly | calibrated EV + Kelly |
| 25 | Drawdown/Sharpe/Sortino | Metrics | replay/metrics.py | standard | N | post-hoc | — |
| 26 | Fill/partial/fee ratios | Metrics | replay/metrics.py | counts | N | — | — |
| 27 | pnl_by bucket | Metrics | replay/metrics.py | group P&L | N | not by edge | edge-bucket P&L |
| 28 | Replay calibration | Metrics | replay + script | Brier on resolved | N | no ECE/markout | add ECE/log-loss/markout |
| 29 | MarketQualityScore | Selection | markets/universe_manager.py | wt quality − penalties | selection | not P | — |
| 30 | Research risk gates | Risk | risk.py | evidence/ambiguity/stale/conflict | risk | fixed thresholds | combined band |
| 31 | Spread/stale gates | Risk | risk.py | spread≤.10, age≤60s | risk | fixed | dynamic |
| 32 | Sizing (fixed fraction) | Sizing | engine.py | `max_stake_fraction·base` | Y | no Kelly | fractional Kelly |

---

## Part C — Missing methods (by priority)

**Present (✅), partial (◑), missing (❌):**

### High priority
1. Market-implied baseline — ✅ (A1/A2) but ◑ not used as the *anchor everywhere*
2. Vig/spread-adjusted fair probability — ❌ (uses listed price / raw mid; no vig removal)
3. Liquidity-weighted midpoint — ❌
4. Order-book-depth-adjusted probability (micro-price) — ❌ (depth only used in selection score)
5. Bayesian update (prior + evidence) — ❌ (shrinkage ≠ posterior)
6. Logistic/Platt calibration — ❌ (only shrink-to-0.5 D2 + bucket D1)
7. Isotonic calibration — ❌
8. Ensemble shrinkage toward market — ◑ (market-weighted, but no explicit uncertainty-scaled shrink)
9. Edge after fees/slippage/adverse-selection — ◑ (fees+slippage yes; adverse-selection ❌)
10. Probability calibration by category — ❌ (calibration is global)
11. Time-to-resolution decay — ❌ (universe scores TTR; probability path ignores it)
12. Resolution-ambiguity penalty — ✅ (C4 + H6)
13. Correlated-exposure model — ◑ (event-group dedup in campaign; no correlation matrix)
14. No-trade uncertainty band — ◑ (discrete gates exist; no combined band)
15. Fractional Kelly sizing with caps — ❌

### Medium priority
Hierarchical Bayesian by category — ❌ · Elo for repeated event classes — ❌ · HMM regime — ◑ (3-state Markov, not full HMM) · Kalman latent fair-prob — ❌ · Survival/hazard time-to-event — ❌ · Beta-binomial calibration — ◑ (D1 shrinkage is beta-binomial-like, global) · Dirichlet multi-outcome — ❌ · Bayesian model averaging — ❌ · Conformal/uncertainty intervals — ❌ · Adverse-selection model — ❌ · Quote-staleness model — ◑ (stale gate, no decay) · Liquidity-shock model — ❌ · Cross-market arb consistency — ◑ (arb engine exists, not consistency-priced) · News-freshness decay — ❌ · Source-reliability scoring — ◑ (evidence weights, no learned reliability)

### Advanced
Online learning with delayed labels — ◑ (feature model online; research labels delayed/unused) · Contextual bandit for market selection — ❌ · Causal event model — ❌ · NLP structured evidence extraction — ◑ (Grok freeform) · Graph of related markets — ❌ · Microstructure imbalance — ❌ · Markout execution-alpha — ❌ · Category calibration curves — ❌ · Meta-labeling trade/no-trade — ❌ · Expected utility with drawdown penalty — ❌

---

## Part D — Quant Team Edge Improvement Plan (plain English)

Goal: **make the probabilities more accurate and the trade selection more honest** —
not more aggressive. Ranked by expected value.

### Tier 1 — Must do now (paper/replay only)
1. **Use market price as the baseline probability** for every market, and measure every model against it. If a model can't beat the market mid, don't trade it.
2. **Add a spread/liquidity-adjusted fair price** (remove the vig; use the executable side: buy at ask, sell at bid).
3. **Calibrate by bucket AND by category** (extend the existing pulse calibrator to prediction-market categories), and report **Brier, log-loss, and ECE**.
4. **Add a no-trade uncertainty band**: only trade when edge clears costs *plus* an uncertainty cushion that grows with spread, ambiguity, staleness, and weak evidence.
5. **Use fractional Kelly only after calibration is proven** (and with a hard cap). Until then, keep fixed small size.
6. **Penalize high ambiguity and stale evidence** harder (already gated; make it a continuous penalty in the edge).
7. **Track P&L by edge bucket** (does predicted edge actually pay?).
8. **Track predicted edge vs realized markout** (are we getting picked off right after entry?).
9. **Block trades where edge disappears after fees + slippage (+ adverse selection)**.
10. **Always compare to two baselines: "do nothing" and "market midpoint."** A model only earns capital if it beats both.

### Tier 2 — After clean paper/replay
1. Bayesian evidence update (prior = market, update with Grok evidence by reliability).
2. Hierarchical calibration by market category.
3. Time-to-close decay model.
4. Order-book imbalance / depth (micro-price) probability.
5. Source-reliability scoring (learned, replaces flat evidence weights).
6. Learn ensemble weights from replay/shadow outcomes (instead of fixed 0.5/0.3/0.2).

### Tier 3 — Later
1. Contextual bandit for which markets to even look at.
2. Graph of related markets (consistency pricing).
3. Advanced NLP structured evidence extraction.
4. Survival model for event-time markets.
5. Full regime (HMM/Kalman) model for market behavior.

---

## Part E — Specific formulas to adopt

```
# 1. Market midpoint
p_mid = (best_bid + best_ask) / 2

# 2. Executable prices (what you actually pay/receive)
spread = best_ask - best_bid
p_buy  = best_ask          # you BUY YES at the ask
p_sell = best_bid          # you SELL YES at the bid

# 3. Edge
edge_buy  = p_fair - p_buy
edge_sell = p_sell - p_fair

# 4. Edge after costs
edge_net = edge - fee_cost - slippage_cost - ambiguity_penalty - stale_penalty - adverse_sel

# 5. Fractional Kelly (binary contract) — ONLY after calibration is proven
b              = (1 - price) / price
kelly          = (b * p - (1 - p)) / b
stake_fraction = clamp(kelly * kelly_fraction, 0, max_fraction)   # e.g. kelly_fraction=0.25

# 6/7/8. Scoring
brier    = mean((p_i - y_i)^2)
log_loss = -mean(y_i*log(p_i) + (1-y_i)*log(1-p_i))
ECE      = sum_k (n_k/n) * abs(avg_conf_k - avg_outcome_k)

# 9. Confidence-weighted blend (down-weight LLM on weak evidence / high ambiguity)
p_final = w_model*p_model + w_llm*p_llm + w_market*p_market
#   w_llm   reduced when evidence low or ambiguity high
#   w_model reduced when category calibration poor

# 10. Conservative ensemble (shrink toward market)
p_final = p_market + shrink_factor * (p_raw_model - p_market)
#   shrink_factor LOWER when: spread wide, liquidity low, evidence weak,
#   model poorly calibrated, time-to-resolution short, ambiguity high

# 11. No-trade zone
trade only if  abs(p_final - executable_price) > min_edge_after_costs + uncertainty_band

# 12. Uncertainty band
uncertainty_band = base_uncertainty
                 + ambiguity_weight * ambiguity_score
                 + spread_weight    * spread
                 + stale_weight     * stale_score
                 + evidence_weight  * (1 - evidence_score)
```

**Note on what already exists:** the research ensemble (E1) already does a
market-anchored, evidence/ambiguity-shrunk blend, and the pulse calibrator (D1)
already does bucket calibration with shrinkage — so Tier-1 items 1–4 are mostly
*extensions/hardening* of existing code, not green-field work.

---

## Part F — Next implementation prompt

See `NEXT_IMPLEMENTATION_PROMPT_PROBABILITY_EDGE.md` (Tier-1 only, paper/replay/
shadow only, with tests + report + validation; no live/Micro-Live/production).
