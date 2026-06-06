# Feature Activation Audit (Pass 1) — Hermes Polymarket Paper Training

_PAPER ONLY · audit + instrumentation only · no strategy/threshold/sizing/live changes. Verdicts traced from `run_tick` to trade open._

## Pass 2 — wired (raw-catalog Bregman → certified PAPER execution)
- Raw ABCAS/Bregman scanner: **active candidate generation (full-catalog group_markets)**
- Trainer Bregman certifier: **active**
- Bregman paper execution: **active if opportunities pass certification**
- Bregman sees full raw catalog: **True**
- Bregman execution priority before directional: **True**
  - run_tick feeds scan.eligible[:bregman_discovery_limit] (full eligible catalog) to Bregman, not watch[:budget].
  - ScanResult.eligible = all ranked kept markets (after safety filters).
  - _run_bregman/_open_bregman_sets runs BEFORE the directional loop in run_tick.
  - Groups de-duped by (group_type, market-id set, outcome set) before certify_all.
  - Per-tick caps + explicit reject reasons enforced; binary_yes_no skipped as synthetic_binary_not_executable.
  - Funnel written to metrics/bregman_execution.json (discovery→certify→open).

## Pass 6 — profitability-aware active learning (exploration authority)
- Active learning is the exploration authority: **True**
- Random/hash exploration opens trades: **False**
- Exploration requires paper realism: **True**
- Exploration bounded loss: **True**
- Exploration excluded from readiness: **True**
- Exploration cannot consume Bregman reserved capacity: **True**
- Near-misses logged for learning: **True**
- Bregman-first priority preserved: **True**

## Pass 5 — profitability-first ranking
- Profitability-first enabled: **True**
- Annotation before shortlist truncation: **True**
- Directional ranked by after-cost EV: **True**
- Bregman ranked by after-cost profit/ROI: **True**
- Negative after-cost cannot count as edge: **True**
- Missing annotation rejected/shadow: **True**
- Profitability governor active (hard gate): **True**
- Bregman-first priority preserved: **True**

## Pass 4 — Bregman-first strategy priority
- Bregman priority enabled: **True**
- Raw ABCAS/Bregman scanner controls candidate generation: **True**
- Trainer Bregman certifier active: **True**
- Bregman execution before directional: **True**
- Directional secondary after Bregman: **True**
- Exploration tertiary after exploit strategies: **True**
- Paper realism still enforced: **True**

## Pass 3 — paper execution realism (trustworthy paper training)
- Reference-price fills allowed for exploit validation: **False**
- Missing ask fallback allowed: **False**
- Stale book fills allowed: **False**
- Offline stub fills count as real PnL: **False**
- Bregman requires all executable legs: **True**
- Realistic executable trades separated from shadow: **True**
- Readiness excludes unrealistic fills: **True**
  - Centralized policy: `engine/training/paper_execution.py:PaperExecutionPolicy`

## Summary
- **Truly active (control trades):** Trainer Bregman certifier, Bregman paper execution, Bregman INPUT UNIVERSE (catalog vs shortlist), Profitability-first ranking, Active learning selector, Stale-book rejection, Spread/depth gates, Ambiguity gate, Chainlink conditioning, News/research/model overlay, Profitability governor, Position/open-slot governor, Stop-loss/take-profit/settlement handling
- **Telemetry-only:** Raw ABCAS/Bregman scanner, Grok/LLM reasoning overlay
- **Dead / imported-only:** Graph grouping (groups_from_graph), Random/hash exploration, Reference-price fill fallback
- **PnL-inflation risks:** Raw ABCAS/Bregman scanner, Paper fill realism (slippage/depth), Reference-price fill fallback
- Status counts: {'active': 13, 'telemetry': 2, 'annotated': 2, 'imported': 1, 'dead': 2}

## Runtime feature truth table

| Feature | File(s) | Runtime status | Controls trades? | Telemetry only? | Config/env flag | Evidence | Risk if unchanged |
|---|---|---|---|---|---|---|---|
| Raw ABCAS/Bregman scanner | engine/strategies/bregman_scanner.py<br>engine/arbitrage/constraint_discovery.py | `telemetry` | no | YES | BREGMAN_PAPER_SCAN_ENABLED / ABCAS_ENABLED | Run only from start_polymarket_paper_training loop; writes bregman_scan.json + metrics/bregman.json. NOT imported by polymarket_trainer.py — never opens a paper position. | ABCAS looks 'enabled' and reports candidates but never trades — false impression the flagship edge is live.<br>**Pass-2:** RESOLVED — raw-catalog combinatorial candidate generation is now ACTIVE in the trainer: group_markets runs over scan.eligible (full eligible catalog) every tick and certified sets open in PAPER. The standalone ABCAS scanner remains telemetry, but its candidate-source role is now realized by the trainer's full-catalog Bregman path. |
| Trainer Bregman certifier | engine/training/bregman_execution.py<br>engine/training/bregman_grouping.py<br>engine/training/polymarket_trainer.py | `active` | YES | no | bregman_enabled (cfg) | run_tick → _run_bregman → _bregman_tradable → scan_bregman → group_markets(records)+certify_all (trainer ~617-657). | Certifies only over the directional shortlist (see input universe).<br>**Pass-2:** RESOLVED — certifier now consumes the FULL eligible catalog (scan.eligible[:bregman_discovery_limit]); groups are de-duped by (group_type, market-id set, outcome set) before certify_all. |
| Bregman paper execution | engine/training/polymarket_trainer.py:_open_bregman_sets/_open_bregman | `active` | YES | no | bregman_execution_enabled (cfg) + mode==paper_train | _open_bregman_sets gated on paper_train + bregman_execution_enabled; appends hedged-leg PaperPositions via RiskEngine+PaperBroker; skips group_type=='binary_yes_no' (synthetic NO leg). | Almost never fires: binary YES/NO (most of Polymarket) is skipped and the input is the shortlist, so few/no real multi-leg sets.<br>**Pass-2:** ACTIVE if opportunities pass certification — runs BEFORE directional (Tier 1) and is bounded by per-tick caps (bregman_max_bundles_per_tick, bregman_max_capital_per_tick_usd, bregman_max_open_bundles, bregman_min_roi). Explicit reject reasons: synthetic_binary_not_executable, incomplete_or_uncertain_exhaustive_set, roi_below_min, capital_cap_per_tick, max_bundles_per_tick, max_open_bundles (+ certifier reasons).<br>**Pass-4:** FIRST-PRIORITY — certified-realistic opps reserve open slots (bregman_reserve_open_slots) + capital (bregman_reserve_capital_usd) before directional; directional is admission-gated (_directional_admit) and blocked on Bregman markets/events; opps sorted by after-cost quality. Reserve released to directional only when no certified-realistic opp exists. |
| Bregman INPUT UNIVERSE (catalog vs shortlist) | engine/training/polymarket_trainer.py:run_tick<br>engine/training/market_scanner.py:ScanResult.eligible | `active` | YES | no | bregman_discovery_limit (full-catalog cap; directional still uses budget) | PASS-2: run_tick now feeds Bregman scan.eligible[:bregman_discovery_limit] — the FULL ranked eligible catalog (all kept markets after safety filters), NOT watch[:budget]. ScanResult.eligible = [d['record'] for d in ranked]; directional still uses the shortlist (records). | RESOLVED — combinatorial arbitrage across the full market universe is now discoverable; previously only the directional shortlist was visible.<br>**Pass-2:** RESOLVED — Bregman sees the full eligible raw catalog. |
| Graph grouping (groups_from_graph) | engine/training/bregman_grouping.py | `dead` | no | no | (none) | No groups_from_graph() on this branch; the active grouping is group_markets(records). Dependency-graph clustering exists but is used only for cluster_id annotation. | Structural graph grouping not used for arbitrage discovery.<br>**Pass-2:** Left disabled (not present); metrics/bregman_execution.json records groups_from_graph_used=false + reason. group_markets now runs over the full eligible catalog, so full-universe discovery no longer depends on it. |
| Profitability-first ranking | engine/training/candidate_ranker.py:annotate_profitability<br>engine/training/profitability_governor.py<br>engine/training/market_scanner.py | `active` | YES | no | profitability_first (POLYMARKET_PROFITABILITY_FIRST=1 default) | market_scanner.scan calls rank_candidates (quality score) + annotate_feedback_value, then shortlist=ranked[:shortlist_limit]. annotate_profitability() is never called in the runtime path. | HIGH: candidates are truncated by quality score, NOT after-cost EV — profitable-but-lower-quality markets are dropped before any decision.<br>**Pass-5:** RESOLVED — annotate_profitability now runs in market_scanner.scan BEFORE shortlist truncation and (profitability_first) re-ranks by after-cost score; every candidate carries conservative executable economics (spread/depth/fee/slippage/tick drag + bucket). Directional opens are hard-gated at decision time by the profitability governor (after-cost edge/ROI/EV); negative after-cost is rejected. |
| Active learning selector | engine/training/active_learning.py:ActiveLearningSelector | `active` | YES | no | active_learning_enabled=1 (default) / random_exploration_enabled=0 | ActiveLearningSelector is not imported by the trainer or any engine/scripts runtime module; feedback_value is annotated but the selector is never invoked. | Exploration is blind; high-feedback-value markets are not prioritized.<br>**Pass-6:** RESOLVED — ActiveLearningSelector is now the EXPLORATION AUTHORITY: the trainer constructs it and _active_learning_admit scores every near-miss (uncertainty + calibration + category + disagreement + near-miss profit + execution quality - penalties), gates strict realism + bounded loss + diversity caps, and selects the most informative candidates. Random/hash cannot open a trade while active learning is enabled. |
| Random/hash exploration | engine/training/polymarket_trainer.py:_explore_gate/_consider | `dead` | no | no | random_exploration_enabled=0 (default; legacy fallback only) | _explore_gate = sha256(market+tick) % 1000 < exploration_rate; opens near-miss exploration trades at capped exploration_notional_usd (paper_train only). | Deterministic hash sampling (not learning-value); correctly tiny + counts_for_readiness=False, but adds no targeted edge.<br>**Pass-6:** DISABLED by default — _active_learning_admit routes exploration through the ActiveLearningSelector; the hash gate can no longer open a trade while active learning is on (legacy_random_exploration_blocked counts would-be opens). Kept only as a diagnostic/tie-breaker. |
| Cluster/correlation gate | engine/training/market_scanner.py (sets cluster_id)<br>engine/training/edge_engine.py (accepts open_clusters)<br>engine/training/polymarket_trainer.py:_consider | `annotated` | no | no | (cluster_id computed; open_clusters NOT passed) | market_scanner sets d['cluster_id']=graph.cluster_of(...); EdgeEngine.best_side accepts open_clusters/cluster_id, but the trainer call passes only open_event_groups (group_key), so the cluster gate is never triggered. | Correlated (non-same-event) exposure is NOT blocked — concentration risk; only same-event group_key duplication is gated. |
| Paper fill realism (slippage/depth) | engine/training/paper_policy.py<br>engine/execution/paper_broker.py<br>engine/training/config.py | `annotated` | YES | no | realistic_fill_enabled (default False) | realistic_fill_enabled defaults False (slippage+depth modeling OFF) outside the campaign-safe profile; status emits fill_realism=null. | HIGH: without realistic_fill_enabled, fills can be optimistic and null telemetry hides whether PnL is inflated.<br>**Pass-3:** HARDENED — a centralized PaperExecutionPolicy now classifies every directional + Bregman fill as realistic_executable / shadow_only_* / rejected. Reference/offline-stub/missing-ask/stale/thin/wide/ambiguous fills are downgraded to shadow (logged, never PnL) or rejected; only realistic_executable trades count toward readiness_pnl. docker-compose strict defaults: PM reference fills + offline stub OFF, spread<=0.08, depth>=25, ambiguity<=0.45, book age<=20s. |
| Stale-book rejection | engine/training/edge_engine.py<br>engine/training/config.py | `active` | YES | no | reject_on_stale_book=True / clob_stale_ms=3000 | Hard reject in EdgeEngine.evaluate when the book is stale. | Low (correctly enforced) — disabling it would allow stale fills. |
| Reference-price fill fallback | engine/execution/paper_broker.py<br>engine/training/config.py | `imported` | YES | no | allow_pm_reference_price_fills=False (default) | PaperBroker supports reference-price fills but they are OFF by default (and campaign-safe forces them off). | If enabled, produces fantasy fills not backed by a real ask.<br>**Pass-3:** RESOLVED — docker-compose now sets PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS=0 and PAPER_ALLOW_REFERENCE_PRICE_FILLS=0; the PaperExecutionPolicy downgrades any reference-price fill to shadow_only_reference_price and quarantines its (theoretical) PnL out of readiness. |
| Spread/depth gates | engine/training/edge_engine.py<br>engine/training/config.py | `active` | YES | no | max_spread=0.08 / min_depth_at_price=50 / max_fill_depth_fraction=0.35 | Hard rejects in EdgeEngine.evaluate before edge math. | Low — correctly enforced hard gates. |
| Ambiguity gate | engine/training/edge_engine.py<br>engine/training/config.py | `active` | YES | no | max_ambiguity_score=0.35 (hard) + ambiguity_penalty_weight (soft) | Hard reject above max_ambiguity_score; soft penalty below. | Low — enforced; mis-set threshold could over/under-filter. |
| Chainlink conditioning | engine/training/chainlink_oracle.py<br>engine/training/polymarket_trainer.py<br>engine/training/edge_engine.py | `active` | YES | no | chainlink_enabled / btc_pulse_require_chainlink | Read each tick (read-only); conditions/gates Bregman + BTC Pulse and applies a directional penalty when stale. | Low for paper; stale anchor correctly penalizes. |
| News/research/model overlay | engine/research/news_scanner.py<br>engine/research/probability.py<br>engine/training/edge_engine.py | `active` | YES | no | NEWS_SCANNER_ENABLED / RESEARCH_USE_IN_STRATEGY | Advisory, read-only; feeds the probability estimate when RESEARCH_USE_IN_STRATEGY; cannot bypass risk/fill gates. | Medium: research nudges probability; weak calibration could bias edge. |
| Grok/LLM reasoning overlay | engine/research/grok_client.py | `telemetry` | no | YES | NEWS_ENABLE_GROK_PACKET (grok_with_news_count null in report) | Advisory research-only; cannot place/size/approve. Report shows grok_with_news_count=null (telemetry gap, not a trade control). | Low for trades; unmeasured contribution (null counters). |
| Profitability governor | engine/training/profitability_governor.py | `active` | YES | no | require_profitability_annotation / min_after_cost_edge (cfg) | Not referenced by polymarket_trainer.py; only used inside annotate_profitability, which is itself never called. | No after-cost graylist/throttle is applied to directional ranking.<br>**Pass-5:** RESOLVED — ProfitabilityGovernor is constructed in the trainer and wired into _open via _profitability_gate: it computes conservative after-cost edge/ROI/EV, hard-rejects negative-after-cost (bucket negative_after_cost), shadows sub-threshold candidates, records strikes in MarketQualityMemory, and never lets an unannotated candidate execute (require_profitability_annotation). |
| Position/open-slot governor | engine/training/polymarket_trainer.py:run_tick<br>engine/training/edge_engine.py<br>engine/risk.py | `active` | YES | no | max_open_trades / RiskEngine caps | run_tick breaks on len(open_positions) >= max_open_trades; EdgeEngine gates max_open_trades; RiskEngine enforces exposure. | Low — enforced. |
| Stop-loss/take-profit/settlement handling | engine/training/polymarket_trainer.py:_monitor | `active` | YES | no | (monitor/settlement each tick) | _monitor marks open positions to market each tick and settles resolved markets into realized PnL. | Medium: explicit SL/TP is mark-and-settle; no intra-round stop. |

## Top 10 edge leaks (ranked by profit impact)

1. **[highest]** Bregman/ABCAS only sees the directional shortlist, not the full normalized catalog _( Bregman INPUT UNIVERSE (catalog vs shortlist) )_
2. **[high]** Raw ABCAS scanner is telemetry-only — the flagship edge never opens a trade _( Raw ABCAS/Bregman scanner )_
3. **[high]** Profitability-first ranking is unused — candidates truncated by quality score, not after-cost EV _( Profitability-first ranking )_
4. **[high]** realistic_fill_enabled defaults False — paper PnL may be optimistic and fill_realism telemetry is null _( Paper fill realism (slippage/depth) )_
5. **[medium-high]** Cluster/correlation gate annotated but not enforced (open_clusters not passed) _( Cluster/correlation gate )_
6. **[medium-high]** binary_yes_no Bregman groups skipped (correct safety) leaves the trainer Bregman with almost nothing to trade _( Bregman paper execution )_
7. **[medium]** Active learning unused — exploration is blind hash sampling _( Active learning selector )_
8. **[medium]** Profitability governor dead — no after-cost graylist/throttle on directional ranking _( Profitability governor )_
9. **[low-medium]** Grok/news evidence counters null — research overlay impact is unmeasured _( Grok/LLM reasoning overlay )_
10. **[medium]** Two divergent Bregman implementations (strategies/bregman_scanner ABCAS vs training/bregman_execution) — reporting and execution disagree _( Trainer Bregman certifier )_

## Pass 2 recommendation
- **Recommended:** True
- Pass 2 SHOULD connect ABCAS to certified paper execution — but only AFTER widening the Bregman input universe and unifying the two Bregman implementations.
- Preconditions:
  - Feed the FULL normalized catalog (engine.arbitrage.constraint_discovery) to combinatorial discovery, not the directional shortlist.
  - Require BOTH legs real + executable (no synthetic binary NO leg) before any certified-executable open.
  - Turn on realistic_fill_enabled so certified after-cost profit is real.
  - Unify engine/strategies/bregman_scanner (ABCAS) with engine/training/bregman_execution so reporting == execution.
- Guardrails:
  - PAPER ONLY — route certified-executable arbs through the existing RiskEngine + PaperBroker; never enable a live path.
  - Keep EXECUTABLE_AFTER_COST_CERTIFIED gating; theoretical-only stays shadow.
- Rationale: Executing ABCAS today would produce ~0 trades (shortlist input + binary skip) or fantasy multi-leg fills (realistic_fill off). Fix the input universe + fill realism first.
