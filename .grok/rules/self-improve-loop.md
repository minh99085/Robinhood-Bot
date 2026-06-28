# Self-improve closed loop (operator ON — 2026-06-28)

Operator mandate: bot must **scan → trade → learn → adjust** without manual prompting.

## Adjust layer (runtime — engine)

| Knob | Value | Effect |
|------|-------|--------|
| `PULSE_RESEARCH_AUTO_APPLY` | **1** | Research loop applies **evidence-backed** avoid/exploit contexts (maker-checker; never loosens gates) |
| `PULSE_RESEARCH_FORBID_SIZE_INCREASE` | **1** | No auto size-ups from research |
| `PULSE_LEARNING_ENABLED` | **1** | Edge-model blend when model beats market Brier (weight 0 until then) |

## Outer loop (babysit)

| Item | Value |
|------|-------|
| `state.json` `phase` | `soak` (not `hands_off`) |
| `babysit_autopilot` | `true` |
| Windows task `GrokBot2-PulseBabysit` | Enabled (~15 min tick; full eval after soak) |

Babysit may still relax gates on **trade_starvation** only — never TV trade gates, never Grok follow.

## Still frozen

- Grok decider **shadow** only
- TV observe-only (no signal/MTF/context trade gates)
- Paper-only, arb + dep-arb ON, 15s tick
- No live trading