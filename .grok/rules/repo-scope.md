# Repo scope

Work only in `https://github.com/minh99085/Robinhood-Bot` (default branch `main`).

Never commit or push to any other repo unless the operator explicitly says otherwise in the
current turn.

## Git workflow (OPERATOR MEMORY — set 2026-07-03)

**Merge all branches into `main`; always push to `main`.** Completed work must not stay on
`cursor/*` feature branches. Every turn that finishes a task: merge → `git push origin main` →
deploy VPS. `origin/main` is canonical. See `.grok/rules/vps-deploy-mandate.md`.

## The bot

The only trading bot in this repo is the Robinhood Agentic plugin at
`hermes-agent-main/plugins/hermes-trading-engine-robinhood/` (options + equities via
Robinhood's Trading MCP). The Polymarket pulse engine has been removed — do not reintroduce it.

## Before destructive changes

Read `.grok/rules/destructive-change-guard.md` before any delete/remove/disable that could
damage the bot or its safety layer.

## VPS deploy — after every push to `main`

**Always:** push to `main` → sync VPS → remove orphans → rebuild. Execute yourself; never push
and stop. Goal: `origin/main` HEAD == VPS HEAD.

1. `git push origin main`
2. `.\scripts\sync-vps-robinhood.ps1`
3. `.\scripts\verify-sync.ps1` — VPS HEAD SHA must equal `origin/main`; containers healthy

### VPS access

- Host: `45.32.224.147`, user `root`, repo: `/opt/Robinhood-Bot`
- SSH key: `$env:USERPROFILE\.ssh\bot2_grok_temp`
- Plugin compose: `/opt/Robinhood-Bot/hermes-agent-main/plugins/hermes-trading-engine-robinhood`
  (profile `robinhood`)
