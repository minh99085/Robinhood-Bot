# Technical Data Grades

**Generated:** 2026-06-28T20:12:45.463079+00:00  
**Repo SHA:** `da780c7ed2af`  
**Ticks:** 366 | **Settled:** 132

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **77.8** | **C+** |
| Report overall | 74.1 | C |
| Technical runtime | 86.5 | B+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 80.8 | B |
| Operation | 87.9 | B+ |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 97.0 | 20 |
| tv_intake | 96.9 | 20 |
| design_compliance | 70.0 | 25 |
| trade_pipeline | 100.0 | 20 |
| gate_coupling | 67.8 | 15 |

### Rtds Health (97.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 85.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (96.9)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 100.0 | 25 |
| reject_rate | 99.7 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 80.0 | 15 |

### Design Compliance (70.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| series_15m | 100.0 | 15 |
| green_path | 100.0 | 10 |
| paper_only | 100.0 | 10 |
| grok_shadow | 100.0 | 5 |
| tick_seconds | 100.0 | 10 |
| max_price | 50.0 | 10 |
| min_edge | 50.0 | 5 |
| min_reward_risk | 50.0 | 5 |
| cohort_relaxed | 100.0 | 10 |
| tv_trade_gates_off | 0.0 | 20 |

### Trade Pipeline (100.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| accounting_integrity | 100.0 | 25 |
| lifecycle | 100.0 | 20 |
| execution_gate | 100.0 | 20 |
| recon_checks | 100.0 | 15 |
| not_halted | 100.0 | 10 |
| uptime_ticks | 100.0 | 10 |

### Gate Coupling (67.8)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 37.4 | 25 |
| exec_pass_rate | 91.3 | 25 |
| reject_diversity | 69.3 | 20 |
| cohort_session_load | 70.0 | 15 |
| recent_eval_spread | 75.0 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-28 18:31:11 UTC | 129 | 74.1 | 80.6 | 88.3 | 47.0 |
| 2026-06-28 18:48:12 UTC | 130 | 74.4 | 81.2 | 88.2 | 47.0 |
| 2026-06-28 19:18:12 UTC | 130 | 74.4 | 81.2 | 88.1 | 47.0 |
| 2026-06-28 19:30:27 UTC | 131 | 73.9 | 80.3 | 88.1 | 47.0 |
| 2026-06-28 19:48:12 UTC | 132 | 74.2 | 80.8 | 88.1 | 47.0 |
