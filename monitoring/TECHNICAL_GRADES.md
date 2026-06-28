# Technical Data Grades

**Generated:** 2026-06-28T18:34:01.283103+00:00  
**Repo SHA:** `7d93cf9a4252`  
**Ticks:** 366 | **Settled:** 129

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **78.2** | **C+** |
| Report overall | 74.1 | C |
| Technical runtime | 87.7 | B+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 80.6 | B |
| Operation | 88.3 | B+ |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 100.0 | 20 |
| tv_intake | 99.9 | 20 |
| design_compliance | 70.0 | 25 |
| trade_pipeline | 100.0 | 20 |
| gate_coupling | 68.4 | 15 |

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
| reject_rate | 99.6 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 100.0 | 15 |

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

### Gate Coupling (68.4)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 37.5 | 25 |
| exec_pass_rate | 90.1 | 25 |
| reject_diversity | 69.3 | 20 |
| cohort_session_load | 76.0 | 15 |
| recent_eval_spread | 75.0 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-28 17:16:56 UTC | 126 | 73.4 | 79.1 | 88.5 | 47.0 |
| 2026-06-28 17:31:56 UTC | 127 | 73.7 | 79.7 | 88.5 | 47.0 |
| 2026-06-28 17:47:11 UTC | 128 | 73.9 | 80.1 | 88.4 | 47.0 |
| 2026-06-28 18:17:11 UTC | 128 | 73.9 | 80.1 | 88.3 | 47.0 |
| 2026-06-28 18:31:11 UTC | 129 | 74.1 | 80.6 | 88.3 | 47.0 |
