# Technical Data Grades

**Generated:** 2026-06-27T23:39:17.522363+00:00  
**Repo SHA:** `3ed0fa570f24`  
**Ticks:** 488 | **Settled:** 93

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **62.5** | **D** |
| Report overall | 54.9 | F |
| Technical runtime | 80.2 | B |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 51.1 | F |
| Operation | 70.4 | C |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 97.0 | 20 |
| tv_intake | 98.9 | 20 |
| design_compliance | 80.0 | 25 |
| trade_pipeline | 60.0 | 20 |
| gate_coupling | 60.0 | 15 |

### Rtds Health (97.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 85.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (98.9)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 100.0 | 25 |
| reject_rate | 92.8 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 100.0 | 15 |

### Design Compliance (80.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| series_15m | 100.0 | 15 |
| green_path | 100.0 | 10 |
| paper_only | 100.0 | 10 |
| grok_shadow | 100.0 | 5 |
| tick_seconds | 100.0 | 10 |
| max_price | 100.0 | 10 |
| min_edge | 100.0 | 5 |
| min_reward_risk | 100.0 | 5 |
| cohort_relaxed | 100.0 | 10 |
| tv_trade_gates_off | 0.0 | 20 |

### Trade Pipeline (60.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| accounting_integrity | 0.0 | 25 |
| lifecycle | 100.0 | 20 |
| execution_gate | 100.0 | 20 |
| recon_checks | 0.0 | 15 |
| not_halted | 100.0 | 10 |
| uptime_ticks | 100.0 | 10 |

### Gate Coupling (60.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 38.6 | 25 |
| exec_pass_rate | 83.4 | 25 |
| reject_diversity | 69.5 | 20 |
| cohort_session_load | 46.0 | 15 |
| recent_eval_spread | 58.3 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-27 21:50:54 UTC | 91 | 51.8 | 44.8 | 70.4 | 47.0 |
| 2026-06-27 22:03:10 UTC | 92 | 53.4 | 48.1 | 70.4 | 47.0 |
| 2026-06-27 22:33:25 UTC | 92 | 53.4 | 48.1 | 70.4 | 47.0 |
| 2026-06-27 23:03:40 UTC | 92 | 53.4 | 48.1 | 70.4 | 47.0 |
| 2026-06-27 23:30:10 UTC | 93 | 54.9 | 51.1 | 70.4 | 47.0 |
