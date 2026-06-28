---
name: pulse-babysit
description: >-
  Autonomous BTC pulse bot closed loop: soak on VPS after deploy, pull reports,
  score trading performance, diagnose issues, fix code, commit/push main, sync-vps
  with orphan cleanup and rebuild, repeat. Use when the user wants hands-off bot
  iteration, autonomous improvement, closed-loop ops, or runs /pulse-babysit.
argument-hint: "cycle | force-eval | status | deploy | soak <minutes>"
---

# Pulse Babysit (closed loop)

You operate the **Grok-Bot-2** paper pulse bot without asking the operator for permission
between cycles. Execute tools yourself. Paper-only — never enable live trading.

**Team identity:** quant research + engineer + trader. **Learning-collection mode active** —
prioritize trade rate + ledger continuity over WR. Read `.grok/rules/self-improve-loop.md` (adjust ON),
`.grok/rules/soak-learning-lock.md`, `.grok/rules/quant-team.md`, and
**`.grok/rules/hands-off-untouchable.md`** (profitable-bot lock).

## Repo anchors

| Item | Path |
|------|------|
| Workspace | `C:\Users\tieut\Grok-Bot-2` |
| Plugin | `hermes-agent-main/plugins/hermes-trading-engine` |
| Deploy | `.\scripts\sync-vps.ps1` (always orphan cleanup + rebuild) |
| VPS | `root@45.32.224.147` `/opt/Grok-Bot-2` |
| Dashboard | `http://45.32.224.147/` |
| State | `scripts/pulse-babysit/state.json` |

## Commands

| Command | Behavior |
|---------|----------|
| `cycle` | Default loop iteration (respects soak timer) |
| `force-eval` | Pull + evaluate now; skip soak wait |
| `status` | Print state + last evaluation summary |
| `deploy` | `git push origin main` + full VPS deploy (sync-vps + env + force-recreate training) |
| `soak <minutes>` | Set soak duration (default **240 min / 4h** in learning_collection) via `set-soak.ps1` |

If no argument: run `cycle`.

## State machine

```
DEPLOY → SOAK (240m / 4h learning default) → PULL → EVALUATE → (issues?) → FIX → COMMIT → DEPLOY → …
```

1. Read `scripts/pulse-babysit/state.json`.
2. If `phase` is `hands_off` and `now < hands_off_until`: print status + baseline metrics, **exit**
   (no pull, no eval, no fix, no deploy). Respect `.grok/rules/hands-off-untouchable.md`.
3. If `phase` is `soak` and `now < soak_until`: run `status`, exit (do not fix).
4. Run `python scripts/pulse-babysit/scan-health.py` — full runtime checklist (Grok/verifier/loops/stop).
   Run `python scripts/pulse-babysit/validate-frozen-lock.py` — manifest drift (P0 authority keys).
5. Run `.\scripts\pulse-babysit\pull-vps-artifacts.ps1` — pulls artifacts **and always commits +
   pushes** `vps_full_reports/latest/` to `origin/main` (includes `report.docx`). Use `-SkipPush`
   only for local debugging.
6. Run `python scripts/pulse-babysit/evaluate-cycle.py` — parse JSON stdout.
7. If `verdict` is `healthy`: append history, set `phase=soak`, `soak_until=now+soak_hours`, done.
8. If `verdict` is `issues`: pick **at most 2** highest-severity issues; fix in plugin code only.
9. Run targeted tests under `hermes-agent-main/plugins/hermes-trading-engine/tests/`.
10. Commit with clear message; `git push origin main`.
11. **MANDATORY VPS deploy** (never skip after any push to `main` — unless `hands_off`):
    - `.\scripts\sync-vps.ps1` — `down --remove-orphans` → `build` → `up -d --remove-orphans`
    - SSH: `python3 /opt/Grok-Bot-2/scripts/apply-loop-arch-env.py` if env/gates changed
    - SSH: `python3 /opt/Grok-Bot-2/scripts/pulse-babysit/validate-frozen-lock.py` (wired in sync-vps)
    - SSH: `cd .../hermes-trading-engine && docker compose up -d --force-recreate hermes-training`
    - `.\scripts\verify-sync.ps1`
12. Update state: `phase=soak`, `deployed_at`, `soak_until`, `last_fixes`, increment `cycle`.

## Env coupling (mandatory memory)

Read `scripts/pulse-babysit/env-coupling.md` before any gate/TTC env change.

**Rule:** with baseline cohort + TV context gate both on,
`PULSE_TV_CONTEXT_MAX_TTC_S` must exceed the scaled cohort band on every series in
`PULSE_SERIES_SLUGS` (dual 5m+15m → use **900**, never **180** or **120**).

- Status field: `config_coupling.configured_ok` / `effective_s` / `fix_hint`
- `scan-health.py` flags `gate_coupling_misconfigured` (P0) if `.env` is unsafe
- Engine auto-clamps at runtime but `.env` must still be fixed
- TradingView: **INDEX:BTCUSD** — 2m/3m/4m chart alerts (observe-only); see `tradingview/README.md`
- **Soak/learning lock:** `.grok/rules/soak-learning-lock.md` + `frozen-env-keys.json` — frozen authority
  chain + relaxed quant params; tunable bounds only.
- **TV observe-only lock (operator mandate):** `.grok/rules/tv-observe-only-lock.md` — never re-enable
  MTF/context/signal/baseline-TV gates in babysit fixes; relax quant gates only.

## Evaluation rules (do not override without evidence)

The script flags issues. You may fix only what the report supports:

- **`trade_starvation` / `trade_starvation_streak` (P0)** → bot ticks but settled flat for **2**
  consecutive eval cycles, or no fills for ≥6h. **Relax quant gates** (cohort edge/CEX, execution floor,
  selectivity) — **never** re-enable TV trade gates per `tv-observe-only-lock.md`. **Never tighten**
  on `win_rate_below_target` / `profit_factor_low` in the same cycle when starvation is present.
- `win_rate_low` / `profit_factor_low` → quant gates, selectivity, reward/risk (**not** TV gates;
  **only if not trade_starvation** — stale WR on zero new trades is misleading)
- `up_side_bleed` → DOWN-only + quant restrictors (not TV context/MTF gates — locked off)
- `mtf_starved` → TV webhook health only (observe-only); **do not** enable MTF require/side-align
- `reconciliation_broken` → bug fix immediately (P0)
- `verifier_disabled` / `grok_not_follow` → run `validate-vps-env.py` on VPS; fix `.env`; recreate `hermes-training`
- `strategy_halted` → stop_conditions (Wilson/PF/DD); adjust `PULSE_STOP_MIN_SAMPLES` or performance
- `tv_feed_unhealthy` → webhook/secret/symbol (ops)
- `learning_hurts` → learning weight / bench veto

**Never** in autopilot: enable live trading, disable execution gate, re-enable any TV trade gate
(MTF/context/signal/baseline-TV), set exploration > 0 on TV gates, or large refactors.

## Soak duration

| Situation | Duration |
|-----------|----------|
| Learning collection (default) | **240 min (4h)** — no deploy/fixes during soak |
| WR optimization (operator ends learning mode) | **60 min** |
| Operator override | `.\scripts\pulse-babysit\set-soak.ps1 -Minutes N` |

## Todo scaffold (each cycle)

- `pb:pull` — artifacts on disk
- `pb:eval` — evaluate-cycle.py run
- `pb:fix` — code change (skip if healthy)
- `pb:deploy` — push + sync-rebuild
- `pb:soak` — timer set

## Autonomous scheduling (operator setup)

**Option A — Grok TUI (session open):**
```
/loop 15m /pulse-babysit cycle
/always-approve
```

**Option B — Windows Task Scheduler (hands-off):**
```
.\scripts\pulse-babysit\install-scheduled-task.ps1 -IntervalHours 1
```

**Option C — One-shot headless:**
```
grok -p "/pulse-babysit cycle" --yolo --cwd C:\Users\tieut\Grok-Bot-2 --max-turns 40
```

## Report outputs (mandatory)

After every pull, **always** commit + push `vps_full_reports/latest/` to `origin/main`, including
`report.docx` and **`CYCLE_SUMMARY.md`** (plain-English operator summary). Generated by
`scripts/pulse-babysit/write-cycle-summary.py` after pull + evaluate.
This is automatic via `pull-vps-artifacts.ps1` → `push-report-to-main.ps1`.
Standalone push: `.\scripts\pulse-babysit\push-report-to-main.ps1`.

## Completion message

End with: cycle number, verdict, soak_until (UTC), fixes applied (or "none"), VPS SHA.