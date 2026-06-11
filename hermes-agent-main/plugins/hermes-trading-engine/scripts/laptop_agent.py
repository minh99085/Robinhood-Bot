#!/usr/bin/env python3
"""Laptop Hermes Agent — local operator layer (Phase 1).

A SAFE, local operator CLI for the human running Hermes from a laptop (PowerShell
on Windows, or any shell). It treats GitHub ``main`` as the source of truth, Cursor
as the code engineer, ChatGPT as an independent report judge, and a Vultr VPS as the
paper runtime.

This tool automates *operator chores only*. It is intentionally incapable of:
  * trading (it never calls any execution path),
  * loosening any spread / depth / freshness / edge / ROI / correlation gate,
  * changing paper-realism or live-trading safety controls,
  * making strategy decisions,
  * auto-prompting Cursor or ChatGPT, or auto-approving long runs.

Safety model
------------
* DRY-RUN IS THE DEFAULT. Pass ``--execute`` to actually perform an action.
* Read-only local probes (git status / HEAD / remote main / docker version) run
  even in dry-run because they cannot change anything.
* Any command that touches the VPS (SSH) or that copies/replaces ``runtime_data``
  REQUIRES ``--execute``; in dry-run the exact command is printed but never run.
* Secrets (VPS host/user/key, runtime source, etc.) are loaded from an UNCOMMITTED
  local config and are NEVER printed and NEVER committed.

Local config (uncommitted)
---------------------------
Copy ``.laptop_agent.example.json`` to ``.laptop_agent.local.json`` (git-ignored) and
fill it in, OR set ``.env.laptop_agent`` with ``LAPTOP_AGENT_*`` keys.

Run ``python scripts/laptop_agent.py --help`` for the command list.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
# scripts/laptop_agent.py -> plugin root is one level up.
REPO_ROOT = Path(__file__).resolve().parents[1]

CONFIG_JSON = ".laptop_agent.local.json"
CONFIG_ENV = ".env.laptop_agent"
EXAMPLE_CONFIG = ".laptop_agent.example.json"
ENV_PREFIX = "LAPTOP_AGENT_"

DEFAULT_RUNTIME_DATA = "runtime_data"
DEFAULT_INSPECTION_OUTPUT = "inspection_reports"
DEFAULT_GIT_REMOTE = "origin"
DEFAULT_MAIN_BRANCH = "main"
PYTHON_BIN = "python"  # PowerShell-friendly; matches the documented commands.

REPORT_SCRIPT = "scripts/generate_bot_inspection_report.py"
VALIDATE_SCRIPT = "scripts/validate_training_runtime.py"

# Config keys whose VALUES must never be printed or committed.
SECRET_KEYS = frozenset({
    "vps_host", "vps_user", "vps_ssh_key", "runtime_source",
    "runtime_remote_path", "api_key", "ssh_key",
})

# A short, stable marker the VPS echoes back on a successful SSH probe.
VPS_OK_MARKER = "hermes-vps-ok"

# Provenance / freshness guardrails (Phase 2). These files live INSIDE the report
# bundle dir so a package carries proof of the exact workspace state it came from.
REPORT_PROVENANCE = "laptop_agent_provenance.json"            # report + validation proof
PACKAGE_PROVENANCE = "laptop_agent_package_provenance.json"   # written at package time
VALIDATION_RESULT = "laptop_agent_validation_result.txt"      # captured validate output
# Existing inspection_reports are archived here (git-ignored) instead of being reused.
STALE_DIR_PREFIX = "_stale_inspection_reports_"
# Relative path recorded in provenance (never an absolute/secret path).
SCRIPT_REL_PATH = "scripts/laptop_agent.py"

RunResult = "tuple[int, str, str]"
Runner = Callable[..., "tuple[int, str, str]"]


# --------------------------------------------------------------------------- #
# Default (real) subprocess runner — injectable for tests
# --------------------------------------------------------------------------- #
def default_runner(argv, cwd=None, timeout: int = 60):
    """Execute ``argv`` (an explicit argument array — never a shell string) and
    return ``(returncode, stdout, stderr)``. Never raises on non-zero exit."""
    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd) if cwd else None, timeout=timeout,
            capture_output=True, text=True, shell=False)
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError as exc:
        return 127, "", f"{exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — operator tool must not crash on probe
        return 1, "", f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    runtime_data_dir: str = DEFAULT_RUNTIME_DATA
    inspection_output_dir: str = DEFAULT_INSPECTION_OUTPUT
    git_remote: str = DEFAULT_GIT_REMOTE
    git_main_branch: str = DEFAULT_MAIN_BRANCH
    vps_host: str = ""
    vps_user: str = ""
    vps_port: int = 22
    vps_ssh_key: str = ""
    # rsync/scp source, e.g. "ubuntu@host:/opt/hermes/runtime_data/"
    runtime_source: str = ""
    source_file: str = ""          # which file the config came from (non-secret)

    def vps_configured(self) -> bool:
        return bool(self.vps_host and self.vps_user)

    def collect_configured(self) -> bool:
        return bool(self.runtime_source)

    def public_summary(self) -> dict:
        """A SECRET-FREE view safe to print/log."""
        return {
            "config_source": self.source_file or "(none — using defaults)",
            "runtime_data_dir": self.runtime_data_dir,
            "inspection_output_dir": self.inspection_output_dir,
            "git_remote": self.git_remote,
            "git_main_branch": self.git_main_branch,
            "vps_configured": self.vps_configured(),
            "vps_port": self.vps_port if self.vps_configured() else None,
            "runtime_source_configured": self.collect_configured(),
        }


def _coerce_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _config_from_mapping(raw: dict, source_file: str) -> Config:
    cfg = Config()
    cfg.source_file = source_file
    cfg.runtime_data_dir = str(raw.get("runtime_data_dir") or cfg.runtime_data_dir)
    cfg.inspection_output_dir = str(raw.get("inspection_output_dir") or cfg.inspection_output_dir)
    cfg.git_remote = str(raw.get("git_remote") or cfg.git_remote)
    cfg.git_main_branch = str(raw.get("git_main_branch") or cfg.git_main_branch)
    cfg.vps_host = str(raw.get("vps_host") or "")
    cfg.vps_user = str(raw.get("vps_user") or "")
    cfg.vps_port = _coerce_int(raw.get("vps_port"), 22)
    cfg.vps_ssh_key = str(raw.get("vps_ssh_key") or raw.get("ssh_key") or "")
    cfg.runtime_source = str(raw.get("runtime_source") or "")
    return cfg


def _parse_env_file(text: str) -> dict:
    """Parse a ``KEY=VALUE`` env file into a lower-cased config mapping. Keys are
    expected to be prefixed with ``LAPTOP_AGENT_``; the prefix is stripped."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key.upper().startswith(ENV_PREFIX):
            key = key[len(ENV_PREFIX):]
        out[key.lower()] = val
    return out


def load_config(repo_root: Path = REPO_ROOT, explicit_path: Optional[str] = None,
                env: Optional[dict] = None):
    """Load operator config from (in priority order): an explicit path, the JSON
    local file, the env file, then ``LAPTOP_AGENT_*`` environment variables.

    Returns ``(Config, found: bool)``. ``found=False`` means no config file/env was
    present — callers that need VPS/secret values should show the setup message. A
    missing config is NEVER an error here (it must not crash)."""
    env = env if env is not None else os.environ
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    candidates.append(repo_root / CONFIG_JSON)
    candidates.append(repo_root / CONFIG_ENV)

    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            if path.suffix == ".json" or text.lstrip().startswith("{"):
                raw = json.loads(text)
            else:
                raw = _parse_env_file(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(raw, dict):
            return _config_from_mapping(raw, source_file=path.name), True

    # Environment-variable fallback (LAPTOP_AGENT_*), still secret-safe.
    env_raw = {k[len(ENV_PREFIX):].lower(): v for k, v in env.items()
               if k.startswith(ENV_PREFIX)}
    if env_raw:
        return _config_from_mapping(env_raw, source_file="(environment)"), True

    return Config(), False


def setup_message() -> str:
    return (
        "No local operator config found.\n"
        f"  -> Copy '{EXAMPLE_CONFIG}' to '{CONFIG_JSON}' and fill in your VPS details,\n"
        f"     OR create '{CONFIG_ENV}' with LAPTOP_AGENT_* keys.\n"
        f"  Both files are git-ignored and will NEVER be committed.\n"
        f"  Commands that only read the local repo / Docker still work without it."
    )


# --------------------------------------------------------------------------- #
# Command builders (pure — return explicit argument arrays; easy to unit-test)
# --------------------------------------------------------------------------- #
def git_status_cmd() -> list:
    return ["git", "status", "--porcelain"]


def git_local_head_cmd() -> list:
    return ["git", "rev-parse", "HEAD"]


def git_remote_head_cmd(cfg: Config) -> list:
    return ["git", "ls-remote", cfg.git_remote,
            f"refs/heads/{cfg.git_main_branch}"]


def git_fetch_cmd(cfg: Config) -> list:
    return ["git", "fetch", cfg.git_remote, cfg.git_main_branch]


def docker_version_cmd() -> list:
    return ["docker", "version", "--format", "{{.Server.Version}}"]


def build_inspection_report_cmd(cfg: Config, python_bin: str = PYTHON_BIN) -> list:
    """EXACTLY the documented light-mode inspection report command."""
    return [python_bin, REPORT_SCRIPT,
            "--output", cfg.inspection_output_dir,
            "--data-dir", cfg.runtime_data_dir,
            "--bundle-mode", "light"]


def build_validate_cmd(cfg: Config, python_bin: str = PYTHON_BIN) -> list:
    """EXACTLY the documented training-runtime validation command."""
    return [python_bin, VALIDATE_SCRIPT, "--data-dir", cfg.runtime_data_dir]


def build_vps_check_cmd(cfg: Config) -> list:
    """Read-only SSH connectivity probe (BatchMode: no password prompt). Built as an
    explicit argv array. Executed only with ``--execute``."""
    argv = ["ssh"]
    if cfg.vps_ssh_key:
        argv += ["-i", cfg.vps_ssh_key]
    argv += ["-p", str(cfg.vps_port),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{cfg.vps_user}@{cfg.vps_host}",
             f"echo {VPS_OK_MARKER}"]
    return argv


def build_collect_cmd(cfg: Config) -> list:
    """rsync the configured runtime source into the local ``runtime_data`` dir.
    ``--delete`` mirrors the source (REPLACES local runtime_data) so this is gated
    behind ``--execute``. Built as an explicit argv array."""
    ssh = "ssh"
    if cfg.vps_ssh_key:
        ssh += f" -i {shlex.quote(cfg.vps_ssh_key)}"
    ssh += f" -p {cfg.vps_port}"
    dest = cfg.runtime_data_dir.rstrip("/\\") + "/"
    return ["rsync", "-az", "--delete", "-e", ssh, cfg.runtime_source, dest]


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #
def redact_command(argv, cfg: Config) -> str:
    """Render a command for DISPLAY with any secret config values masked. Used so a
    dry-run never prints a VPS host / key / source path."""
    secrets = {getattr(cfg, k) for k in SECRET_KEYS if getattr(cfg, k, "")}
    # also mask "user@host" and the embedded ssh '-i <key>' inside an -e string
    parts = []
    for tok in argv:
        tok = str(tok)
        masked = tok
        for s in secrets:
            if s and s in masked:
                masked = masked.replace(s, "<redacted>")
        if "@" in masked and cfg.vps_host and "<redacted>" not in masked:
            # belt-and-suspenders: mask any residual user@host token
            masked = "<redacted-target>"
        parts.append(masked)
    return " ".join(shlex.quote(p) if " " not in p else f'"{p}"' for p in parts)


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def find_latest_report_dir(output_dir: Path) -> Optional[Path]:
    """Return the most-recently-modified report subdirectory under ``output_dir``,
    or ``output_dir`` itself if it has files but no subdirs. None if nothing."""
    if not output_dir.is_dir():
        return None
    subdirs = [p for p in output_dir.iterdir() if p.is_dir()]
    if subdirs:
        return max(subdirs, key=lambda p: p.stat().st_mtime)
    if any(p.is_file() and p.suffix != ".zip" for p in output_dir.iterdir()):
        return output_dir
    return None


def build_package_path(output_dir: Path, now: _dt.datetime) -> Path:
    ts = now.strftime("%Y%m%d_%H%M%S")
    return output_dir / f"hermes_inspection_package_{ts}.zip"


def make_package(src_dir: Path, dest_zip: Path) -> int:
    """Zip every non-zip file under ``src_dir`` into ``dest_zip``. Returns file count."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file() and p.suffix != ".zip" and p.resolve() != dest_zip.resolve():
                zf.write(p, arcname=str(p.relative_to(src_dir)))
                count += 1
    return count


# --------------------------------------------------------------------------- #
# Provenance & freshness guardrails (Phase 2)
# --------------------------------------------------------------------------- #
def _iso(now: _dt.datetime) -> str:
    return now.replace(microsecond=0).isoformat()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def capture_repo_state(ctx: "Ctx") -> dict:
    """SECRET-FREE snapshot of the git workspace: local HEAD, origin/main HEAD,
    cleanliness, and whether they are in sync. Unknown fields are None/False so a
    caller can never mistake 'could not verify' for 'verified safe'."""
    dirty = probe_repo_dirty(ctx)
    local = probe_local_head(ctx)
    remote = probe_remote_head(ctx)
    in_sync = bool(local and remote and local == remote)
    return {
        "local_head": local,
        "remote_main_head": remote,
        "repo_clean": (None if dirty is None else (not dirty)),
        "in_sync_with_main": in_sync if (local and remote) else None,
    }


def report_provenance_path(report_dir: Path) -> Path:
    return report_dir / REPORT_PROVENANCE


def read_report_provenance(report_dir: Optional[Path]) -> Optional[dict]:
    if report_dir is None:
        return None
    return _read_json(report_provenance_path(report_dir))


def write_report_provenance(ctx: "Ctx", report_dir: Path, state: dict, *,
                            validation_at: Optional[str] = None,
                            validation_epoch: Optional[float] = None,
                            validation_result_path: Optional[str] = None) -> dict:
    """Write/merge the report-level provenance INSIDE the report bundle. Contains only
    non-secret git evidence + timestamps. Never includes VPS host/user/key/source."""
    now = ctx.now_fn()
    prov = read_report_provenance(report_dir) or {}
    prov.setdefault("report_generated_at", _iso(now))
    prov.setdefault("report_generated_epoch", now.timestamp())
    prov["report_dir"] = str(report_dir.relative_to(ctx.repo_root)) \
        if _is_relative(report_dir, ctx.repo_root) else report_dir.name
    prov["local_head"] = state.get("local_head")
    prov["remote_main_head"] = state.get("remote_main_head")
    prov["repo_clean"] = state.get("repo_clean")
    prov["in_sync_with_main"] = state.get("in_sync_with_main")
    if validation_at is not None:
        prov["validation_completed_at"] = validation_at
        prov["validation_epoch"] = validation_epoch
        prov["validation_result_path"] = validation_result_path
    _write_json(report_provenance_path(report_dir), prov)
    return prov


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def assess_package_freshness(ctx: "Ctx", report_dir: Optional[Path]):
    """Return (ok, reasons, state, prov). A package is fresh ONLY if every invariant
    holds: report exists + has provenance, repo clean, local==origin/main now, the
    report's recorded git HEAD matches the current HEAD, and validation ran AFTER the
    report was generated."""
    state = capture_repo_state(ctx)
    prov = read_report_provenance(report_dir)
    reasons: list = []
    if report_dir is None:
        reasons.append("no inspection report found (run report/fresh-package first)")
    if report_dir is not None and prov is None:
        reasons.append("report has no provenance (generated outside the fresh flow)")
    if state["repo_clean"] is False:
        reasons.append("repo is dirty (main must be the source of truth)")
    if state["repo_clean"] is None:
        reasons.append("could not read git status")
    if state["in_sync_with_main"] is None:
        reasons.append("could not determine local/remote main hashes")
    elif state["in_sync_with_main"] is False:
        reasons.append("local HEAD differs from origin/main")
    if prov is not None:
        if prov.get("local_head") and state.get("local_head") \
                and prov["local_head"] != state["local_head"]:
            reasons.append("report git evidence does not match current local HEAD")
        if not prov.get("validation_completed_at"):
            reasons.append("validation did not run after report generation")
        elif prov.get("validation_epoch") is not None \
                and prov.get("report_generated_epoch") is not None \
                and prov["validation_epoch"] < prov["report_generated_epoch"]:
            reasons.append("validation ran before the report was generated")
    return (not reasons, reasons, state, prov)


def build_package_provenance(ctx: "Ctx", *, report_dir: Path, state: dict,
                             report_prov: Optional[dict], package_path: Path) -> dict:
    """Assemble the SECRET-FREE package provenance written into every uploaded zip."""
    now = ctx.now_fn()
    rp = report_prov or {}
    return {
        "package_created_at": _iso(now),
        "report_generated_at": rp.get("report_generated_at"),
        "validation_completed_at": rp.get("validation_completed_at"),
        "local_head": state.get("local_head"),
        "remote_main_head": state.get("remote_main_head"),
        "repo_clean": state.get("repo_clean"),
        "in_sync_with_main": state.get("in_sync_with_main"),
        "report_dir": (str(report_dir.relative_to(ctx.repo_root))
                       if _is_relative(report_dir, ctx.repo_root) else report_dir.name),
        "validation_result_path": rp.get("validation_result_path"),
        "package_path": (str(package_path.relative_to(ctx.repo_root))
                         if _is_relative(package_path, ctx.repo_root) else package_path.name),
        "laptop_agent_script_path": SCRIPT_REL_PATH,
        "generated_by": "laptop_agent.fresh-package",
    }


def archive_stale_reports(ctx: "Ctx", out_dir: Path) -> Optional[Path]:
    """Move an existing inspection_reports dir to a timestamped, git-ignored stale
    folder so a fresh report can NEVER reuse old evidence. Returns the archive path."""
    if not out_dir.exists():
        return None
    ts = ctx.now_fn().strftime("%Y%m%d_%H%M%S")
    dest = ctx.repo_root / f"{STALE_DIR_PREFIX}{ts}"
    n = 1
    while dest.exists():
        dest = ctx.repo_root / f"{STALE_DIR_PREFIX}{ts}_{n}"
        n += 1
    out_dir.rename(dest)
    return dest


# --------------------------------------------------------------------------- #
# Output context
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    cfg: Config
    config_found: bool
    execute: bool
    repo_root: Path
    runner: Runner
    printer: Callable[[str], None]
    now_fn: Callable[[], _dt.datetime]
    allow_stale: bool = False                      # --allow-stale-package override
    planned: list = field(default_factory=list)   # commands we WOULD run (dry-run)
    executed: list = field(default_factory=list)   # commands we actually ran

    def say(self, msg: str = "") -> None:
        self.printer(msg)

    def run_readonly(self, argv, timeout: int = 30):
        """Run a non-destructive probe; allowed even in dry-run."""
        self.executed.append(list(argv))
        return self.runner(list(argv), cwd=str(self.repo_root), timeout=timeout)

    def run_action(self, argv, *, label: str, timeout: int = 1800,
                   redact: bool = False) -> Optional["tuple[int, str, str]"]:
        """Run (or, in dry-run, only display) a side-effecting/destructive command.
        Requires ``--execute``; otherwise the exact command is printed and skipped."""
        shown = redact_command(argv, self.cfg) if redact else " ".join(
            shlex.quote(str(t)) for t in argv)
        if not self.execute:
            self.planned.append(list(argv))
            self.say(f"  [DRY-RUN] would run {label}:")
            self.say(f"    {shown}")
            self.say(f"    (add --execute to run this)")
            return None
        self.say(f"  [EXECUTE] {label}:")
        self.say(f"    {shown}")
        self.executed.append(list(argv))
        return self.runner(list(argv), cwd=str(self.repo_root), timeout=timeout)


# --------------------------------------------------------------------------- #
# Probes used by several commands
# --------------------------------------------------------------------------- #
def probe_local_head(ctx: Ctx) -> Optional[str]:
    rc, out, _ = ctx.run_readonly(git_local_head_cmd())
    return out.strip() if rc == 0 and out.strip() else None


def probe_remote_head(ctx: Ctx) -> Optional[str]:
    rc, out, _ = ctx.run_readonly(git_remote_head_cmd(ctx.cfg))
    if rc != 0 or not out.strip():
        return None
    # ls-remote: "<sha>\trefs/heads/main"
    return out.split()[0].strip() or None


def probe_repo_dirty(ctx: Ctx) -> Optional[bool]:
    rc, out, _ = ctx.run_readonly(git_status_cmd())
    if rc != 0:
        return None
    return bool(out.strip())


def probe_docker(ctx: Ctx) -> Optional[str]:
    rc, out, _ = ctx.run_readonly(docker_version_cmd(), timeout=20)
    return out.strip() if rc == 0 and out.strip() else None


def runtime_data_present(ctx: Ctx) -> bool:
    d = ctx.repo_root / ctx.cfg.runtime_data_dir
    return d.is_dir() and any(d.iterdir())


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_status(ctx: Ctx) -> int:
    c = ctx.cfg
    ctx.say("=" * 64)
    ctx.say("Laptop Hermes Agent — operator status (read-only)")
    ctx.say("=" * 64)
    ctx.say(f"mode            : {'EXECUTE' if ctx.execute else 'DRY-RUN (safe default)'}")
    summ = c.public_summary()
    for k, v in summ.items():
        ctx.say(f"  {k:<26}: {v}")

    dirty = probe_repo_dirty(ctx)
    local = probe_local_head(ctx)
    remote = probe_remote_head(ctx)
    docker = probe_docker(ctx)
    rt_present = runtime_data_present(ctx)

    ctx.say("")
    ctx.say(f"  repo_clean                : {None if dirty is None else (not dirty)}")
    ctx.say(f"  local_head                : {local or 'unknown'}")
    ctx.say(f"  remote_main_head          : {remote or 'unknown'}")
    in_sync = (local is not None and remote is not None and local == remote)
    ctx.say(f"  in_sync_with_main         : {in_sync if (local and remote) else 'unknown'}")
    ctx.say(f"  docker_available          : {bool(docker)}"
            + (f" (server {docker})" if docker else ""))
    ctx.say(f"  runtime_data_present      : {rt_present}")
    ctx.say(f"  report_dir_present        : {(ctx.repo_root / c.inspection_output_dir).is_dir()}")

    # SAFE / STOP decision — conservative: any unknown/dirty/out-of-sync => STOP.
    reasons = []
    if dirty:
        reasons.append("local repo has uncommitted changes (main is source of truth)")
    if dirty is None:
        reasons.append("could not read git status")
    if local and remote and not in_sync:
        reasons.append("local HEAD is not in sync with origin/main")
    if not (local and remote):
        reasons.append("could not determine local/remote main hashes")

    ctx.say("")
    safe = not reasons
    ctx.say("-" * 64)
    if safe:
        ctx.say("DECISION: SAFE TO CONTINUE")
    else:
        ctx.say("DECISION: STOP")
        for r in reasons:
            ctx.say(f"  - {r}")

    # exact next command
    nxt = _next_command(ctx, safe=safe, reasons=reasons, rt_present=rt_present)
    ctx.say(f"NEXT COMMAND: {nxt}")
    # ChatGPT upload guidance
    report_ready = (ctx.repo_root / c.inspection_output_dir).is_dir() and \
        find_latest_report_dir(ctx.repo_root / c.inspection_output_dir) is not None
    ctx.say(f"UPLOAD REPORT TO CHATGPT: {'yes' if report_ready else 'no (generate a report first)'}")
    ctx.say("-" * 64)
    return 0 if safe else 3


def _next_command(ctx: Ctx, *, safe: bool, reasons: list, rt_present: bool) -> str:
    c = ctx.cfg
    if not ctx.config_found and (c.vps_configured() is False):
        # config not strictly required for status, but guide the operator
        pass
    if reasons:
        if any("uncommitted" in r for r in reasons):
            return "review your changes, then `git stash` or commit — keep main as source of truth"
        if any("sync" in r for r in reasons):
            return f"git pull {c.git_remote} {c.git_main_branch}"
        return "git fetch && re-run: python scripts/laptop_agent.py status"
    if not rt_present:
        if c.collect_configured():
            return "python scripts/laptop_agent.py collect --execute"
        return ("configure runtime_source in .laptop_agent.local.json, then "
                "`python scripts/laptop_agent.py collect --execute`")
    return ("python scripts/laptop_agent.py fresh-package --execute"
            "   (builds a fresh, provenance-verified zip to upload to ChatGPT)")


def cmd_repo_status(ctx: Ctx) -> int:
    rc, out, err = ctx.run_readonly(git_status_cmd())
    if rc != 0:
        ctx.say(f"git status failed: {err.strip() or rc}")
        return 1
    if out.strip():
        ctx.say("local repo has uncommitted changes:")
        ctx.say(out.rstrip())
    else:
        ctx.say("local repo is clean.")
    return 0


def cmd_local_head(ctx: Ctx) -> int:
    h = probe_local_head(ctx)
    ctx.say(h or "unknown (not a git repo?)")
    return 0 if h else 1


def cmd_remote_head(ctx: Ctx) -> int:
    h = probe_remote_head(ctx)
    ctx.say(h or "unknown (no network / remote not reachable?)")
    return 0 if h else 1


def cmd_verify_sync(ctx: Ctx) -> int:
    local = probe_local_head(ctx)
    remote = probe_remote_head(ctx)
    ctx.say(f"local_head       : {local or 'unknown'}")
    ctx.say(f"remote_main_head : {remote or 'unknown'}")
    if not (local and remote):
        ctx.say("DECISION: STOP — could not determine both hashes.")
        ctx.say(f"NEXT COMMAND: git fetch {ctx.cfg.git_remote} {ctx.cfg.git_main_branch}")
        return 3
    if local == remote:
        ctx.say("DECISION: SAFE TO CONTINUE — local matches origin/main.")
        return 0
    ctx.say("DECISION: STOP — local HEAD differs from origin/main.")
    ctx.say(f"NEXT COMMAND: git pull {ctx.cfg.git_remote} {ctx.cfg.git_main_branch}")
    return 3


def cmd_check_docker(ctx: Ctx) -> int:
    v = probe_docker(ctx)
    if v:
        ctx.say(f"docker available — server version {v}")
        return 0
    ctx.say("docker NOT available (is Docker Desktop running?)")
    return 1


def cmd_check_vps(ctx: Ctx) -> int:
    if not ctx.config_found or not ctx.cfg.vps_configured():
        ctx.say(setup_message())
        return 2
    argv = build_vps_check_cmd(ctx.cfg)
    res = ctx.run_action(argv, label="VPS SSH connectivity probe",
                         timeout=20, redact=True)
    if res is None:
        return 0  # dry-run printed the (redacted) command
    rc, out, err = res
    if rc == 0 and VPS_OK_MARKER in out:
        ctx.say("DECISION: SAFE TO CONTINUE — VPS reachable over SSH.")
        return 0
    ctx.say(f"DECISION: STOP — VPS SSH probe failed (rc={rc}).")
    if err.strip():
        ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
    return 3


def cmd_collect(ctx: Ctx) -> int:
    if not ctx.config_found or not ctx.cfg.collect_configured():
        ctx.say(setup_message())
        ctx.say("  (set 'runtime_source' to copy artifacts into runtime_data)")
        return 2
    argv = build_collect_cmd(ctx.cfg)
    ctx.say("Collect runtime artifacts from the configured runtime source.")
    ctx.say("  NOTE: this REPLACES local runtime_data (rsync --delete).")
    res = ctx.run_action(argv, label="collect runtime_data", timeout=1800, redact=True)
    if res is None:
        return 0
    rc, _out, err = res
    if rc == 0:
        ctx.say("collected runtime_data successfully.")
        ctx.say("NEXT COMMAND: python scripts/laptop_agent.py report --execute")
        return 0
    ctx.say(f"collect failed (rc={rc}).")
    if err.strip():
        ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
    return 1


def cmd_report(ctx: Ctx) -> int:
    if not runtime_data_present(ctx):
        ctx.say("runtime_data is missing or empty — cannot build a light-mode report.")
        if ctx.cfg.collect_configured():
            ctx.say("NEXT COMMAND: python scripts/laptop_agent.py collect --execute")
        else:
            ctx.say("Provide runtime_data (e.g. configure runtime_source and run 'collect').")
        return 2
    argv = build_inspection_report_cmd(ctx.cfg)
    res = ctx.run_action(argv, label="generate light-mode inspection report",
                         timeout=3600)
    if res is None:
        return 0
    rc, out, err = res
    if out.strip():
        ctx.say(out.rstrip())
    if rc == 0:
        # stamp report provenance so a later `package` can prove freshness
        latest = find_latest_report_dir(ctx.repo_root / ctx.cfg.inspection_output_dir)
        if latest is not None:
            write_report_provenance(ctx, latest, capture_repo_state(ctx))
        ctx.say("report generated.")
        ctx.say("NEXT COMMAND: python scripts/laptop_agent.py validate --execute")
        return 0
    ctx.say(f"report generation failed (rc={rc}).")
    if err.strip():
        ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
    return 1


def _record_validation(ctx: Ctx, rc: int, out: str, err: str) -> None:
    """Persist validation output + timestamp into the latest report's provenance so a
    package can prove validation ran AFTER report generation. Records pass/fail
    honestly — a failure is never hidden."""
    latest = find_latest_report_dir(ctx.repo_root / ctx.cfg.inspection_output_dir)
    if latest is None:
        return
    now = ctx.now_fn()
    result_path = latest / VALIDATION_RESULT
    body = (f"validation_rc={rc}\ncompleted_at={_iso(now)}\n\n"
            f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
    try:
        result_path.write_text(body, encoding="utf-8")
    except OSError:
        result_path = None
    write_report_provenance(
        ctx, latest, capture_repo_state(ctx), validation_at=_iso(now),
        validation_epoch=now.timestamp(),
        validation_result_path=(VALIDATION_RESULT if result_path else None))


def cmd_validate(ctx: Ctx) -> int:
    argv = build_validate_cmd(ctx.cfg)
    res = ctx.run_action(argv, label="validate training runtime", timeout=600)
    if res is None:
        return 0
    rc, out, err = res
    if out.strip():
        ctx.say(out.rstrip())
    _record_validation(ctx, rc, out, err)
    if rc == 0:
        ctx.say("DECISION: SAFE TO CONTINUE — runtime validation passed.")
        return 0
    # NEVER hide a validation failure.
    ctx.say(f"DECISION: STOP — runtime validation FAILED (rc={rc}). Do not proceed.")
    if err.strip():
        ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
    return 3


def _finalize_package(ctx: Ctx, *, report_dir: Path, state: dict,
                      report_prov: Optional[dict], out_dir: Path) -> Path:
    """Write the package provenance INTO the report dir, then zip it (so the proof
    travels inside the uploaded package). Returns the zip path."""
    dest = build_package_path(out_dir, ctx.now_fn())
    pkg_prov = build_package_provenance(ctx, report_dir=report_dir, state=state,
                                        report_prov=report_prov, package_path=dest)
    _write_json(report_dir / PACKAGE_PROVENANCE, pkg_prov)
    make_package(report_dir, dest)
    return dest


def cmd_package(ctx: Ctx) -> int:
    out_dir = ctx.repo_root / ctx.cfg.inspection_output_dir
    latest = find_latest_report_dir(out_dir)
    ok, reasons, state, prov = assess_package_freshness(ctx, latest)

    if not ok:
        if not ctx.allow_stale:
            # DEFAULT: STOP — never silently zip stale reports.
            ctx.say("DECISION: STOP — refusing to package a STALE/unverified report.")
            for r in reasons:
                ctx.say(f"  - {r}")
            ctx.say("NEXT COMMAND: python scripts/laptop_agent.py fresh-package --execute")
            ctx.say("  (or, only if you understand the risk, re-run with --allow-stale-package)")
            return 3
        ctx.say("WARNING: --allow-stale-package set — packaging an UNVERIFIED report.")
        for r in reasons:
            ctx.say(f"  - (ignored) {r}")

    if latest is None:   # nothing to zip even under override
        ctx.say("no inspection report found to package.")
        ctx.say("NEXT COMMAND: python scripts/laptop_agent.py fresh-package --execute")
        return 2

    if not ctx.execute:
        dest = build_package_path(out_dir, ctx.now_fn())
        ctx.say("  [DRY-RUN] would package the verified report:")
        ctx.say(f"    source : {latest}")
        ctx.say(f"    zip    : {dest}")
        ctx.say(f"    + write {PACKAGE_PROVENANCE} into the package")
        ctx.say("    (add --execute to create the zip)")
        return 0

    dest = _finalize_package(ctx, report_dir=latest, state=state,
                             report_prov=prov, out_dir=out_dir)
    ctx.say(f"packaged verified report -> {dest}")
    ctx.say("UPLOAD REPORT TO CHATGPT: yes — upload the zip above for an independent judge.")
    return 0


def cmd_fresh_package(ctx: Ctx) -> int:
    """Provenance-guarded packaging: refuse a dirty/ahead repo, archive any stale
    reports, generate a fresh report, validate, prove freshness, then zip with
    provenance. This is the command operators should use to build an upload."""
    out_dir = ctx.repo_root / ctx.cfg.inspection_output_dir
    state = capture_repo_state(ctx)

    gate = []
    if state["repo_clean"] is False:
        gate.append("repo is dirty (commit/stash; main is the source of truth)")
    if state["repo_clean"] is None:
        gate.append("could not read git status")
    if state["in_sync_with_main"] is None:
        gate.append("could not determine local/remote main hashes (no network?)")
    elif state["in_sync_with_main"] is False:
        gate.append("local HEAD differs from origin/main")
    if gate:
        ctx.say("DECISION: STOP — workspace is not clean & in sync with origin/main.")
        for r in gate:
            ctx.say(f"  - {r}")
        if any("differs" in r for r in gate):
            ctx.say(f"NEXT COMMAND: git pull {ctx.cfg.git_remote} {ctx.cfg.git_main_branch}")
        elif any("dirty" in r for r in gate):
            ctx.say("NEXT COMMAND: review changes, then `git stash` or commit")
        else:
            ctx.say(f"NEXT COMMAND: git fetch {ctx.cfg.git_remote} {ctx.cfg.git_main_branch}")
        return 3

    report_cmd = build_inspection_report_cmd(ctx.cfg)
    validate_cmd = build_validate_cmd(ctx.cfg)

    if not ctx.execute:
        ctx.say("  [DRY-RUN] fresh-package would, in order (mutating NOTHING now):")
        ctx.say(f"    1. archive any existing '{ctx.cfg.inspection_output_dir}' -> "
                f"{STALE_DIR_PREFIX}<timestamp>/ (git-ignored)")
        ctx.say(f"    2. {' '.join(shlex.quote(t) for t in report_cmd)}")
        ctx.say(f"    3. {' '.join(shlex.quote(t) for t in validate_cmd)}")
        ctx.say(f"    4. write {REPORT_PROVENANCE} + verify report/validation freshness")
        ctx.say(f"    5. write {PACKAGE_PROVENANCE} and create a timestamped zip")
        ctx.say("    (add --execute to run this sequence)")
        return 0

    if not runtime_data_present(ctx):
        ctx.say("runtime_data is missing or empty — cannot build a report.")
        if ctx.cfg.collect_configured():
            ctx.say("NEXT COMMAND: python scripts/laptop_agent.py collect --execute")
        return 2

    # 1) archive stale reports so nothing old can be reused
    archived = archive_stale_reports(ctx, out_dir)
    if archived is not None:
        ctx.say(f"archived stale reports -> {archived.name}/")

    # 2) generate the fresh report
    ctx.say(f"  [EXECUTE] {' '.join(shlex.quote(t) for t in report_cmd)}")
    rc, out, err = ctx.runner(report_cmd, cwd=str(ctx.repo_root), timeout=3600)
    if out.strip():
        ctx.say(out.rstrip())
    if rc != 0:
        ctx.say(f"DECISION: STOP — report generation failed (rc={rc}).")
        if err.strip():
            ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
        return 3
    latest = find_latest_report_dir(out_dir)
    if latest is None:
        ctx.say("DECISION: STOP — report command produced no bundle directory.")
        return 3
    report_state = capture_repo_state(ctx)
    write_report_provenance(ctx, latest, report_state)

    # 3) validate AFTER the report, and record it
    ctx.say(f"  [EXECUTE] {' '.join(shlex.quote(t) for t in validate_cmd)}")
    vrc, vout, verr = ctx.runner(validate_cmd, cwd=str(ctx.repo_root), timeout=600)
    if vout.strip():
        ctx.say(vout.rstrip())
    _record_validation(ctx, vrc, vout, verr)
    if vrc != 0:
        ctx.say(f"DECISION: STOP — validation FAILED (rc={vrc}). Not packaging.")
        if verr.strip():
            ctx.say(f"  detail: {verr.strip().splitlines()[-1]}")
        return 3

    # 4) prove freshness end-to-end
    ok, reasons, final_state, prov = assess_package_freshness(ctx, latest)
    if not ok:
        ctx.say("DECISION: STOP — freshness check failed after generation:")
        for r in reasons:
            ctx.say(f"  - {r}")
        return 3

    # 5) write package provenance + zip
    dest = _finalize_package(ctx, report_dir=latest, state=final_state,
                             report_prov=prov, out_dir=out_dir)
    ctx.say("DECISION: SAFE TO CONTINUE — fresh, verified package created.")
    ctx.say(f"PACKAGE: {dest}")
    ctx.say(f"UPLOAD REPORT TO CHATGPT: yes — upload this exact zip:\n  {dest}")
    return 0


COMMANDS = {
    "status": cmd_status,
    "repo-status": cmd_repo_status,
    "local-head": cmd_local_head,
    "remote-head": cmd_remote_head,
    "verify-sync": cmd_verify_sync,
    "check-docker": cmd_check_docker,
    "check-vps": cmd_check_vps,
    "collect": cmd_collect,
    "report": cmd_report,
    "validate": cmd_validate,
    "package": cmd_package,
    "fresh-package": cmd_fresh_package,
}


# --------------------------------------------------------------------------- #
# Parser & main
# --------------------------------------------------------------------------- #
def _add_shared_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true",
                        help="actually run side-effecting/VPS/runtime_data commands "
                             "(default is a safe DRY-RUN that only prints them)")
    parser.add_argument("--dry-run", action="store_true",
                        help="force dry-run (default). Always wins over --execute.")
    parser.add_argument("--config", default=None,
                        help="path to a local operator config (default: "
                             f"{CONFIG_JSON} or {CONFIG_ENV})")


def build_parser() -> argparse.ArgumentParser:
    # A parent parser carries the shared flags so they work BEFORE or AFTER the
    # subcommand (e.g. `status --dry-run` and `--execute collect`).
    shared = argparse.ArgumentParser(add_help=False)
    _add_shared_flags(shared)
    p = argparse.ArgumentParser(
        prog="laptop_agent", parents=[shared],
        description="Laptop Hermes Agent — SAFE local operator layer (Phase 1). "
                    "Automates operator chores only; never trades or loosens gates.")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    helps = {
        "status": "read-only overview + SAFE/STOP + next command",
        "repo-status": "show local repo status (porcelain)",
        "local-head": "print local HEAD hash",
        "remote-head": "print origin/main hash (read-only)",
        "verify-sync": "verify local matches origin/main",
        "check-docker": "check Docker availability",
        "check-vps": "check VPS SSH connectivity (needs --execute)",
        "collect": "copy runtime artifacts into runtime_data (needs --execute)",
        "report": "run the light-mode inspection report (needs --execute)",
        "validate": "run training-runtime validation (needs --execute)",
        "package": "zip the latest report ONLY if provenance is fresh (needs --execute)",
        "fresh-package": "clean+sync gated: archive stale, report, validate, prove, "
                         "zip with provenance (needs --execute)",
    }
    for name in COMMANDS:
        sp = sub.add_parser(name, parents=[shared], help=helps.get(name, ""))
        if name == "package":
            sp.add_argument(
                "--allow-stale-package", action="store_true",
                help="DANGER: package even when freshness checks fail. Default is STOP.")
    return p


def main(argv=None, *, runner: Optional[Runner] = None,
         printer: Optional[Callable[[str], None]] = None,
         now_fn: Optional[Callable[[], _dt.datetime]] = None,
         repo_root: Path = REPO_ROOT, env: Optional[dict] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    printer = printer or print
    runner = runner or default_runner
    now_fn = now_fn or _dt.datetime.now

    if not args.command:
        parser.print_help()
        return 0

    # DRY-RUN IS DEFAULT. --execute is the only way to turn it off; --dry-run is a
    # no-op safety affirmation and always wins over nothing.
    execute = bool(args.execute) and not bool(args.dry_run)

    cfg, found = load_config(repo_root=repo_root, explicit_path=args.config, env=env)
    ctx = Ctx(cfg=cfg, config_found=found, execute=execute, repo_root=repo_root,
              runner=runner, printer=printer, now_fn=now_fn,
              allow_stale=bool(getattr(args, "allow_stale_package", False)))

    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover — argparse restricts choices
        parser.print_help()
        return 2
    return handler(ctx)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
