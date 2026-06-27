# Technical Data Grades

**Generated:** 2026-06-27T21:34:07.210179+00:00  
**Repo SHA:** `64e5de39fbc7`  
**Ticks:** 6 | **Settled:** 91

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **66.3** | **D** |
| Report overall | 58.0 | F |
| Technical runtime | 85.7 | B+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 54.8 | F |
| Operation | 75.4 | C+ |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 100.0 | 20 |
| tv_intake | 98.8 | 20 |
| design_compliance | 77.5 | 25 |
| trade_pipeline | 80.3 | 20 |
| gate_coupling | 69.7 | 15 |

### Rtds Health (100.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 100.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (98.8)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 100.0 | 25 |
| reject_rate | 91.9 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 100.0 | 15 |

### Design Compliance (77.5)

| Component | Score | Weight |
|-----------|------:|-------:|
| series_15m | 100.0 | 15 |
| green_path | 100.0 | 10 |
| paper_only | 100.0 | 10 |
| grok_shadow | 100.0 | 5 |
| tick_seconds | 100.0 | 10 |
| max_price | 100.0 | 10 |
| min_edge | 50.0 | 5 |
| min_reward_risk | 100.0 | 5 |
| cohort_relaxed | 100.0 | 10 |
| tv_trade_gates_off | 0.0 | 20 |

### Trade Pipeline (80.3)

| Component | Score | Weight |
|-----------|------:|-------:|
| accounting_integrity | 100.0 | 25 |
| lifecycle | 100.0 | 20 |
| execution_gate | 100.0 | 20 |
| recon_checks | 100.0 | 15 |
| not_halted | 0.0 | 10 |
| uptime_ticks | 3.0 | 10 |

### Gate Coupling (69.7)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 38.7 | 25 |
| exec_pass_rate | 84.4 | 25 |
| reject_diversity | 69.5 | 20 |
| cohort_session_load | 100.0 | 15 |
| recent_eval_spread | 66.7 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-27 20:57:30 UTC | 90 | 63.5 | 58.2 | 90.4 | 47.0 |
| 2026-06-27 21:19:20 UTC | 90 | 53.5 | 48.2 | 70.4 | 47.0 |
| 2026-06-27 21:32:40 UTC | 90 | 63.5 | 58.2 | 90.4 | 47.0 |
| 2026-06-27 21:33:10 UTC | 91 | 61.8 | 54.8 | 90.4 | 47.0 |
| 2026-06-27 21:33:25 UTC | 91 | 58.0 | 54.8 | 75.4 | 47.0 |
