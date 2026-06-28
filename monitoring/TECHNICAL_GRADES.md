# Technical Data Grades

**Generated:** 2026-06-28T07:56:16.342935+00:00  
**Repo SHA:** `f3534deccc71`  
**Ticks:** 33 | **Settled:** 103

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **73.4** | **C** |
| Report overall | 67.7 | D |
| Technical runtime | 86.6 | B+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 67.0 | D |
| Operation | 89.8 | B+ |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 100.0 | 20 |
| tv_intake | 99.9 | 20 |
| design_compliance | 72.5 | 25 |
| trade_pipeline | 91.7 | 20 |
| gate_coupling | 67.7 | 15 |

### Rtds Health (100.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 100.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (99.9)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 100.0 | 25 |
| reject_rate | 99.4 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 100.0 | 15 |

### Design Compliance (72.5)

| Component | Score | Weight |
|-----------|------:|-------:|
| series_15m | 100.0 | 15 |
| green_path | 100.0 | 10 |
| paper_only | 100.0 | 10 |
| grok_shadow | 100.0 | 5 |
| tick_seconds | 100.0 | 10 |
| max_price | 50.0 | 10 |
| min_edge | 100.0 | 5 |
| min_reward_risk | 50.0 | 5 |
| cohort_relaxed | 100.0 | 10 |
| tv_trade_gates_off | 0.0 | 20 |

### Trade Pipeline (91.7)

| Component | Score | Weight |
|-----------|------:|-------:|
| accounting_integrity | 100.0 | 25 |
| lifecycle | 100.0 | 20 |
| execution_gate | 100.0 | 20 |
| recon_checks | 100.0 | 15 |
| not_halted | 100.0 | 10 |
| uptime_ticks | 16.5 | 10 |

### Gate Coupling (67.7)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 38.0 | 25 |
| exec_pass_rate | 87.5 | 25 |
| reject_diversity | 69.3 | 20 |
| cohort_session_load | 100.0 | 15 |
| recent_eval_spread | 50.0 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-28 06:00:22 UTC | 102 | 67.3 | 65.7 | 90.7 | 47.0 |
| 2026-06-28 06:18:06 UTC | 103 | 67.9 | 67.0 | 90.6 | 47.0 |
| 2026-06-28 06:48:20 UTC | 103 | 67.8 | 67.0 | 90.4 | 47.0 |
| 2026-06-28 07:18:24 UTC | 103 | 67.8 | 67.0 | 90.2 | 47.0 |
| 2026-06-28 07:48:31 UTC | 103 | 67.8 | 67.0 | 90.0 | 47.0 |
