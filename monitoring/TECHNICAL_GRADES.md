# Technical Data Grades

**Generated:** 2026-06-28T06:38:50.209002+00:00  
**Repo SHA:** `b07c3923de17`  
**Ticks:** 8 | **Settled:** 103

## Composite

| Metric | Score | Grade |
|--------|------:|-------|
| **Composite** | **73.3** | **C** |
| Report overall | 67.8 | D |
| Technical runtime | 86.3 | B+ |

## Report scores (engine)

| Section | Score | Grade |
|---------|------:|-------|
| Trading Performance | 67.0 | D |
| Operation | 90.4 | A |
| External Signals | 47.0 | F |

## Technical runtime

_RTDS/oracle health, TV observe-only intake, design manifest compliance, pipeline integrity, gate coupling._

| Component | Score | Weight |
|-----------|------:|-------:|
| rtds_health | 100.0 | 20 |
| tv_intake | 96.9 | 20 |
| design_compliance | 72.5 | 25 |
| trade_pipeline | 90.4 | 20 |
| gate_coupling | 71.5 | 15 |

### Rtds Health (100.0)

| Component | Score | Weight |
|-----------|------:|-------:|
| connected | 100.0 | 35 |
| oracle_fresh | 100.0 | 30 |
| stability | 100.0 | 20 |
| price_feed | 100.0 | 15 |

### Tv Intake (96.9)

| Component | Score | Weight |
|-----------|------:|-------:|
| observe_only | 100.0 | 25 |
| alert_flow | 100.0 | 25 |
| reject_rate | 99.4 | 15 |
| trade_gates_off | 100.0 | 20 |
| mtf_freshness | 80.0 | 15 |

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

### Trade Pipeline (90.4)

| Component | Score | Weight |
|-----------|------:|-------:|
| accounting_integrity | 100.0 | 25 |
| lifecycle | 100.0 | 20 |
| execution_gate | 100.0 | 20 |
| recon_checks | 100.0 | 15 |
| not_halted | 100.0 | 10 |
| uptime_ticks | 4.0 | 10 |

### Gate Coupling (71.5)

| Component | Score | Weight |
|-----------|------:|-------:|
| lifecycle_funnel | 38.1 | 25 |
| exec_pass_rate | 87.4 | 25 |
| reject_diversity | 69.3 | 20 |
| cohort_session_load | 100.0 | 15 |
| recent_eval_spread | 75.0 | 15 |

## VPS score history (last entries)

| UTC | Settled | Overall | Trading | Operation | External |
|-----|--------:|--------:|--------:|----------:|---------:|
| 2026-06-28 04:33:10 UTC | 100 | 65.2 | 61.4 | 90.9 | 47.0 |
| 2026-06-28 05:00:13 UTC | 101 | 66.3 | 63.6 | 90.9 | 47.0 |
| 2026-06-28 05:30:14 UTC | 102 | 67.3 | 65.7 | 90.9 | 47.0 |
| 2026-06-28 06:00:22 UTC | 102 | 67.3 | 65.7 | 90.7 | 47.0 |
| 2026-06-28 06:18:06 UTC | 103 | 67.9 | 67.0 | 90.6 | 47.0 |
