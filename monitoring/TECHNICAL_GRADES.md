# Technical Data Grades

**Generated:** 2026-06-28T04:36:21.293179+00:00  
**Repo SHA:** `203ec2d505f5`  
**Ticks:** 483 | **Settled:** 100

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **71.9** | **C** |
| Report overall | 65.2 | D |
| Technical runtime | 87.6 | B+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 61.4 | D |
| Operation | 90.9 | A |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 97.0 | 20 |
| tv_intake | 99.0 | 20 |
| design_compliance | 77.5 | 25 |
| trade_pipeline | 100.0 | 20 |
| gate_coupling | 60.0 | 15 |

### Rtds Health (97.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 85.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (99.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 100.0 | 25 |
| reject_rate | 93.3 | 15 |
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
| min_edge | 100.0 | 5 |
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

### Gate Coupling (60.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 38.2 | 25 |
| exec_pass_rate | 86.2 | 25 |
| reject_diversity | 69.4 | 20 |
| cohort_session_load | 25.0 | 15 |
| recent_eval_spread | 75.0 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-28 03:03:10 UTC | 98 | 65.4 | 61.9 | 90.9 | 47.0 |
| 2026-06-28 03:33:10 UTC | 98 | 65.4 | 61.9 | 90.9 | 47.0 |
| 2026-06-28 04:03:25 UTC | 98 | 65.4 | 61.9 | 90.9 | 47.0 |
| 2026-06-28 04:18:10 UTC | 99 | 64.2 | 59.5 | 90.9 | 47.0 |
| 2026-06-28 04:33:10 UTC | 100 | 65.2 | 61.4 | 90.9 | 47.0 |
