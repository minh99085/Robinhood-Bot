# Laptop Hermes Agent — Operator Workflow (Phase 1)

This guide is for the **human operator** running Hermes from a laptop. You do **not**
need to be a coder. The tool (`scripts/laptop_agent.py`) automates the boring,
repeatable chores and tells you, in plain language, whether it is **SAFE TO
CONTINUE** or you should **STOP**, plus the exact next command to run.

---

## Accelerated discovery / learning mode (VPS env)

To process **more markets, candidates, shadow/no-trade labels, near-misses, and Bregman
diagnostics per runtime hour** — WITHOUT loosening any execution gate — start the
training container with `HERMES_ACCELERATED_DISCOVERY=1`.

On the **VPS**, in the plugin dir:

```bash
# one-off run with accelerated discovery
HERMES_ACCELERATED_DISCOVERY=1 docker compose up -d --build hermes-training

# or persist it for the container in .env (plugin dir), then restart:
echo "HERMES_ACCELERATED_DISCOVERY=1" >> .env
docker compose up -d --build hermes-training
```

What it scales UP (observation/learning only): scan breadth (`scan_limit`,
`shortlist_limit`), Bregman discovery breadth (`bregman_discovery_limit`), shadow labels
per tick, near-miss capture + store, CLOB hydration coverage, and a faster scan cadence.

What it **NEVER** changes: minimum depth, maximum spread, book freshness, after-cost
edge + ROI thresholds, ambiguity threshold, correlation gate, strict paper realism, and
the reference/missing-ask/stale/fake-fill bans. **Live trading stays disabled.** A weaker
opportunity can never count as a realistic executable or certified bundle — the report
keeps the buckets separated (`report_buckets`) and surfaces
`accelerated_discovery_enabled` + the per-tick throughput counters.

---

## Phase 5: the simplified autonomous operator loop (recommended)

The coordinator now does the mechanical work. You mainly (1) run one command,
(2) upload a zip to ChatGPT, (3) paste ChatGPT's reply back, (4) approve gated actions.
**Live trading stays disabled; no ChatGPT free text is ever executed as a shell command;
a long paper run requires an explicit approval flag.**

Run from local Windows PowerShell at
`C:\hermes-agent\hermes-agent-main\plugins\hermes-trading-engine`:

```powershell
# 0. (anytime) one-glance status + the suggested next command
python scripts/laptop_agent_coordinator.py status --config .laptop_agent.json

# 1. ONE command — the whole safe mechanical workflow (verify repo+config, sync GitHub
#    main, verify VPS SSH+commit+Docker, verify paper/live safety, collect the VPS light
#    report, copy the zip locally, and print exactly what to upload to ChatGPT).
python scripts/laptop_agent_coordinator.py operator-cycle --config .laptop_agent.json

# (optional) ALSO start an approved PAPER run at the end — never starts without this flag.
#   short = 2-hour proof run;  long = approved longer paper run.  Live trading stays OFF.
python scripts/laptop_agent_coordinator.py operator-cycle --config .laptop_agent.json --approved-paper-run --mode short
python scripts/laptop_agent_coordinator.py operator-cycle --config .laptop_agent.json --approved-paper-run --mode long
```

`operator-cycle` prints a final status block: **SAFE TO CONTINUE / STOP**, the local +
VPS commit hashes, whether paper training is running, the local report-zip path, the
exact ChatGPT upload instruction, and whether Cursor is needed (a Cursor handoff file is
prepared only when a blocker is detected — it is never auto-run). `collect-report` is a
friendly alias of `collect-light-report`.

Then **upload the printed zip to ChatGPT** and save ChatGPT's reply to a `.md` file
(e.g. `decision.md`). Classify it (the coordinator never auto-runs risky actions):

```powershell
python scripts/laptop_agent_coordinator.py record-chatgpt-decision --config .laptop_agent.json --file decision.md
```

**If ChatGPT says a code fix is needed (Cursor):**

```powershell
python scripts/laptop_agent_coordinator.py prepare-cursor-handoff --config .laptop_agent.json --file decision.md
# paste the generated cursor_handoffs\cursor_prompt_*.md into WEB Cursor;
# web Cursor pushes to GitHub main and reports the commit hash, then:
python scripts/laptop_agent_coordinator.py post-cursor-verify --config .laptop_agent.json
```

**If ChatGPT approves a long paper run:**

```powershell
python scripts/laptop_agent_coordinator.py record-chatgpt-decision --config .laptop_agent.json --file decision.md
python scripts/laptop_agent_coordinator.py start-paper-run --config .laptop_agent.json --mode long --approved-by-chatgpt
```

A **short** test run is the default and needs no approval:
`python scripts/laptop_agent_coordinator.py start-paper-run --config .laptop_agent.json --mode short`

Every cycle is recorded in `inspection_reports_artifacts\artifact_index.jsonl`
(timestamp, local/remote commit, report zip, validation/summary presence, handoff
files, decision classification).

---

## Who does what

| Role | Responsibility |
| --- | --- |
| **GitHub `main`** | The single source of truth for code. |
| **Cursor** | The code engineer (makes changes, opens PRs). |
| **ChatGPT** | An independent judge that reviews the inspection report you upload. |
| **Vultr VPS** | The paper-trading runtime that produces `runtime_data`. |
| **This laptop tool** | Operator chores only — status, sync checks, collecting artifacts, building & packaging the report. |

> The laptop tool **never trades, never loosens any gate, never changes paper-realism
> or live-safety controls, and never auto-prompts Cursor/ChatGPT.** It cannot.

## One-time setup

1. Make a local config (it is **git-ignored** and never committed):
   ```powershell
   copy .laptop_agent.example.json .laptop_agent.local.json
   notepad .laptop_agent.local.json
   ```
   Fill in your VPS host, user, SSH key path, and the `runtime_source` path.
   (You can instead create `.env.laptop_agent` with `LAPTOP_AGENT_*` keys.)

2. Confirm the tool runs:
   ```powershell
   python scripts/laptop_agent.py --help
   ```

## Everyday workflow (the short version)

Everything defaults to a **safe dry-run**. Add `--execute` only when the tool tells
you to. Run these from the plugin folder
(`...\hermes-agent-main\plugins\hermes-trading-engine`):

1. **See where you stand** (read-only, always safe):
   ```powershell
   python scripts/laptop_agent.py status --dry-run
   ```
   If it says **STOP**, follow the `NEXT COMMAND:` (usually `git pull origin main`)
   until it says **SAFE TO CONTINUE**.

2. **Bring runtime data over from the VPS** (only when you need fresh data):
   ```powershell
   python scripts/laptop_agent.py collect --dry-run   # shows the method it will use
   python scripts/laptop_agent.py collect --execute
   ```

3. **Build a fresh, provenance-verified package** (the one command to remember):
   ```powershell
   python scripts/laptop_agent.py fresh-package --dry-run    # preview the steps
   python scripts/laptop_agent.py fresh-package --execute    # do it for real
   ```

4. **Upload the printed zip to ChatGPT** for an independent review. The exact path is
   printed on the `PACKAGE:` / `UPLOAD REPORT TO CHATGPT:` line.

> **Always use `fresh-package`, not `package`, to build an upload.** `fresh-package`
> refuses a dirty or behind/ahead repo, archives any old `inspection_reports`,
> regenerates the report, validates it, proves freshness, and writes a
> `laptop_agent_package_provenance.json` *inside* the zip so the upload can prove it
> came from the current clean `origin/main` state.

## What `fresh-package --execute` does (in order)

1. **Refuses** if the repo is dirty or your local HEAD ≠ `origin/main`.
2. **Archives** any existing `inspection_reports/` to a git-ignored
   `_stale_inspection_reports_<timestamp>/` (old evidence is never reused).
3. Runs the light-mode report:
   `python scripts/generate_bot_inspection_report.py --output inspection_reports --data-dir runtime_data --bundle-mode light`
4. Runs validation:
   `python scripts/validate_training_runtime.py --data-dir runtime_data`
5. Verifies the report/validation pair is fresh (validation ran *after* the report,
   git evidence matches the current HEAD).
6. Writes `laptop_agent_package_provenance.json` and creates a timestamped zip.
7. Prints the exact zip path to upload.

## One-command operator handoff (coordinator — Phase 2)

`scripts/laptop_agent_coordinator.py` runs the full operator handoff in a few simple
commands: **Laptop → GitHub main → Vultr VPS → collect light report → package zip →
ChatGPT**. It is coordinator tooling only — it never changes trading strategy, gates,
or live-trading behavior, and never prints your VPS host/user/key.

Run these from the plugin folder in **PowerShell**:

   Run all of these from the plugin directory, e.g.
   `cd C:\hermes-agent\hermes-agent-main\plugins\hermes-trading-engine`.

1. **Create your config** (git-ignored; never committed). Prefer `init-config` — it
   writes a clean, **BOM-free**, secret-free file that the coordinator can always read
   (Notepad's "Save As" can add a UTF-8 BOM that used to break loading):
   ```powershell
   python scripts/laptop_agent_coordinator.py init-config --config .laptop_agent.json
   notepad .laptop_agent.json
   ```
   Fill in `repo_root`, `plugin_path`, `vps_host`, `vps_user`, `vps_ssh_key`,
   `vps_remote_plugin_path`, and `local_artifact_dir`.

   > **`vps_ssh_key` must be the PRIVATE key FILE PATH** (e.g.
   > `C:\Users\you\.ssh\hermes_vps_ed25519`) — **not** the public key text. If you
   > paste a value starting with `ssh-ed25519`/`ssh-rsa`, `doctor` fails with a clear
   > message. If you must edit by hand and hit a "BOM" parse error, just re-run
   > `init-config --force` and re-enter your values.

2. **Check your laptop is set up** (read-only):
   ```powershell
   python scripts/laptop_agent_coordinator.py doctor --config .laptop_agent.json
   ```
   Fix any `[FAIL]` lines until it prints `DOCTOR: SAFE TO CONTINUE`.

3. **Smoke-test the VPS** (read-only over SSH):
   ```powershell
   python scripts/laptop_agent_coordinator.py vps-smoke --config .laptop_agent.json
   ```
   Confirms SSH works, the remote plugin path exists (via the same `cd` collection
   uses, with stderr shown on failure), Docker is up, reports the `hermes-training`
   container status, and shows **which dependency-capable remote Python** will be used
   (`[PASS] remote Python can import pydantic — will use .../.report_venv/bin/python`).
   Collection picks the first interpreter that can `import pydantic`, preferring a
   project venv (`.report_venv`/`.venv`) over bare `python3`/`python`.

   If it shows `[FAIL] remote Python can import pydantic`, the VPS Python lacks the
   report dependencies. Build the dependency venv on the VPS with the one documented
   command (it never needs manual `pip install`):
   ```bash
   # on the VPS, in the plugin dir:
   bash scripts/vps_generate_light_report.sh
   ```
   Then re-run `vps-smoke` / `collect-light-report`.

4. **Collect a light report zip from the VPS** (one command):
   ```powershell
   python scripts/laptop_agent_coordinator.py collect-light-report --config .laptop_agent.json
   ```
   On the VPS this replaces remote `runtime_data`, runs
   `generate_bot_inspection_report.py ... --bundle-mode light`, runs
   `validate_training_runtime.py | tee validation_light_latest.txt`, zips
   `inspection_reports` + `runtime_data/inspection_summary.json` +
   `validation_light_latest.txt`, then copies a timestamped zip back to your
   `local_artifact_dir`. (Add `--dry-run` first to preview the exact commands.)

5. **Get the ChatGPT upload checklist:**
   ```powershell
   python scripts/laptop_agent_coordinator.py handoff-summary --config .laptop_agent.json
   ```
   Prints the zip path, timestamp, and whether the validation file +
   `inspection_summary.json` are inside — then: **upload that zip to ChatGPT for
   inspection.**

## Generating the light report ON THE VPS (one command)

Run this **on the Vultr VPS** (over SSH), from the plugin directory
`.../hermes-agent-main/plugins/hermes-trading-engine`:

```bash
bash scripts/vps_generate_light_report.sh
```

This is the single, permanent VPS report command. It is fully self-bootstrapping —
**no manual `pip install` is ever needed**:

- creates/updates a dedicated `.report_venv` and installs all report + test
  dependencies from `requirements.txt` + `requirements-dev.txt` (and explicitly
  `pytest`, `pydantic`, `numpy`, `fastapi`, `httpx`) — fixing the old
  `FAIL_NOT_RUN_READY` "missing pydantic/pytest/numpy" failures;
- uses `.report_venv/bin/python` for everything (never system python);
- prints `docker compose ps` + a tail of the `hermes-training` logs and the container
  state/health first (so a stale status shows whether the container is stopped,
  unhealthy, or `runtime_data` was copied too late);
- refreshes `runtime_data` via `docker cp hermes-training:/data runtime_data` (and
  `chown`s it), deletes the old `inspection_reports`, regenerates the light report, and
  runs `validate_training_runtime.py`;
- writes a **unique** `vps_light_report_<timestamp>.zip` and also updates
  `vps_light_report_latest.zip`, including `inspection_reports`,
  `runtime_data/metrics`, `validation_light_latest.txt`, and `report_logs/`.

It still enforces run-ready gating and exits with the report generator's own exit code
(it does **not** hide real failures). Then download/upload `vps_light_report_latest.zip`
to ChatGPT for inspection.

## Collecting runtime data on Windows (rsync vs scp)

`collect` copies `runtime_data` from the VPS and **replaces** your local copy. It
picks a transport automatically:

* **`rsync`** — used when it is on your `PATH` (Linux/macOS, or Windows with
  WSL/cwRsync). Mirrors the source with `--delete`.
* **`scp`** (the built-in Windows **OpenSSH client**) — used automatically when
  `rsync` is missing. This is the normal path on a stock Windows laptop in
  PowerShell — **no Git Bash, WSL, or cwRsync required**. Because `scp` has no
  `--delete`, the tool first **clears your local `runtime_data`**, then copies fresh
  (only with `--execute`; `--dry-run` deletes nothing).

`collect --dry-run` prints which method it selected and the exact (secret-redacted)
command it would run. If **neither** `rsync` nor `scp` exists, it **STOPs** and tells
you to enable the OpenSSH Client:
`Settings > Apps > Optional features > Add a feature > OpenSSH Client`.

It uses your configured `vps_ssh_key`, `vps_port`, and `runtime_source`, and never
prints the key path, host, or private-key contents.

## The lower-level commands (advanced)

`status`, `verify-sync`, `local-head`, `remote-head`, `check-docker`,
`check-vps --execute`, `collect --execute`, `report --execute`,
`validate --execute`, and `package --execute` are still available.

> **Do not use `package` on its own** unless `status` is **SAFE** and the report's
> provenance is fresh. By default `package --execute` **refuses** to zip a stale
> report — it STOPs if the report is missing, has no provenance, the repo is dirty,
> your HEAD differs from `origin/main`, the report's git evidence doesn't match your
> current HEAD, or validation didn't run after the report. (`--allow-stale-package`
> exists only as a deliberate, risky override.)

## Safety rules built into the tool

* **Dry-run is the default.** Nothing that touches the VPS or replaces `runtime_data`
  runs without `--execute`.
* **Secrets never printed, never committed.** VPS host/user/key and the runtime
  source are loaded from the git-ignored local config and are masked in all output.
* **Validation failures are never hidden** — a failed `validate` prints `STOP`.
* The tool has **no trading code path** and cannot change strategy, gates, or
  live-safety settings.

## Troubleshooting

* *"No local operator config found"* — you skipped one-time setup; copy the example
  file as shown above. (Read-only commands like `status`/`check-docker` still work.)
* *`remote-head` shows `unknown`* — no network or the remote is unreachable.
* *`check-vps` STOP* — verify the host/key in your local config and that the VPS is up.
