#!/usr/bin/env python3
"""Laptop Hermes Agent — coordinator CLI (Phase 2).

Drives the real operator handoff workflow from a laptop (PowerShell or any shell):

    Laptop -> GitHub main -> Vultr VPS -> collect light-mode report
           -> package artifact -> ChatGPT inspection handoff

This is **coordinator / runtime-operator tooling only**. It NEVER changes trading
strategy, trade gates, paper-realism, or live-trading behavior, and it NEVER prints
secrets (VPS host/user/key) or API keys.

Commands
--------
* ``doctor``               local environment check (repo/plugin path, git, ssh/scp,
                           python, artifact dirs).
* ``vps-smoke``            read-only VPS checks over SSH (reachable, remote plugin path
                           exists, Docker present, hermes-training inspectable).
* ``collect-light-report`` run the standard light-report collection on the VPS and copy
                           a timestamped zip back to the laptop artifact directory.
* ``handoff-summary``      print a concise ChatGPT upload checklist for the latest zip.

Config is loaded from ``.laptop_agent.json`` (template: ``.laptop_agent.example.json``).
The config file is git-ignored and is never committed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_CONFIG = ".laptop_agent.json"
EXAMPLE_CONFIG = ".laptop_agent.example.json"
DEFAULT_CONTAINER = "hermes-training"
VPS_OK_MARKER = "hermes-coordinator-ok"

# Module the report generator + tests require; a Python that can't import it is useless
# for report generation (the exact `ModuleNotFoundError: pydantic` failure).
REMOTE_REQUIRED_IMPORT = "pydantic"

# Candidate remote interpreters, in PREFERENCE order: a dependency-capable project venv
# first (.report_venv is what scripts/vps_generate_light_report.sh builds), then plugin/
# repo/root venvs, then bare python3/python. The plugin dir is the CWD on the remote.
REMOTE_PY_CANDIDATES = (
    "./.report_venv/bin/python ./.venv/bin/python ../.venv/bin/python "
    "../../.venv/bin/python ./venv/bin/python python3 python"
)
# Documented one-command setup that creates a dependency-capable venv on the VPS.
REMOTE_DEP_FIX = "bash scripts/vps_generate_light_report.sh"


def remote_python_select(*, on_fail_exit: bool) -> str:
    """Shell snippet (run from the plugin dir) that picks a DEPENDENCY-CAPABLE remote
    Python: it tries each candidate in preference order and uses the FIRST one that can
    ``import pydantic``. Prints ``remote python: <path>`` on success. On failure it
    prints which candidates were tested + the exact safe fix command, then either
    exits 12 (``on_fail_exit``) or prints ``NO_DEP_PYTHON`` (probe mode). It NEVER
    auto-installs packages."""
    fail = (
        'echo "FATAL: no dependency-capable Python on the VPS (none could import '
        f'{REMOTE_REQUIRED_IMPORT})." 1>&2; '
        f'echo "tested candidates: {REMOTE_PY_CANDIDATES}" 1>&2; '
        f'echo "FIX (safe, no manual pip): run \\"{REMOTE_DEP_FIX}\\" in the plugin dir '
        'to build .report_venv and install all report dependencies." 1>&2; '
        + ("exit 12" if on_fail_exit else 'echo "NO_DEP_PYTHON"'))
    return (
        'PYBIN=""; '
        f'for cand in {REMOTE_PY_CANDIDATES}; do '
        'c="$cand"; '
        'case "$cand" in */*) [ -x "$c" ] || continue;; '
        '*) c="$(command -v "$cand" 2>/dev/null)" || continue;; esac; '
        '[ -n "$c" ] || continue; '
        f'if "$c" -c "import {REMOTE_REQUIRED_IMPORT}" >/dev/null 2>&1; '
        'then PYBIN="$c"; break; fi; '
        'done; '
        f'if [ -z "$PYBIN" ]; then {fail}; else echo "remote python: $PYBIN"; fi'
    )

# Config keys whose VALUES must never be printed.
SECRET_KEYS = frozenset({"vps_host", "vps_user", "vps_ssh_key"})

# Required (non-secret-leaking) fields the operator must set.
REQUIRED_FIELDS = ("repo_root", "plugin_path", "vps_host", "vps_user",
                   "vps_remote_plugin_path", "local_artifact_dir")

REPORT_SCRIPT = "scripts/generate_bot_inspection_report.py"
VALIDATE_SCRIPT = "scripts/validate_training_runtime.py"
VALIDATION_FILE = "validation_light_latest.txt"
INSPECTION_SUMMARY = "runtime_data/inspection_summary.json"

# Live / real-money flags that must NEVER be enabled for the mechanical paper workflow.
# If any is truthy the coordinator STOPS immediately (it never enables a live path and
# never loosens a gate — it only refuses to proceed). Mirrors engine.aggressive_paper.
LIVE_FORBIDDEN_FLAGS = (
    "BTC_PULSE_LIVE_ENABLED", "BTC_AUTOTRADE_ENABLED", "GUARDED_LIVE_ENABLED",
    "MICRO_LIVE_ENABLED", "MICRO_LIVE_EXECUTION_ENABLED",
    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION", "ARB_EXECUTION_ENABLED",
    "HTE_AUTOTRADE", "LIVE_TRADING_ENABLED", "REAL_MONEY_ENABLED",
)


def _flag_truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on") if v is not None else False


def detect_live_flags(env=None) -> list:
    """Return the list of LIVE_FORBIDDEN_FLAGS that are currently TRUTHY in ``env``
    (defaults to ``os.environ``). Pure + read-only; used to fail closed before any
    mechanical step or approved paper run."""
    import os as _os
    e = env if env is not None else _os.environ
    return [f for f in LIVE_FORBIDDEN_FLAGS if _flag_truthy(e.get(f))]

Runner = Callable[..., "tuple[int, str, str]"]


# --------------------------------------------------------------------------- #
# Default subprocess runner (injectable for tests)
# --------------------------------------------------------------------------- #
def default_runner(argv, cwd=None, timeout: int = 120):
    """Run ``argv`` (explicit array — never a shell string) and return
    ``(rc, stdout, stderr)``. Never raises on non-zero exit."""
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd) if cwd else None,
                              timeout=timeout, capture_output=True, text=True, shell=False)
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError as exc:
        return 127, "", f"{exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    repo_root: str = ""
    plugin_path: str = ""
    vps_host: str = ""
    vps_user: str = ""
    vps_port: int = 22
    vps_ssh_key: str = ""
    vps_remote_plugin_path: str = ""
    local_artifact_dir: str = "inspection_reports_artifacts"
    hermes_training_container: str = DEFAULT_CONTAINER
    source_file: str = ""

    def missing_required(self) -> list:
        return [f for f in REQUIRED_FIELDS if not str(getattr(self, f, "")).strip()]

    def public_summary(self) -> dict:
        """SECRET-FREE view: report which required fields are SET, never their values."""
        out = {"config_source": self.source_file or "(none)"}
        for f in REQUIRED_FIELDS:
            val = str(getattr(self, f, "")).strip()
            # secret fields => only report set/unset; non-secret => echo the value
            if f in SECRET_KEYS:
                out[f] = "set" if val else "MISSING"
            else:
                out[f] = val or "MISSING"
        out["vps_port"] = self.vps_port
        out["vps_ssh_key"] = "set" if str(self.vps_ssh_key).strip() else "(default agent/key)"
        out["hermes_training_container"] = self.hermes_training_container
        return out


def _coerce_int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


# Config-load status codes (distinct so doctor never mislabels a real file as missing).
LOAD_OK = "ok"
LOAD_MISSING = "missing"
LOAD_UNREADABLE = "unreadable"
LOAD_PARSE_ERROR = "parse_error"


@dataclass
class ConfigLoad:
    cfg: Config
    status: str
    detail: str = ""
    path: str = ""

    @property
    def found(self) -> bool:
        return self.status == LOAD_OK


def load_config(path: Path) -> ConfigLoad:
    """Load coordinator config from JSON, tolerating a Windows UTF-8 BOM
    (``utf-8-sig``). Returns a :class:`ConfigLoad` that DISTINGUISHES missing vs.
    unreadable vs. JSON parse error — a present-but-broken file is NEVER reported as
    simply 'missing'. Never raises."""
    resolved = str(path.resolve()) if path else str(path)
    if not path.is_file():
        return ConfigLoad(Config(), LOAD_MISSING, f"no file at {resolved}", resolved)
    try:
        # utf-8-sig transparently strips a leading BOM that Notepad adds on Windows.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return ConfigLoad(Config(), LOAD_UNREADABLE, f"{type(exc).__name__}: {exc}", resolved)
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return ConfigLoad(Config(), LOAD_PARSE_ERROR, f"JSON parse error: {exc}", resolved)
    if not isinstance(raw, dict):
        return ConfigLoad(Config(), LOAD_PARSE_ERROR,
                          "top-level JSON must be an object {…}", resolved)
    cfg = Config()
    cfg.source_file = path.name
    cfg.repo_root = str(raw.get("repo_root") or "")
    cfg.plugin_path = str(raw.get("plugin_path") or "")
    cfg.vps_host = str(raw.get("vps_host") or "")
    cfg.vps_user = str(raw.get("vps_user") or "")
    cfg.vps_port = _coerce_int(raw.get("vps_port"), 22)
    cfg.vps_ssh_key = str(raw.get("vps_ssh_key") or raw.get("ssh_key") or "")
    cfg.vps_remote_plugin_path = str(raw.get("vps_remote_plugin_path") or "")
    cfg.local_artifact_dir = str(raw.get("local_artifact_dir")
                                 or raw.get("artifact_dir") or "inspection_reports_artifacts")
    cfg.hermes_training_container = str(raw.get("hermes_training_container")
                                        or DEFAULT_CONTAINER)
    return ConfigLoad(cfg, LOAD_OK, "", resolved)


def validate_ssh_key(cfg: Config):
    """Validate ``vps_ssh_key`` (when set). Returns ``(ok, message)``.

    Catches the two most common Windows mistakes: pasting the PUBLIC key text
    (``ssh-ed25519 …``) instead of a file path, and pointing at a key file that does
    not exist. Never prints key contents."""
    key = str(cfg.vps_ssh_key or "").strip()
    if not key:
        return True, "no key set (will use the SSH agent / default key)"
    low = key.lower()
    if low.startswith(("ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2", "sk-ssh")):
        return False, ("vps_ssh_key looks like a PUBLIC key (it starts with a key type "
                       "like 'ssh-ed25519'). Set it to the PRIVATE key FILE PATH instead, "
                       "e.g. C:\\Users\\you\\.ssh\\hermes_vps_ed25519")
    if "-----begin" in low:
        return False, ("vps_ssh_key contains private-key TEXT. Set it to the private "
                       "key FILE PATH instead (do not paste the key body).")
    if not Path(key).expanduser().is_file():
        return False, "vps_ssh_key path does not point to an existing file"
    return True, "private key file found"


def load_message(load: ConfigLoad) -> str:
    """Operator-facing message for a non-OK config load (exact, never 'just missing')."""
    if load.status == LOAD_MISSING:
        return (f"No coordinator config found at '{load.path}'.\n"
                f"  -> Create one: python scripts/laptop_agent_coordinator.py init-config\n"
                f"     (or copy '{EXAMPLE_CONFIG}' to '{DEFAULT_CONFIG}' and fill it in).\n"
                f"  The file is git-ignored and is NEVER committed; secrets are never printed.")
    if load.status == LOAD_PARSE_ERROR:
        return (f"Config at '{load.path}' could not be parsed (it EXISTS but is invalid).\n"
                f"  detail: {load.detail}\n"
                f"  -> If you edited it in Notepad and see a 'BOM' error, recreate it "
                f"cleanly: python scripts/laptop_agent_coordinator.py init-config --force")
    if load.status == LOAD_UNREADABLE:
        return f"Config at '{load.path}' could not be read.\n  detail: {load.detail}"
    return ""


def setup_message(path_name: str) -> str:   # back-compat thin wrapper
    return (f"No coordinator config found at '{path_name}'.\n"
            f"  -> Run: python scripts/laptop_agent_coordinator.py init-config\n"
            f"     (or copy '{EXAMPLE_CONFIG}' to '{DEFAULT_CONFIG}').")


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #
def redact(argv, cfg: Config) -> str:
    """Render a command for display with secret values (host/user/key) masked."""
    secrets = set()
    for k in SECRET_KEYS:
        v = str(getattr(cfg, k, "") or "")
        if v:
            secrets.add(v)
            secrets.add(v.rstrip("/\\"))
    secrets = sorted((s for s in secrets if s), key=len, reverse=True)
    parts = []
    for tok in argv:
        s = str(tok)
        for sec in secrets:
            if sec and sec in s:
                s = s.replace(sec, "<redacted>")
        # belt-and-suspenders: never leak a residual user@host
        if cfg.vps_host and (cfg.vps_host in str(tok) or "@" in s) and "<redacted>" not in s:
            s = s.replace("@", "@<redacted>") if "@" in s else s
        parts.append(s)
    return " ".join(shlex.quote(p) for p in parts)


# --------------------------------------------------------------------------- #
# Command builders (pure — explicit argv arrays; unit-testable)
# --------------------------------------------------------------------------- #
def _ssh_base(cfg: Config) -> list:
    argv = ["ssh"]
    if cfg.vps_ssh_key:
        argv += ["-i", cfg.vps_ssh_key]
    argv += ["-p", str(cfg.vps_port),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new"]
    return argv


def build_ssh_cmd(cfg: Config, remote_cmd: str) -> list:
    """SSH that runs ``remote_cmd`` (a single remote shell string) on the VPS."""
    return _ssh_base(cfg) + [f"{cfg.vps_user}@{cfg.vps_host}", remote_cmd]


def build_scp_pull_cmd(cfg: Config, remote_path: str, local_dir: str) -> list:
    """scp a remote file back to the local artifact directory (note: scp uses -P)."""
    argv = ["scp"]
    if cfg.vps_ssh_key:
        argv += ["-i", cfg.vps_ssh_key]
    argv += ["-P", str(cfg.vps_port),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{cfg.vps_user}@{cfg.vps_host}:{remote_path}",
             local_dir.rstrip("/\\") + "/"]
    return argv


# Canonical VPS report runner + the COMPLETE bundle zip it always produces. The
# coordinator no longer builds its own thin zip — it runs this script (which refreshes
# runtime_data, regenerates the light report, validates, and packages the FULL bundle
# via scripts/_report_bundle.py with a git_commit_proof.txt) and copies the latest zip.
CANONICAL_REPORT_SCRIPT = "scripts/vps_generate_light_report.sh"
CANONICAL_REPORT_ZIP = "vps_light_report_latest.zip"
GIT_PROOF_MEMBER = "git_commit_proof.txt"

# A non-thin light bundle must contain ALL of these (substring/suffix matched against the
# zip's member names). Refusing a thin zip is what stops the repeated 4-file failure.
REQUIRED_ZIP_MARKERS = (
    "report.json", "report.md", "git_commit_proof.txt", "validation_light_latest.txt",
    "metrics/", "samples/", "runtime_data/metrics",
)


def remote_zip_name(now: _dt.datetime) -> str:
    # the canonical runner always overwrites this stable name (a timestamped copy is
    # also kept on the VPS); the coordinator copies the stable one back.
    return CANONICAL_REPORT_ZIP


def zip_completeness(names) -> "tuple[bool, list]":
    """Return (is_complete, missing_markers) for a report zip's member list. A complete
    light bundle has report.json + report.md + metrics + samples + validation output +
    git_commit_proof.txt + runtime_data/metrics."""
    present = list(names or [])
    missing = []
    for marker in REQUIRED_ZIP_MARKERS:
        if "/" in marker:                       # path marker -> substring match
            ok = any(marker in n for n in present)
        else:                                   # bare filename -> suffix match
            ok = any(n.endswith(marker) for n in present)
        if not ok:
            missing.append(marker)
    return (not missing), missing


def build_remote_collect_script(cfg: Config) -> str:
    """The remote shell workflow run over SSH for collect-light-report / collect-report.

    Runs the CANONICAL self-bootstrapping VPS runner (which builds the .report_venv,
    refreshes runtime_data from the container, regenerates the light report, validates,
    and packages the COMPLETE bundle into vps_light_report_latest.zip via
    scripts/_report_bundle.py — never a thin zip). PAPER ONLY; read-only collection."""
    plugin = cfg.vps_remote_plugin_path
    return (
        f"cd {shlex.quote(plugin)} || {{ echo 'cannot cd to plugin' 1>&2; exit 12; }}; "
        f"bash {CANONICAL_REPORT_SCRIPT}")


def _legacy_build_remote_collect_script(cfg: Config, remote_zip: str) -> str:
    """DEPRECATED inline zip workflow (kept only for reference; no longer used). The
    canonical path is build_remote_collect_script -> vps_generate_light_report.sh."""
    plugin = cfg.vps_remote_plugin_path
    container = cfg.hermes_training_container
    return (
        f"cd {shlex.quote(plugin)} || {{ echo 'cannot cd to plugin' 1>&2; exit 12; }}; "
        f"{remote_python_select(on_fail_exit=True)}; "
        f"set -e; "
        f"rm -rf runtime_data; "
        f"docker cp {shlex.quote(container)}:/data runtime_data; "
        f'"$PYBIN" scripts/generate_bot_inspection_report.py '
        f"--output inspection_reports --data-dir runtime_data --bundle-mode light; "
        f'"$PYBIN" scripts/validate_training_runtime.py --data-dir runtime_data '
        f"| tee {VALIDATION_FILE}; "
        f"rm -f {shlex.quote(remote_zip)}; "
        f"zip -r {shlex.quote(remote_zip)} inspection_reports "
        f"{INSPECTION_SUMMARY} {VALIDATION_FILE}"
    )


def build_remote_python_probe(cfg: Config) -> list:
    """SSH probe that selects a dependency-capable remote Python from the plugin dir and
    prints ``remote python: <path>`` (or ``NO_DEP_PYTHON`` + the fix). Never exits non-zero
    for a missing candidate, so vps-smoke can read + report the result."""
    return build_ssh_cmd(
        cfg, f"cd {shlex.quote(cfg.vps_remote_plugin_path)} 2>/dev/null; "
             + remote_python_select(on_fail_exit=False))


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    cfg: Config
    config_found: bool
    repo_root: Path
    runner: Runner
    printer: Callable[[str], None]
    now_fn: Callable[[], _dt.datetime]
    which_fn: Callable[[str], Optional[str]] = shutil.which
    config_status: str = LOAD_OK
    config_detail: str = ""
    config_path: str = ""

    def say(self, msg: str = "") -> None:
        self.printer(msg)

    def run(self, argv, *, timeout: int = 120, redact_display: bool = False, cwd=None):
        shown = redact(argv, self.cfg) if redact_display else " ".join(
            shlex.quote(str(t)) for t in argv)
        self.say(f"  $ {shown}")
        return self.runner(list(argv), cwd=str(cwd or self.repo_root), timeout=timeout)


def _check(ctx: Ctx, label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    ctx.say(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(ctx: Ctx) -> int:
    cfg = ctx.cfg
    ctx.say("Laptop coordinator — doctor (local environment, read-only)")
    ctx.say("-" * 60)
    # ALWAYS show the resolved config path + whether it parsed (so a present-but-broken
    # file is never silently mislabeled as missing).
    ctx.say(f"  config path (resolved)    : {ctx.config_path or '(none)'}")
    ctx.say(f"  config parsed             : {'yes' if ctx.config_found else 'NO (' + ctx.config_status + ')'}")
    if not ctx.config_found:
        ctx.say("-" * 60)
        ctx.say(load_message(ConfigLoad(cfg, ctx.config_status, ctx.config_detail, ctx.config_path)))
        return 2
    for k, v in cfg.public_summary().items():
        ctx.say(f"  {k:<26}: {v}")
    ctx.say("-" * 60)
    missing = cfg.missing_required()
    results = []
    results.append(_check(ctx, "required config fields set", not missing,
                          "" if not missing else f"missing: {', '.join(missing)}"))

    repo = Path(cfg.repo_root) if cfg.repo_root else ctx.repo_root
    plugin = Path(cfg.plugin_path) if cfg.plugin_path else ctx.repo_root
    results.append(_check(ctx, "repo root exists", repo.is_dir(), str(repo)))
    plugin_ok = plugin.is_dir() and (plugin / REPORT_SCRIPT).is_file()
    results.append(_check(ctx, "plugin path + report script present", plugin_ok, str(plugin)))

    git_ok = bool(ctx.which_fn("git"))
    results.append(_check(ctx, "git available", git_ok))
    branch = ""
    if git_ok and repo.is_dir():
        rc, out, _ = ctx.runner(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=str(ctx.repo_root), timeout=20)
        branch = out.strip() if rc == 0 else ""
        results.append(_check(ctx, "git branch/status readable", bool(branch),
                              f"branch={branch}" if branch else "could not read"))
    else:
        results.append(_check(ctx, "git branch/status readable", False))

    results.append(_check(ctx, "ssh available", bool(ctx.which_fn("ssh"))))
    results.append(_check(ctx, "scp available", bool(ctx.which_fn("scp"))))
    key_ok, key_msg = validate_ssh_key(cfg)
    results.append(_check(ctx, "vps_ssh_key valid", key_ok, key_msg))
    results.append(_check(ctx, "python can run local scripts",
                          bool(sys.executable) and plugin_ok))

    art = (repo / cfg.local_artifact_dir) if not Path(cfg.local_artifact_dir).is_absolute() \
        else Path(cfg.local_artifact_dir)
    try:
        art.mkdir(parents=True, exist_ok=True)
        art_ok = art.is_dir()
    except OSError:
        art_ok = False
    results.append(_check(ctx, "artifact directory creatable", art_ok, str(art)))

    ok = all(results)
    ctx.say("-" * 60)
    ctx.say(f"DOCTOR: {'SAFE TO CONTINUE' if ok else 'STOP — fix the FAIL items above'}")
    if ok:
        ctx.say("NEXT: python scripts/laptop_agent_coordinator.py vps-smoke "
                f"--config {cfg.source_file or DEFAULT_CONFIG}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# vps-smoke
# --------------------------------------------------------------------------- #
def cmd_vps_smoke(ctx: Ctx) -> int:
    cfg = ctx.cfg
    if not ctx.config_found:
        ctx.say(load_message(ConfigLoad(cfg, ctx.config_status, ctx.config_detail, ctx.config_path)))
        return 2
    missing = cfg.missing_required()
    if missing:
        ctx.say(f"STOP — config missing required fields: {', '.join(missing)}")
        return 2
    key_ok, key_msg = validate_ssh_key(cfg)
    if not key_ok:
        ctx.say(f"STOP — {key_msg}")
        return 2
    ctx.say("Laptop coordinator — VPS smoke test (read-only over SSH)")
    ctx.say("-" * 60)
    results = []

    rc, out, err = ctx.run(build_ssh_cmd(cfg, f"echo {VPS_OK_MARKER}"),
                           timeout=20, redact_display=True)
    results.append(_check(ctx, "SSH connection works", rc == 0 and VPS_OK_MARKER in out,
                          "" if rc == 0 else (err.strip().splitlines() or [""])[-1]))

    # Use the SAME `cd` that collect uses (authoritative; avoids `test -d` false
    # negatives) and surface stderr on failure.
    rc, out, err = ctx.run(
        build_ssh_cmd(cfg, f"cd {shlex.quote(cfg.vps_remote_plugin_path)} "
                           f"&& echo {VPS_OK_MARKER}"),
        timeout=20, redact_display=True)
    results.append(_check(ctx, "remote plugin path exists", rc == 0 and VPS_OK_MARKER in out,
                          "" if rc == 0 else (err.strip().splitlines() or ["cd failed"])[-1]))

    rc, out, _ = ctx.run(build_ssh_cmd(cfg, "docker version --format '{{.Server.Version}}' "
                                            "|| docker --version"),
                         timeout=25, redact_display=True)
    results.append(_check(ctx, "Docker available on VPS", rc == 0 and bool(out.strip()),
                          out.strip().splitlines()[0] if out.strip() else ""))

    # remote Python preflight: collection needs a DEPENDENCY-CAPABLE Python (can import
    # pydantic), not merely any python3. Report which executable is selected + the dep
    # check result, with the exact fix if none qualifies.
    rc, out, _ = ctx.run(build_remote_python_probe(cfg), timeout=25, redact_display=True)
    sel = ""
    for ln in out.splitlines():
        if ln.startswith("remote python: "):
            sel = ln.split("remote python: ", 1)[1].strip()
    py_ok = bool(sel) and "NO_DEP_PYTHON" not in out
    results.append(_check(
        ctx, f"remote Python can import {REMOTE_REQUIRED_IMPORT}", py_ok,
        f"will use {sel}" if py_ok
        else f"no dependency-capable Python; FIX: run `{REMOTE_DEP_FIX}` on the VPS"))

    # hermes-training is OPTIONAL — report status if present, don't fail if absent.
    rc, out, _ = ctx.run(
        build_ssh_cmd(cfg, f"docker inspect -f '{{{{.State.Status}}}}' "
                           f"{shlex.quote(cfg.hermes_training_container)} 2>/dev/null "
                           f"|| echo absent"),
        timeout=25, redact_display=True)
    status = out.strip().splitlines()[-1] if out.strip() else "unknown"
    ctx.say(f"  [INFO] hermes-training container: {status}")

    ok = all(results)
    ctx.say("-" * 60)
    ctx.say(f"VPS SMOKE: {'SAFE TO CONTINUE' if ok else 'STOP — VPS checks failed'}")
    if ok:
        ctx.say("NEXT: python scripts/laptop_agent_coordinator.py collect-light-report "
                f"--config {cfg.source_file or DEFAULT_CONFIG}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# collect-light-report
# --------------------------------------------------------------------------- #
def cmd_collect_light_report(ctx: Ctx, *, dry_run: bool = False) -> int:
    cfg = ctx.cfg
    if not ctx.config_found:
        ctx.say(load_message(ConfigLoad(cfg, ctx.config_status, ctx.config_detail, ctx.config_path)))
        return 2
    missing = cfg.missing_required()
    if missing:
        ctx.say(f"STOP — config missing required fields: {', '.join(missing)}")
        return 2
    key_ok, key_msg = validate_ssh_key(cfg)
    if not key_ok:
        ctx.say(f"STOP — {key_msg}")
        return 2

    zip_name = CANONICAL_REPORT_ZIP
    remote_zip = f"{cfg.vps_remote_plugin_path.rstrip('/')}/{zip_name}"
    remote_script = build_remote_collect_script(cfg)
    ssh_cmd = build_ssh_cmd(cfg, remote_script)
    art = (ctx.repo_root / cfg.local_artifact_dir) \
        if not Path(cfg.local_artifact_dir).is_absolute() else Path(cfg.local_artifact_dir)
    scp_cmd = build_scp_pull_cmd(cfg, remote_zip, str(art))

    ctx.say("Laptop coordinator — collect light-mode report from the VPS (canonical)")
    ctx.say(f"  NOTE: runs the canonical {CANONICAL_REPORT_SCRIPT} on the VPS (refreshes")
    ctx.say("  runtime_data, regenerates the light report, validates, packages the FULL")
    ctx.say(f"  bundle into {CANONICAL_REPORT_ZIP}), then copies that zip back here.")
    ctx.say("-" * 60)
    ctx.say("  remote workflow (run over SSH):")
    for step in remote_script.split("; "):
        ctx.say(f"    - {step}")

    if dry_run:
        ctx.say("  [DRY-RUN] would run (secrets redacted):")
        ctx.say(f"    {redact(ssh_cmd, cfg)}")
        ctx.say(f"    {redact(scp_cmd, cfg)}")
        ctx.say("  (omit --dry-run to execute)")
        return 0

    art.mkdir(parents=True, exist_ok=True)
    rc, out, err = ctx.run(ssh_cmd, timeout=3600, redact_display=True)
    if out.strip():
        ctx.say(out.rstrip())
    if rc != 0:
        ctx.say(f"STOP — remote collection failed (rc={rc}).")
        if err.strip():
            ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
        return 1
    rc, _out, err = ctx.run(scp_cmd, timeout=600, redact_display=True)
    if rc != 0:
        ctx.say(f"STOP — copying the zip back failed (rc={rc}).")
        if err.strip():
            ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
        return 1
    local_zip = art / zip_name
    # Refuse a THIN zip: the canonical bundle must carry report.json/md + metrics +
    # samples + validation output + git_commit_proof.txt + runtime_data/metrics.
    try:
        with zipfile.ZipFile(local_zip) as zf:
            complete, missing = zip_completeness(zf.namelist())
    except (OSError, zipfile.BadZipFile):
        complete, missing = False, ["<unreadable zip>"]
    ctx.say("-" * 60)
    if not complete:
        ctx.say(f"STOP — collected report is THIN/incomplete (missing: {missing}). "
                f"Re-run {CANONICAL_REPORT_SCRIPT} on the VPS; not shipping a thin zip.")
        return 1
    ctx.say(f"COLLECTED (complete bundle): {local_zip}")
    ctx.say("NEXT: python scripts/laptop_agent_coordinator.py handoff-summary "
            f"--config {cfg.source_file or DEFAULT_CONFIG}")
    return 0


# --------------------------------------------------------------------------- #
# handoff-summary
# --------------------------------------------------------------------------- #
def _latest_zip(art: Path) -> Optional[Path]:
    if not art.is_dir():
        return None
    # prefer the canonical VPS bundle name(s), then any timestamped light report, then *.zip
    for pat in ("vps_light_report*.zip", "hermes_light_report_*.zip", "*.zip"):
        zips = [p for p in art.glob(pat) if p.is_file()]
        if zips:
            return max(zips, key=lambda p: p.stat().st_mtime)
    return None


def cmd_handoff_summary(ctx: Ctx) -> int:
    cfg = ctx.cfg
    art = (ctx.repo_root / cfg.local_artifact_dir) \
        if not Path(cfg.local_artifact_dir).is_absolute() else Path(cfg.local_artifact_dir)
    z = _latest_zip(art)
    ctx.say("Laptop coordinator — ChatGPT inspection handoff checklist")
    ctx.say("=" * 60)
    if z is None:
        ctx.say(f"  report zip            : NONE found in {art}")
        ctx.say("  -> run: python scripts/laptop_agent_coordinator.py "
                f"collect-light-report --config {cfg.source_file or DEFAULT_CONFIG}")
        return 2
    names = []
    try:
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile):
        names = []
    complete, missing = zip_completeness(names)
    has_validation = any(n.endswith(VALIDATION_FILE) for n in names)
    has_summary = any(n.endswith("inspection_summary.json") for n in names)
    has_proof = any(n.endswith(GIT_PROOF_MEMBER) for n in names)
    ts = _dt.datetime.fromtimestamp(z.stat().st_mtime).replace(microsecond=0).isoformat()
    ctx.say(f"  report zip            : {z}")
    ctx.say(f"  generated (mtime)     : {ts}")
    ctx.say(f"  validation file       : {'included' if has_validation else 'MISSING'} "
            f"({VALIDATION_FILE})")
    ctx.say(f"  inspection_summary    : {'included' if has_summary else 'MISSING'} "
            f"(runtime_data/inspection_summary.json)")
    ctx.say(f"  git_commit_proof      : {'included' if has_proof else 'MISSING'}")
    ctx.say(f"  total files in zip    : {len(names)}")
    if not complete:
        ctx.say("=" * 60)
        ctx.say(f"  REFUSED — THIN/incomplete report bundle. Missing: {missing}")
        ctx.say(f"  -> re-run a full collection: python scripts/laptop_agent_coordinator.py "
                f"collect-report --config {cfg.source_file or DEFAULT_CONFIG}")
        return 1
    ctx.say("=" * 60)
    ctx.say("COMPLETE BUNDLE — NEXT: upload the zip above to ChatGPT for inspection.")
    return 0


# --------------------------------------------------------------------------- #
# init-config
# --------------------------------------------------------------------------- #
SAFE_DEFAULT_CONFIG = {
    "repo_root": "",
    "plugin_path": "",
    "vps_host": "",
    "vps_user": "ubuntu",
    "vps_port": 22,
    "vps_ssh_key": "",
    "vps_remote_plugin_path": "",
    "local_artifact_dir": "inspection_reports_artifacts",
    "hermes_training_container": DEFAULT_CONTAINER,
}

# Only these (non-secret, coordinator) keys are seeded from the example template.
_TEMPLATE_KEYS = tuple(SAFE_DEFAULT_CONFIG.keys())
# Fields that must NEVER be seeded with a value (operator fills them locally).
_NEVER_SEED = frozenset({"vps_host", "vps_ssh_key"})


def _example_path() -> Path:
    return Path(__file__).resolve().parents[1] / EXAMPLE_CONFIG


def build_init_config() -> dict:
    """Build a clean, SECRET-FREE config dict for ``init-config``. Seeds non-secret
    coordinator defaults from ``.laptop_agent.example.json`` when present (BOM-safe);
    host/key are always left blank for the operator to fill in locally."""
    data = dict(SAFE_DEFAULT_CONFIG)
    ex = _example_path()
    try:
        if ex.is_file():
            raw = json.loads(ex.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                for k in _TEMPLATE_KEYS:
                    if k in _NEVER_SEED:
                        continue
                    v = raw.get(k)
                    if v not in (None, ""):
                        data[k] = v
    except (OSError, ValueError):
        pass
    return data


def cmd_init_config(ctx: Ctx, *, target: Path, force: bool = False) -> int:
    """Write a clean, BOM-free ``.laptop_agent.json`` (no secrets). UTF-8 without a
    BOM so Windows Notepad edits + the loader agree."""
    if target.exists() and not force:
        ctx.say(f"{target} already exists. Re-run with --force to overwrite.")
        return 1
    data = build_init_config()
    try:
        # json.dumps + utf-8 => NO BOM (the exact bug this fixes).
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        ctx.say(f"STOP — could not write {target}: {exc}")
        return 1
    ctx.say(f"wrote clean (BOM-free, secret-free) config: {target.resolve()}")
    ctx.say("  Now fill in: repo_root, plugin_path, vps_host, vps_user, vps_ssh_key "
            "(PRIVATE key file path), vps_remote_plugin_path.")
    ctx.say(f"  Then run: python scripts/laptop_agent_coordinator.py doctor "
            f"--config {target.name}")
    return 0


# --------------------------------------------------------------------------- #
# Phase 5: autonomous operator loop with human/ChatGPT approval gates
# --------------------------------------------------------------------------- #
LEDGER_NAME = "artifact_index.jsonl"
UPLOAD_INSTRUCTIONS = "CHATGPT_UPLOAD_INSTRUCTIONS.md"
CURSOR_HANDOFF_DIR = "cursor_handoffs"

DECISION_LABELS = ("LONG_RUN_APPROVED", "SHORT_TEST_ONLY", "CURSOR_PROMPT_REQUIRED",
                   "STOP_REQUIRED", "UNKNOWN_REVIEW_REQUIRED")


def _artifact_dir(ctx: Ctx) -> Path:
    d = ctx.cfg.local_artifact_dir
    return (ctx.repo_root / d) if not Path(d).is_absolute() else Path(d)


def _local_commit(ctx: Ctx) -> str:
    rc, out, _ = ctx.runner(["git", "rev-parse", "HEAD"], cwd=str(ctx.repo_root), timeout=20)
    return out.strip() if rc == 0 else ""


def _append_ledger(ctx: Ctx, record: dict) -> None:
    """Append one cycle record to the artifact ledger (JSONL). Never raises."""
    art = _artifact_dir(ctx)
    try:
        art.mkdir(parents=True, exist_ok=True)
        with (art / LEDGER_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


# Explicit, machine-readable decision tokens an operator (or ChatGPT) can paste so the
# coordinator never has to guess. These take ABSOLUTE priority over fuzzy text.
EXPLICIT_DECISION_TOKENS = ("LONG_RUN_APPROVED", "SHORT_TEST_ONLY",
                            "CURSOR_PROMPT_REQUIRED", "STOP_REQUIRED",
                            "UNKNOWN_REVIEW_REQUIRED")
# The exact approved start command that corroborates an explicit LONG_RUN_APPROVED.
LONG_RUN_APPROVED_FLAGS = "--mode long --approved-by-chatgpt"


def _explicit_decision_tokens(text: str) -> list:
    """Return the DISTINCT explicit decision tokens present as whole words (uppercase),
    in first-seen order. Whole-word match avoids matching a token inside another word."""
    found = []
    for tok in EXPLICIT_DECISION_TOKENS:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(tok) + r"(?![A-Za-z0-9_])", text or ""):
            if tok not in found:
                found.append(tok)
    return found


def _has_long_run_support(text: str) -> bool:
    """LONG_RUN_APPROVED must be corroborated by the exact approved start command OR by
    explicit paper-safety language (so a bare token can never approve a multi-hour run)."""
    t = (text or "").lower()
    if LONG_RUN_APPROVED_FLAGS in t:
        return True
    safety_kw = ("do not enable live trading", "do not loosen", "do not change paper realism",
                 "do not use real money", "safe to continue", "safe to run",
                 "paper training", "paper-only", "paper only")
    return any(k in t for k in safety_kw)


def classify_chatgpt_decision_detail(text: str) -> dict:
    """Classify ChatGPT's decision, preferring EXPLICIT uppercase tokens over fuzzy text.

    Precedence + safety rules (the safe interpretation always wins):
      1. Explicit tokens take priority over fuzzy text.
      2. Multiple DISTINCT conflicting explicit tokens -> UNKNOWN_REVIEW_REQUIRED (the
         conflict is reported so the operator re-reads the response).
      3. A lone explicit LONG_RUN_APPROVED requires corroboration (the approved
         '--mode long --approved-by-chatgpt' command or explicit paper-safety language);
         otherwise it is downgraded to UNKNOWN_REVIEW_REQUIRED.
      4. With no explicit token, fall back to conservative fuzzy keyword matching
         (STOP > CURSOR > LONG > SHORT > UNKNOWN).
    Returns {label, source, explicit_tokens, conflict, reason}. The caller NEVER
    executes anything from this — it only maps to a recommended next *manual* command."""
    explicit = _explicit_decision_tokens(text)
    actionable = [t for t in explicit if t != "UNKNOWN_REVIEW_REQUIRED"]

    # (2) conflicting explicit decisions, or an explicit UNKNOWN mixed with others.
    if len(set(actionable)) > 1 or (("UNKNOWN_REVIEW_REQUIRED" in explicit) and actionable):
        return {"label": "UNKNOWN_REVIEW_REQUIRED", "source": "explicit_conflict",
                "explicit_tokens": explicit, "conflict": True,
                "reason": "conflicting explicit decision tokens: " + ", ".join(explicit)}

    if len(set(actionable)) == 1:
        tok = actionable[0]
        if tok == "LONG_RUN_APPROVED" and not _has_long_run_support(text):
            return {"label": "UNKNOWN_REVIEW_REQUIRED", "source": "explicit_long_uncorroborated",
                    "explicit_tokens": explicit, "conflict": False,
                    "reason": ("explicit LONG_RUN_APPROVED without the approved "
                               f"'{LONG_RUN_APPROVED_FLAGS}' command or paper-safety language")}
        return {"label": tok, "source": "explicit_token",
                "explicit_tokens": explicit, "conflict": False,
                "reason": f"explicit token {tok}"}

    if explicit == ["UNKNOWN_REVIEW_REQUIRED"]:
        return {"label": "UNKNOWN_REVIEW_REQUIRED", "source": "explicit_token",
                "explicit_tokens": explicit, "conflict": False,
                "reason": "explicit token UNKNOWN_REVIEW_REQUIRED"}

    # (4) no explicit token -> conservative fuzzy fallback.
    return {"label": _classify_chatgpt_decision_fuzzy(text), "source": "fuzzy",
            "explicit_tokens": [], "conflict": False, "reason": "fuzzy keyword match"}


def _classify_chatgpt_decision_fuzzy(text: str) -> str:
    """Conservative fuzzy fallback (no explicit token present). Precedence is
    STOP > CURSOR > LONG > SHORT > UNKNOWN so the safe interpretation always wins."""
    t = (text or "").lower()
    stop_kw = ("stop the run", "do not run", "don't run", "do not proceed",
               "not safe", "unsafe", "halt", "abort", "do not start", "stop the bot")
    cursor_kw = ("cursor prompt", "prompt for cursor", "paste into cursor", "send to cursor",
                 "open cursor", "use cursor", "code fix", "needs a fix", "needs fixing",
                 "patch", "repair", "cursor repair", "fix in cursor")
    long_kw = ("long run approved", "approve long", "approved for long", "long-run approved",
               "approved long run", "start the long run", "approve the long",
               "long paper run approved", "ok to run long", "11-hour", "11 hour",
               "multi-hour run", "multi-day run")
    short_kw = ("short test", "short run", "quick test", "smoke test", "short paper",
                "brief run", "short-run", "short paper run")
    if any(k in t for k in stop_kw):
        return "STOP_REQUIRED"
    if any(k in t for k in cursor_kw):
        return "CURSOR_PROMPT_REQUIRED"
    if any(k in t for k in long_kw):
        return "LONG_RUN_APPROVED"
    if any(k in t for k in short_kw):
        return "SHORT_TEST_ONLY"
    return "UNKNOWN_REVIEW_REQUIRED"


def classify_chatgpt_decision(text: str) -> str:
    """Back-compat string API: the classified label only (see
    :func:`classify_chatgpt_decision_detail` for tokens/conflict/reason)."""
    return classify_chatgpt_decision_detail(text)["label"]


def decision_next_command(label: str, cfg_name: str, decision_file: str = "<decision.md>") -> str:
    base = f"python scripts/laptop_agent_coordinator.py"
    if label == "STOP_REQUIRED":
        return ("STOP — do NOT start any run. Re-read ChatGPT's response. If code "
                "changes are needed, prepare a Cursor handoff.")
    if label == "CURSOR_PROMPT_REQUIRED":
        return f"{base} prepare-cursor-handoff --config {cfg_name} --file {decision_file}"
    if label == "LONG_RUN_APPROVED":
        return f"{base} start-paper-run --config {cfg_name} --mode long --approved-by-chatgpt"
    if label == "SHORT_TEST_ONLY":
        return f"{base} start-paper-run --config {cfg_name} --mode short"
    return ("Manual review required — re-read ChatGPT's response; the coordinator took "
            "NO automatic action. Re-run record-chatgpt-decision after clarifying.")


def extract_cursor_prompt(text: str) -> str:
    """Extract the Cursor prompt from a ChatGPT response: prefer the largest fenced
    code block, then a 'Cursor prompt:'-style section, else the whole text. Pure."""
    blocks = re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)```", text or "", re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    m = re.search(r"(?:cursor prompt|prompt for cursor|paste (?:this )?into cursor)"
                  r"\s*[:\-]?\s*\n(.*)", text or "", re.IGNORECASE | re.DOTALL)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return (text or "").strip()


def _read_text_file(path: Path):
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None


# ---- operator-cycle -------------------------------------------------------- #
def _hex_commit(text: str) -> str:
    """Extract a git commit hash (7–40 hex chars) from output, else '' (so a mocked or
    noisy SSH reply is never shown as a fake commit)."""
    for tok in (text or "").split():
        t = tok.strip()
        if 7 <= len(t) <= 40 and all(c in "0123456789abcdef" for c in t.lower()):
            return t
    return ""


def _remote_vps_commit(ctx: Ctx) -> str:
    """Read-only: the VPS repo's current commit at the remote plugin path. Never fails
    the cycle (informational); returns '' when unknown."""
    cfg = ctx.cfg
    try:
        rc, out, _ = ctx.run(
            build_ssh_cmd(cfg, f"cd {shlex.quote(cfg.vps_remote_plugin_path)} 2>/dev/null "
                               f"&& git rev-parse HEAD 2>/dev/null || true"),
            timeout=25, redact_display=True)
        return _hex_commit(out) if rc == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _remote_paper_status(ctx: Ctx) -> "tuple[bool, str]":
    """Read-only: (paper_training_running, container_status) from `docker inspect` of the
    hermes-training container. Never fails the cycle."""
    cfg = ctx.cfg
    try:
        rc, out, _ = ctx.run(
            build_ssh_cmd(cfg, f"docker inspect -f '{{{{.State.Status}}}}' "
                               f"{shlex.quote(cfg.hermes_training_container)} 2>/dev/null "
                               f"|| echo absent"), timeout=25, redact_display=True)
        status = out.strip().splitlines()[-1] if out.strip() else "unknown"
    except Exception:  # noqa: BLE001
        status = "unknown"
    return (status == "running"), status


def _write_cycle_blocker_handoff(ctx: Ctx, blockers: list) -> str:
    """Prepare (NEVER auto-run) a Cursor handoff FILE describing a mechanical blocker so
    the operator can paste it into web Cursor. Only called when a blocker is detected."""
    out_dir = ctx.repo_root / CURSOR_HANDOFF_DIR
    ts = ctx.now_fn().strftime("%Y%m%d_%H%M%S")
    body = (
        "# Cursor handoff — operator-cycle blocker\n\n"
        "The laptop coordinator hit a mechanical blocker while running the safe operator\n"
        "cycle. Investigate + fix in web Cursor (push to GitHub `main`, report the commit\n"
        "hash). Do NOT enable live trading or loosen any gate.\n\n"
        "## Blockers detected\n" + "".join(f"- {b}\n" for b in blockers) + "\n"
        "## After the fix\n"
        "```\n"
        f"python scripts/laptop_agent_coordinator.py post-cursor-verify "
        f"--config {ctx.cfg.source_file or DEFAULT_CONFIG}\n"
        "```\n")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"cursor_blocker_{ts}.md"
        dest.write_text(body, encoding="utf-8")
        return str(dest)
    except OSError:
        return ""


def _print_cycle_summary(ctx: Ctx, *, safe: bool, vps_commit: str, local_commit: str,
                         paper_running: bool, container_status: str, zip_path,
                         instr: str, cursor_needed: bool, cursor_file: str,
                         run_started: bool, mode: str) -> None:
    cfg_name = ctx.cfg.source_file or DEFAULT_CONFIG
    ctx.say("\n" + "=" * 64)
    ctx.say("OPERATOR CYCLE — FINAL STATUS")
    ctx.say("=" * 64)
    ctx.say(f"  RESULT              : {'SAFE TO CONTINUE' if safe else 'STOP'}")
    ctx.say(f"  local commit        : {local_commit or 'unknown'}")
    ctx.say(f"  VPS commit          : {vps_commit or 'unknown'}")
    ctx.say(f"  paper training      : {'RUNNING' if paper_running else 'NOT running'} "
            f"({container_status})")
    if run_started:
        ctx.say(f"  approved paper run  : STARTED (mode={mode}, PAPER ONLY, live disabled)")
    ctx.say(f"  report zip (local)  : {zip_path if zip_path else '(none)'}")
    ctx.say(f"  Cursor needed       : {'YES' if cursor_needed else 'no'}"
            + (f" -> {cursor_file}" if cursor_needed and cursor_file else ""))
    ctx.say("-" * 64)
    if zip_path is not None:
        ctx.say(f"  UPLOAD TO CHATGPT   : {zip_path}")
        ctx.say(f"  upload instructions : {instr or '(could not write)'}")
        ctx.say("  -> Upload that zip to ChatGPT and ask it to inspect the light report.")
    else:
        ctx.say("  UPLOAD TO CHATGPT   : (no report zip produced — see blockers above)")
    ctx.say("  (No ChatGPT free text is ever executed; Cursor is never auto-run.)")


def cmd_operator_cycle(ctx: Ctx, *, dry_run: bool = False,
                       approved_paper_run: bool = False, mode: str = "short") -> int:
    """ONE command for the non-coder operator. Runs the safe mechanical steps in order:
    verify local repo+config -> sync GitHub main -> verify VPS SSH+commit+Docker ->
    verify paper/live safety -> collect/generate the VPS light report -> copy the zip
    locally -> write the exact ChatGPT upload handoff. It NEVER starts a run unless
    ``--approved-paper-run`` is explicitly given, NEVER enables live trading, NEVER
    loosens a gate, NEVER executes ChatGPT free text, and NEVER auto-runs Cursor (it
    only prepares a Cursor handoff FILE when a blocker is detected)."""
    import os as _os
    cfg = ctx.cfg
    mode = (mode or "short").lower()
    blockers: list = []
    cursor_needed = False
    cursor_file = ""
    ctx.say("=" * 64)
    ctx.say("OPERATOR CYCLE — safe mechanical workflow (one command)")
    ctx.say("=" * 64)

    # (safety) fail closed on any live/micro-live/production flag BEFORE anything else.
    live = detect_live_flags(_os.environ)
    if live:
        ctx.say(f"\nSTOP — live/real-money flag(s) detected: {', '.join(live)}. "
                "This workflow is PAPER ONLY. Disable them and retry.")
        _print_cycle_summary(ctx, safe=False, vps_commit="", local_commit=_local_commit(ctx),
                             paper_running=False, container_status="unknown", zip_path=None,
                             instr="", cursor_needed=False, cursor_file="",
                             run_started=False, mode=mode)
        return 2

    ctx.say("\n[1/6] verify local repo path + config (doctor)")
    if cmd_doctor(ctx) != 0:
        blockers.append("local environment/config check failed (doctor)")
        cursor_file = _write_cycle_blocker_handoff(ctx, blockers)
        _print_cycle_summary(ctx, safe=False, vps_commit="", local_commit=_local_commit(ctx),
                             paper_running=False, container_status="unknown", zip_path=None,
                             instr="", cursor_needed=True, cursor_file=cursor_file,
                             run_started=False, mode=mode)
        return 2

    ctx.say("\n[2/6] sync GitHub main (fast-forward only)")
    if cmd_sync_main(ctx) != 0:
        blockers.append("could not safely sync GitHub main")
        _print_cycle_summary(ctx, safe=False, vps_commit="", local_commit=_local_commit(ctx),
                             paper_running=False, container_status="unknown", zip_path=None,
                             instr="", cursor_needed=False, cursor_file="",
                             run_started=False, mode=mode)
        return 2

    ctx.say("\n[3/6] verify VPS access + Docker (vps-smoke)")
    if cmd_vps_smoke(ctx) != 0:
        blockers.append("VPS access / Docker check failed (vps-smoke)")
        _print_cycle_summary(ctx, safe=False, vps_commit="", local_commit=_local_commit(ctx),
                             paper_running=False, container_status="unknown", zip_path=None,
                             instr="", cursor_needed=False, cursor_file="",
                             run_started=False, mode=mode)
        return 2

    ctx.say("\n[4/6] verify VPS commit + paper/live safety status")
    vps_commit = _remote_vps_commit(ctx)
    paper_running, container_status = _remote_paper_status(ctx)
    ctx.say(f"  VPS commit          : {vps_commit or 'unknown'}")
    ctx.say(f"  paper training      : {'RUNNING' if paper_running else 'NOT running'} "
            f"({container_status})")
    ctx.say("  live trading        : DISABLED (paper-only workflow; no gate changed)")

    ctx.say("\n[5/6] collect / generate the VPS light report (+ copy zip locally)")
    if cmd_collect_light_report(ctx, dry_run=dry_run) != 0:
        blockers.append("light-report collection failed")
        cursor_needed = True
    if dry_run:
        ctx.say("\n[DRY-RUN] cycle preview complete (nothing collected/started).")
        cursor_file = _write_cycle_blocker_handoff(ctx, blockers) if cursor_needed else ""
        _print_cycle_summary(ctx, safe=not blockers, vps_commit=vps_commit,
                             local_commit=_local_commit(ctx), paper_running=paper_running,
                             container_status=container_status, zip_path=None, instr="",
                             cursor_needed=cursor_needed, cursor_file=cursor_file,
                             run_started=False, mode=mode)
        return 0 if not blockers else 1

    art = _artifact_dir(ctx)
    z = _latest_zip(art)
    has_validation = has_summary = False
    if z is not None:
        try:
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
            has_validation = any(n.endswith(VALIDATION_FILE) for n in names)
            has_summary = any(n.endswith("inspection_summary.json") for n in names)
        except (OSError, zipfile.BadZipFile):
            pass
    if z is None or not (has_validation and has_summary):
        blockers.append("report zip incomplete (missing validation/inspection_summary)")
        cursor_needed = True

    ctx.say("\n[6/6] write the ChatGPT upload handoff")
    instr = _write_upload_instructions(ctx, z)

    # approved paper run (ONLY with explicit --approved-paper-run, and only if clean).
    run_started = False
    if approved_paper_run:
        if blockers:
            ctx.say("\nSTOP — blockers detected; refusing to start an approved paper run.")
        else:
            ctx.say(f"\n[run] approved paper run requested (mode={mode}) — PAPER ONLY")
            run_rc = cmd_start_paper_run(ctx, mode=mode, approved_by_chatgpt=True,
                                         dry_run=dry_run)
            run_started = (run_rc == 0)
            if not run_started:
                blockers.append(f"approved paper run failed to start (rc={run_rc})")
            else:
                paper_running, container_status = _remote_paper_status(ctx)

    if cursor_needed and not cursor_file:
        cursor_file = _write_cycle_blocker_handoff(ctx, blockers)

    _append_ledger(ctx, {
        "timestamp": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "event": "operator-cycle", "local_commit": _local_commit(ctx),
        "vps_commit": vps_commit, "report_zip": (str(z) if z else ""),
        "validation_present": has_validation, "inspection_summary_present": has_summary,
        "paper_running": bool(paper_running), "approved_paper_run": bool(approved_paper_run),
        "run_started": bool(run_started), "mode": mode, "blockers": blockers,
        "cursor_needed": bool(cursor_needed),
        "handoff_files": [UPLOAD_INSTRUCTIONS] if instr else [],
        "decision_classification": None})

    safe = not blockers
    _print_cycle_summary(ctx, safe=safe, vps_commit=vps_commit,
                         local_commit=_local_commit(ctx), paper_running=paper_running,
                         container_status=container_status, zip_path=z, instr=instr,
                         cursor_needed=cursor_needed, cursor_file=cursor_file,
                         run_started=run_started, mode=mode)
    return 0 if safe else 1


def _write_upload_instructions(ctx: Ctx, zip_path) -> str:
    art = _artifact_dir(ctx)
    cfg_name = ctx.cfg.source_file or DEFAULT_CONFIG
    body = (
        "# Upload this report to ChatGPT for inspection\n\n"
        f"**Report zip:** `{zip_path if zip_path else '(none found)'}`\n\n"
        "## Steps\n"
        "1. Upload the zip above to ChatGPT and ask it to inspect the light report.\n"
        "2. Save ChatGPT's full reply to a local `.md` file (e.g. `decision.md`).\n"
        "3. Classify the decision (the coordinator does this conservatively):\n"
        "   ```\n"
        f"   python scripts/laptop_agent_coordinator.py record-chatgpt-decision "
        f"--config {cfg_name} --file decision.md\n"
        "   ```\n\n"
        "## If ChatGPT says a CODE FIX is needed (Cursor)\n"
        "   ```\n"
        f"   python scripts/laptop_agent_coordinator.py prepare-cursor-handoff "
        f"--config {cfg_name} --file decision.md\n"
        "   ```\n"
        "   Paste the generated prompt into **web Cursor**. Web Cursor must push to\n"
        "   GitHub `main` and report the commit hash. Then verify:\n"
        "   ```\n"
        f"   python scripts/laptop_agent_coordinator.py post-cursor-verify --config {cfg_name}\n"
        "   ```\n\n"
        "## If ChatGPT APPROVES a long paper run\n"
        "   ```\n"
        f"   python scripts/laptop_agent_coordinator.py record-chatgpt-decision "
        f"--config {cfg_name} --file decision.md\n"
        f"   python scripts/laptop_agent_coordinator.py start-paper-run "
        f"--config {cfg_name} --mode long --approved-by-chatgpt\n"
        "   ```\n\n"
        "> Live trading stays DISABLED. The long run requires the explicit\n"
        "> `--approved-by-chatgpt` flag. No ChatGPT free text is ever executed as a shell command.\n")
    try:
        art.mkdir(parents=True, exist_ok=True)
        path = art / UPLOAD_INSTRUCTIONS
        path.write_text(body, encoding="utf-8")
        return str(path)
    except OSError:
        return ""


# ---- record-chatgpt-decision ----------------------------------------------- #
def cmd_record_chatgpt_decision(ctx: Ctx, *, file: str) -> int:
    """Save ChatGPT's response into the artifact folder and classify it CONSERVATIVELY.
    NEVER executes risky actions from free text — only prints the recommended next
    *manual* command."""
    cfg = ctx.cfg
    src = Path(file)
    text = _read_text_file(src)
    if text is None:
        ctx.say(f"STOP — could not read decision file: {file}")
        return 2
    detail = classify_chatgpt_decision_detail(text)
    label = detail["label"]
    art = _artifact_dir(ctx)
    ts = ctx.now_fn().strftime("%Y%m%d_%H%M%S")
    saved = ""
    try:
        art.mkdir(parents=True, exist_ok=True)
        dest = art / f"chatgpt_decision_{ts}.md"
        dest.write_text(text, encoding="utf-8")
        saved = str(dest)
    except OSError:
        pass
    ctx.say("Laptop coordinator — recorded ChatGPT decision")
    ctx.say("-" * 60)
    ctx.say(f"  saved copy        : {saved or '(could not write)'}")
    ctx.say(f"  classification    : {label}")
    if detail.get("explicit_tokens"):
        ctx.say(f"  explicit tokens   : {', '.join(detail['explicit_tokens'])} "
                f"(source={detail['source']})")
    if detail.get("conflict"):
        ctx.say(f"  CONFLICT          : {detail['reason']}")
    elif label == "UNKNOWN_REVIEW_REQUIRED" and detail.get("source") != "fuzzy":
        ctx.say(f"  review reason     : {detail['reason']}")
    ctx.say("  (no action was executed from the free text — classification only)")
    ctx.say(f"  RECOMMENDED NEXT  : {decision_next_command(label, cfg.source_file or DEFAULT_CONFIG, str(src))}")
    _append_ledger(ctx, {
        "timestamp": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "event": "record-chatgpt-decision", "local_commit": _local_commit(ctx),
        "decision_source": str(src), "decision_saved": saved,
        "decision_classification": label, "decision_source_kind": detail.get("source"),
        "decision_explicit_tokens": detail.get("explicit_tokens"),
        "decision_conflict": bool(detail.get("conflict"))})
    # exit codes: STOP -> 3 (do not proceed), UNKNOWN -> 1 (needs review), else 0
    if label == "STOP_REQUIRED":
        return 3
    if label == "UNKNOWN_REVIEW_REQUIRED":
        return 1
    return 0


# ---- prepare-cursor-handoff ------------------------------------------------ #
def cmd_prepare_cursor_handoff(ctx: Ctx, *, file: str) -> int:
    """Extract the Cursor prompt from ChatGPT's response and SAVE it to a file (never
    executes it). Reminds the operator to paste it into web Cursor, which must push to
    GitHub main and report the commit hash."""
    src = Path(file)
    text = _read_text_file(src)
    if text is None:
        ctx.say(f"STOP — could not read decision file: {file}")
        return 2
    prompt = extract_cursor_prompt(text)
    out_dir = ctx.repo_root / CURSOR_HANDOFF_DIR
    ts = ctx.now_fn().strftime("%Y%m%d_%H%M%S")
    saved = ""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"cursor_prompt_{ts}.md"
        dest.write_text(prompt, encoding="utf-8")
        saved = str(dest)
    except OSError:
        ctx.say("STOP — could not write the Cursor prompt file.")
        return 1
    ctx.say("Laptop coordinator — Cursor handoff prepared (NOT executed)")
    ctx.say("-" * 60)
    ctx.say(f"  cursor prompt saved : {saved}")
    ctx.say("  NEXT (manual):")
    ctx.say("    1. Open WEB Cursor (the user uses web Cursor, not local).")
    ctx.say("    2. Paste the saved prompt above into web Cursor.")
    ctx.say("    3. Web Cursor MUST push to GitHub `main` and report the commit hash.")
    ctx.say("    4. Then run: python scripts/laptop_agent_coordinator.py post-cursor-verify "
            f"--config {ctx.cfg.source_file or DEFAULT_CONFIG}")
    _append_ledger(ctx, {
        "timestamp": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "event": "prepare-cursor-handoff", "local_commit": _local_commit(ctx),
        "decision_source": str(src), "cursor_prompt_file": saved})
    return 0


# ---- sync-main ------------------------------------------------------------- #
def cmd_sync_main(ctx: Ctx) -> int:
    """Fast-forward-only pull of origin/main. Refuses a dirty working tree and never
    overwrites local files silently."""
    remote = ctx.cfg.git_remote if hasattr(ctx.cfg, "git_remote") else "origin"
    main_branch = "main"
    ctx.say("Laptop coordinator — sync-main (fast-forward only)")
    ctx.say("-" * 60)
    rc, _o, err = ctx.run(["git", "fetch", "origin"], timeout=120)
    if rc != 0:
        ctx.say(f"STOP — git fetch failed: {(err.strip().splitlines() or [''])[-1]}")
        return 1
    rc, out, _ = ctx.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
    branch = out.strip()
    if rc != 0 or branch != main_branch:
        ctx.say(f"STOP — current branch is '{branch or 'unknown'}', not '{main_branch}'. "
                f"Checkout {main_branch} first (never auto-switch).")
        return 2
    rc, dirty, _ = ctx.run(["git", "status", "--porcelain"], timeout=30)
    if rc != 0:
        ctx.say("STOP — could not read git status.")
        return 1
    if dirty.strip():
        ctx.say("STOP — local working tree has uncommitted changes. Refusing to pull "
                "(never overwrite local files silently). Commit/stash first:")
        ctx.say(dirty.rstrip())
        return 3
    before = _local_commit(ctx)
    rc, _o, err = ctx.run(["git", "pull", "--ff-only", "origin", main_branch], timeout=120)
    if rc != 0:
        ctx.say(f"STOP — fast-forward pull failed (diverged?): "
                f"{(err.strip().splitlines() or [''])[-1]}")
        return 1
    after = _local_commit(ctx)
    ctx.say(f"  before : {before or 'unknown'}")
    ctx.say(f"  after  : {after or 'unknown'}")
    ctx.say("  in sync with origin/main." if before == after
            else "  fast-forwarded to origin/main.")
    return 0


# ---- post-cursor-verify ---------------------------------------------------- #
def _resolve_plugin_tests(ctx: Ctx):
    """Find the PLUGIN's own tests/ dir (which carries pytest.ini as rootdir), regardless
    of how repo_root/plugin_path are configured. Order: configured plugin_path, the
    coordinator script's own plugin dir (always correct — this file lives in
    <plugin>/scripts/), then repo_root. Returns (plugin_dir, tests_dir) or (None, None)."""
    candidates = []
    if getattr(ctx.cfg, "plugin_path", ""):
        candidates.append(Path(ctx.cfg.plugin_path))
    candidates.append(Path(__file__).resolve().parents[1])   # <plugin>/scripts/.. = plugin
    candidates.append(ctx.repo_root)
    for d in candidates:
        try:
            if (d / "tests").is_dir():
                return d, (d / "tests")
        except OSError:
            continue
    return None, None


def cmd_post_cursor_verify(ctx: Ctx) -> int:
    """After web Cursor pushes to main: sync-main -> local tests -> doctor -> vps-smoke,
    then say whether it is safe to collect another light report."""
    ctx.say("=" * 64)
    ctx.say("POST-CURSOR VERIFY")
    ctx.say("=" * 64)
    ctx.say("\n[1/4] sync-main")
    if cmd_sync_main(ctx) != 0:
        ctx.say("\nSTOP — could not sync main; resolve the issue above first.")
        return 2
    ctx.say("\n[2/4] local tests")
    plugin_dir, tests_dir = _resolve_plugin_tests(ctx)
    if tests_dir is None:
        _check(ctx, "local tests pass", False,
               "no tests/ directory found (checked plugin_path, the coordinator's own "
               "plugin dir, and repo_root) — verification requires real tests")
        tests_ok = False
        out = err = ""
    else:
        # Run from the plugin dir so its pytest.ini (rootdir) is used, and target the
        # plugin's own tests/ explicitly — never the monorepo's tests.
        rc, out, err = ctx.run([sys.executable, "-m", "pytest", str(tests_dir), "-q"],
                               timeout=3600, cwd=plugin_dir)
        if rc == 5:    # pytest "no tests collected" — NEVER treated as success
            tests_ok = False
            _check(ctx, "local tests pass", False,
                   f"NO TESTS COLLECTED at {tests_dir} (exit 5) — fix discovery; "
                   "verification is not weakened to pass on zero tests")
        else:
            tests_ok = rc == 0
            _check(ctx, "local tests pass", tests_ok,
                   "" if tests_ok else (out.strip().splitlines()[-1:]
                                        or err.strip().splitlines()[-1:] or ["see output"])[-1])
    ctx.say("\n[3/4] doctor")
    doctor_ok = cmd_doctor(ctx) == 0
    ctx.say("\n[4/4] vps-smoke")
    smoke_ok = cmd_vps_smoke(ctx) == 0
    ok = tests_ok and doctor_ok and smoke_ok
    ctx.say("\n" + "-" * 64)
    if ok:
        ctx.say("SAFE TO COLLECT — run: python scripts/laptop_agent_coordinator.py "
                f"operator-cycle --config {ctx.cfg.source_file or DEFAULT_CONFIG}")
    else:
        ctx.say("NOT SAFE TO COLLECT yet — fix the failing checks above "
                f"(tests={tests_ok} doctor={doctor_ok} vps_smoke={smoke_ok}).")
    return 0 if ok else 1


# ---- start-paper-run ------------------------------------------------------- #
def build_remote_paper_run_cmd(cfg: Config) -> str:
    """The remote (PAPER ONLY) restart workflow using EXISTING compose commands."""
    plugin = cfg.vps_remote_plugin_path
    container = cfg.hermes_training_container
    return (f"cd {shlex.quote(plugin)} && docker compose down --remove-orphans && "
            f"docker compose up -d --build {shlex.quote(container)}")


def cmd_start_paper_run(ctx: Ctx, *, mode: str = "short",
                        approved_by_chatgpt: bool = False, dry_run: bool = False) -> int:
    """Start/restart PAPER training on the VPS (live trading stays disabled). Default is
    SHORT; a LONG run REQUIRES the explicit --approved-by-chatgpt flag."""
    cfg = ctx.cfg
    mode = (mode or "short").lower()
    if mode not in ("short", "long"):
        ctx.say(f"STOP — unknown mode '{mode}' (use short|long).")
        return 2
    if mode == "long" and not approved_by_chatgpt:
        ctx.say("STOP — a LONG paper run requires explicit ChatGPT approval. Re-run with "
                "--approved-by-chatgpt (only after ChatGPT approved a long run).")
        return 3
    if not ctx.config_found or cfg.missing_required():
        ctx.say("STOP — config missing required fields for a VPS run.")
        return 2
    key_ok, key_msg = validate_ssh_key(cfg)
    if not key_ok:
        ctx.say(f"STOP — {key_msg}")
        return 2

    remote = build_remote_paper_run_cmd(cfg)
    ssh_cmd = build_ssh_cmd(cfg, remote)
    ctx.say(f"Laptop coordinator — start PAPER run (mode={mode})")
    ctx.say("  PAPER TRAINING ONLY — live trading is DISABLED; no trade gates are changed.")
    ctx.say("  This restarts the hermes-training container via existing compose commands:")
    ctx.say(f"    {remote}")
    if dry_run:
        ctx.say("  [DRY-RUN] would run (secrets redacted):")
        ctx.say(f"    {redact(ssh_cmd, cfg)}")
        ctx.say("  (omit --dry-run to execute)")
        return 0
    rc, out, err = ctx.run(ssh_cmd, timeout=1800, redact_display=True)
    if out.strip():
        ctx.say(out.rstrip())
    _append_ledger(ctx, {
        "timestamp": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "event": "start-paper-run", "mode": mode,
        "approved_by_chatgpt": bool(approved_by_chatgpt),
        "local_commit": _local_commit(ctx), "rc": rc})
    if rc != 0:
        ctx.say(f"STOP — paper run start failed (rc={rc}).")
        if err.strip():
            ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
        return 1
    ctx.say(f"PAPER {mode.upper()} run started/restarted (paper training only).")
    return 0


# ---- status ---------------------------------------------------------------- #
def cmd_status(ctx: Ctx) -> int:
    """One-glance operator status: local branch/commit, config, latest zip, VPS SSH,
    Docker/hermes-training, remote Python deps, and the suggested next command."""
    cfg = ctx.cfg
    ctx.say("=" * 64)
    ctx.say("LAPTOP COORDINATOR — STATUS")
    ctx.say("=" * 64)
    rc, br, _ = ctx.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
    branch = br.strip() if rc == 0 else "unknown"
    commit = _local_commit(ctx)
    ctx.say(f"  local branch        : {branch}")
    ctx.say(f"  local commit        : {commit or 'unknown'}")
    ctx.say(f"  config parsed       : {'yes' if ctx.config_found else 'NO (' + ctx.config_status + ')'}")
    art = _artifact_dir(ctx)
    z = _latest_zip(art)
    ctx.say(f"  latest report zip   : {z if z else '(none)'}")

    vps_ssh = docker_state = remote_py = "not configured"
    if ctx.config_found and not cfg.missing_required() and validate_ssh_key(cfg)[0]:
        rc, out, _ = ctx.run(build_ssh_cmd(cfg, f"echo {VPS_OK_MARKER}"),
                             timeout=20, redact_display=True)
        vps_ssh = "reachable" if (rc == 0 and VPS_OK_MARKER in out) else "UNREACHABLE"
        rc, out, _ = ctx.run(
            build_ssh_cmd(cfg, f"docker inspect -f '{{{{.State.Status}}}}' "
                               f"{shlex.quote(cfg.hermes_training_container)} 2>/dev/null "
                               f"|| echo absent"), timeout=25, redact_display=True)
        docker_state = (out.strip().splitlines()[-1] if out.strip() else "unknown")
        rc, out, _ = ctx.run(build_remote_python_probe(cfg), timeout=25, redact_display=True)
        sel = ""
        for ln in out.splitlines():
            if ln.startswith("remote python: "):
                sel = ln.split("remote python: ", 1)[1].strip()
        remote_py = (f"OK ({sel}, can import {REMOTE_REQUIRED_IMPORT})" if sel
                     else f"NO dependency-capable python (run {REMOTE_DEP_FIX} on VPS)")
    ctx.say(f"  VPS SSH             : {vps_ssh}")
    ctx.say(f"  hermes-training     : {docker_state}")
    ctx.say(f"  remote Python deps  : {remote_py}")
    ctx.say("-" * 64)
    ctx.say(f"  NEXT SUGGESTED      : {_status_next_command(ctx, vps_ssh, z)}")
    return 0


def _status_next_command(ctx: Ctx, vps_ssh: str, latest_zip) -> str:
    cfg_name = ctx.cfg.source_file or DEFAULT_CONFIG
    base = "python scripts/laptop_agent_coordinator.py"
    if not ctx.config_found:
        return f"{base} init-config --config {DEFAULT_CONFIG}"
    if vps_ssh != "reachable":
        return f"{base} vps-smoke --config {cfg_name}   (VPS not reachable / configure it)"
    return f"{base} operator-cycle --config {cfg_name}"


# --------------------------------------------------------------------------- #
# Phase 6: Mission-Control Agent (one-command operator mission)
# --------------------------------------------------------------------------- #
MISSION_MODES = ("inspect-only", "proof2h", "long")
# Wait (seconds) the approved proof/long run accumulates before collecting the report.
# Operator-overridable with --proof-wait-seconds (0 = collect immediately, used by tests).
MODE_WAIT_SECONDS = {"inspect-only": 0, "proof2h": 7200, "long": 39600}

# The 100X paper-training runtime env the hermes-training container MUST carry. Live/
# real-money flags are checked separately (detect_live_flags) and must all be off.
REQUIRED_100X_ENV = {
    "AGGRESSIVE_PAPER_TRAINING": "1", "PAPER_PROFIT_DISCOVERY_PROFILE": "1",
    "HERMES_ACCELERATED_DISCOVERY": "1", "FEEDBACK_ACCELERATOR_ENABLED": "1",
    "FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER": "100",
    "POLYMARKET_ACTIVE_LEARNING_ENABLED": "1", "POLYMARKET_EXPLORATION_ENABLED": "1",
    "EXPLORATION_TINY_SIZE_ENABLED": "1", "NEWS_SCANNER_ENABLED": "1",
    "NEWS_PROVIDER_MODE": "live_read_only",
}


def build_remote_compose_config_cmd(cfg: Config) -> str:
    return f"cd {shlex.quote(cfg.vps_remote_plugin_path)} && docker compose config -q"


def build_remote_vps_sync_cmd(cfg: Config) -> str:
    return (f"cd {shlex.quote(cfg.vps_remote_plugin_path)} && git fetch origin && "
            f"git pull --ff-only origin main && git rev-parse HEAD")


def build_remote_container_env_cmd(cfg: Config) -> str:
    # works whether the container is running or merely created; never errors hard.
    return ("docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "
            f"{shlex.quote(cfg.hermes_training_container)} 2>/dev/null || true")


def build_remote_rebuild_cmd(cfg: Config) -> str:
    """Approved rebuild via EXISTING compose commands (PAPER ONLY; never live)."""
    return (f"cd {shlex.quote(cfg.vps_remote_plugin_path)} && "
            f"docker compose down --remove-orphans && docker compose build --no-cache && "
            f"docker compose up -d")


def _remote_compose_config_ok(ctx: Ctx) -> "tuple[bool, str]":
    rc, _o, err = ctx.run(build_ssh_cmd(ctx.cfg, build_remote_compose_config_cmd(ctx.cfg)),
                          timeout=120, redact_display=True)
    return (rc == 0), ("" if rc == 0 else (err.strip().splitlines() or ["compose config failed"])[-1])


def _remote_vps_git_pull(ctx: Ctx) -> "tuple[bool, str, str]":
    rc, out, err = ctx.run(build_ssh_cmd(ctx.cfg, build_remote_vps_sync_cmd(ctx.cfg)),
                           timeout=180, redact_display=True)
    return (rc == 0), _hex_commit(out), ("" if rc == 0 else
            (err.strip().splitlines() or ["vps git pull failed"])[-1])


def _remote_container_env(ctx: Ctx) -> dict:
    rc, out, _ = ctx.run(build_ssh_cmd(ctx.cfg, build_remote_container_env_cmd(ctx.cfg)),
                         timeout=30, redact_display=True)
    env: dict = {}
    if rc == 0:
        for ln in out.splitlines():
            if "=" in ln:
                k, _, v = ln.partition("=")
                env[k.strip()] = v.strip()
    return env


def _remote_100x_proof(ctx: Ctx) -> "tuple[bool, dict, list]":
    """Verify the hermes-training container env carries the 100X paper profile. Returns
    (ok, present_subset, mismatched[]). Never exposes secrets (only the 100X keys)."""
    env = _remote_container_env(ctx)
    present = {k: env.get(k) for k in REQUIRED_100X_ENV}
    mismatched = [f"{k}={env.get(k)!r}(want {want})"
                  for k, want in REQUIRED_100X_ENV.items() if env.get(k) != want]
    return (not mismatched and bool(env)), present, mismatched


def _mission_dirty_artifacts(ctx: Ctx) -> "tuple[list, str]":
    """Return (dirty_generated_paths, safe_cleanup_command_text). Read-only: it NEVER
    deletes anything — it only reports generated artifacts dirtying git status + offers
    the exact safe cleanup command for the operator to run themselves."""
    rc, out, _ = ctx.run(["git", "status", "--porcelain"], timeout=30)
    if rc != 0:
        return [], ""
    markers = ("validation_light_latest.txt", "vps_light_report", "hermes_light_report",
               "report_logs/", ".report_venv", "git_commit_proof.txt",
               "runtime_data_light_", "inspection_reports_light_")
    dirty = []
    for ln in out.splitlines():
        path = ln[3:].strip() if len(ln) > 3 else ln.strip()
        if any(m in path for m in markers):
            dirty.append(path)
    cleanup = ("git rm --cached -r --ignore-unmatch " + " ".join(dirty)) if dirty else ""
    return dirty, cleanup


def _remote_rebuild(ctx: Ctx, *, dry_run: bool) -> int:
    cfg = ctx.cfg
    remote = build_remote_rebuild_cmd(cfg)
    ssh_cmd = build_ssh_cmd(cfg, remote)
    ctx.say("  approved rebuild (PAPER ONLY; existing compose commands):")
    ctx.say(f"    {remote}")
    if dry_run:
        ctx.say(f"  [DRY-RUN] would run: {redact(ssh_cmd, cfg)}")
        return 0
    rc, out, err = ctx.run(ssh_cmd, timeout=3600, redact_display=True)
    if out.strip():
        ctx.say(out.rstrip())
    if rc != 0 and err.strip():
        ctx.say(f"  detail: {err.strip().splitlines()[-1]}")
    return rc


def _print_mission_summary(ctx: Ctx, *, mode: str, safe: bool, local_commit: str,
                           vps_commit: str, compose_config_ok, paper_running: bool,
                           container_status: str, env100_ok, zip_path, zip_complete,
                           instr: str, cursor_needed: bool, cursor_file: str,
                           dirty_artifacts: list, cleanup_cmd: str) -> None:
    ctx.say("\n" + "=" * 64)
    ctx.say(f"MISSION-CONTROL — FINAL STATUS (mode={mode})")
    ctx.say("=" * 64)
    ctx.say(f"  RESULT              : {'SAFE TO CONTINUE' if safe else 'STOP'}")
    ctx.say(f"  local commit        : {local_commit or 'unknown'}")
    ctx.say(f"  VPS commit          : {vps_commit or 'unknown'}")
    ctx.say(f"  docker compose config: {'OK' if compose_config_ok else 'FAILED' if compose_config_ok is False else 'not checked'}")
    ctx.say(f"  hermes-training     : {'RUNNING' if paper_running else 'NOT running'} "
            f"({container_status})")
    ctx.say(f"  100X env proof      : {'OK' if env100_ok else 'NOT proven' if env100_ok is False else 'not checked'}")
    ctx.say(f"  report zip (local)  : {zip_path if zip_path else '(none)'}")
    ctx.say(f"  report bundle       : {'COMPLETE' if zip_complete else 'THIN/incomplete' if zip_complete is False else 'n/a'}")
    ctx.say(f"  Cursor needed       : {'YES' if cursor_needed else 'no'}"
            + (f" -> {cursor_file}" if cursor_needed and cursor_file else ""))
    if dirty_artifacts:
        ctx.say(f"  WARNING: generated artifacts dirty git status: {dirty_artifacts}")
        ctx.say(f"    safe cleanup (run yourself; never auto-run): {cleanup_cmd}")
    ctx.say("-" * 64)
    if zip_path is not None and zip_complete:
        ctx.say(f"  UPLOAD TO CHATGPT   : {zip_path}")
        ctx.say(f"  upload instructions : {instr or '(could not write)'}")
        ctx.say("  -> Upload that zip to ChatGPT and ask it to inspect the light report.")
    else:
        ctx.say("  UPLOAD TO CHATGPT   : (no complete report bundle — see blockers above)")
    ctx.say("  (No ChatGPT free text is ever executed; Cursor is never auto-run.)")


def cmd_mission_control(ctx: Ctx, *, mode: str = "inspect-only",
                        approved_paper_run: bool = False, approved_by_chatgpt: bool = False,
                        dry_run: bool = False, proof_wait_seconds=None) -> int:
    """Phase 6 ONE-COMMAND mission for the non-coder operator. inspect-only (default)
    runs read-only checks + canonical report collection and NEVER starts a run. proof2h
    requires --approved-paper-run; long requires --approved-paper-run AND
    --approved-by-chatgpt. Live trading is never enabled, no gate is loosened, no ChatGPT
    free text is executed, and Cursor is never auto-run (a handoff FILE is written only on
    a blocker)."""
    import os as _os
    cfg = ctx.cfg
    mode = (mode or "inspect-only").lower()
    blockers: list = []
    cursor_needed = False
    cursor_file = ""
    state = {"compose_config_ok": None, "env100_ok": None, "vps_commit": "",
             "paper_running": False, "container_status": "unknown",
             "zip": None, "zip_complete": None, "instr": ""}

    ctx.say("=" * 64)
    ctx.say(f"MISSION-CONTROL — {mode} (PAPER ONLY; live trading disabled)")
    ctx.say("=" * 64)

    def _finish(rc_safe: bool, rc: int) -> int:
        nonlocal cursor_file
        if (blockers or not rc_safe) and not cursor_file:
            cursor_file = _write_cycle_blocker_handoff(
                ctx, blockers or [f"mission-control stopped (mode={mode})"])
        dirty, cleanup = _mission_dirty_artifacts(ctx)
        _append_ledger(ctx, {
            "timestamp": _dt.datetime.now().replace(microsecond=0).isoformat(),
            "event": "mission-control", "mode": mode, "local_commit": _local_commit(ctx),
            "vps_commit": state["vps_commit"], "compose_config_ok": state["compose_config_ok"],
            "paper_running": state["paper_running"], "env100_ok": state["env100_ok"],
            "report_zip": (str(state["zip"]) if state["zip"] else ""),
            "zip_complete": state["zip_complete"], "approved_paper_run": bool(approved_paper_run),
            "approved_by_chatgpt": bool(approved_by_chatgpt), "blockers": blockers,
            "cursor_needed": bool(cursor_file)})
        _print_mission_summary(
            ctx, mode=mode, safe=rc_safe, local_commit=_local_commit(ctx),
            vps_commit=state["vps_commit"], compose_config_ok=state["compose_config_ok"],
            paper_running=state["paper_running"], container_status=state["container_status"],
            env100_ok=state["env100_ok"], zip_path=state["zip"],
            zip_complete=state["zip_complete"], instr=state["instr"],
            cursor_needed=bool(cursor_file), cursor_file=cursor_file,
            dirty_artifacts=dirty, cleanup_cmd=cleanup)
        return rc

    # --- mode + approval gating (NO run starts without explicit approval) ---
    if mode not in MISSION_MODES:
        ctx.say(f"STOP — unknown mode '{mode}' (use {', '.join(MISSION_MODES)}).")
        return _finish(False, 2)
    if mode in ("proof2h", "long") and not approved_paper_run:
        blockers.append(f"{mode} requires explicit --approved-paper-run")
        ctx.say(f"STOP — mode '{mode}' starts/rebuilds a paper run and REQUIRES "
                "--approved-paper-run. Refusing (inspect-only is the safe default).")
        return _finish(False, 3)
    if mode == "long" and not approved_by_chatgpt:
        blockers.append("long requires --approved-by-chatgpt")
        ctx.say("STOP — mode 'long' additionally REQUIRES --approved-by-chatgpt "
                "(only after ChatGPT approved a long run).")
        return _finish(False, 3)

    # --- live/real-money flags must all be off (fail closed) ---
    live = detect_live_flags(_os.environ)
    if live:
        blockers.append(f"live/real-money flags enabled: {', '.join(live)}")
        ctx.say(f"STOP — live/real-money flag(s) detected: {', '.join(live)}. PAPER ONLY.")
        return _finish(False, 2)

    # --- read-only verification phase (always) ---
    ctx.say("\n[1] local doctor")
    if cmd_doctor(ctx) != 0:
        blockers.append("local environment/config check failed (doctor)")
        return _finish(False, 2)
    ctx.say("\n[2] sync local GitHub main (fast-forward only)")
    if cmd_sync_main(ctx) != 0:
        blockers.append("could not safely sync local GitHub main")
        return _finish(False, 2)
    ctx.say("\n[3] VPS SSH smoke check")
    if cmd_vps_smoke(ctx) != 0:
        blockers.append("VPS access / Docker check failed (vps-smoke)")
        return _finish(False, 2)
    ctx.say("\n[4] VPS git fetch/pull main")
    sync_ok, state["vps_commit"], sync_detail = _remote_vps_git_pull(ctx)
    ctx.say(f"  VPS commit          : {state['vps_commit'] or 'unknown'}"
            + ("" if sync_ok else f"  (pull issue: {sync_detail})"))
    if not sync_ok:
        blockers.append(f"VPS git pull main failed: {sync_detail}")
    ctx.say("\n[5] docker compose config -q on VPS")
    state["compose_config_ok"], cc_detail = _remote_compose_config_ok(ctx)
    ctx.say(f"  compose config      : {'OK' if state['compose_config_ok'] else 'FAILED: ' + cc_detail}")
    if not state["compose_config_ok"]:
        blockers.append(f"docker compose config -q failed on VPS: {cc_detail}")
        cursor_needed = True
        return _finish(False, 2)
    ctx.say("\n[6] hermes-training status + 100X env proof")
    state["paper_running"], state["container_status"] = _remote_paper_status(ctx)
    ctx.say(f"  hermes-training     : {'RUNNING' if state['paper_running'] else 'NOT running'} "
            f"({state['container_status']})")
    if state["paper_running"]:
        state["env100_ok"], _present, _missing = _remote_100x_proof(ctx)
        ctx.say(f"  100X env proof      : {'OK' if state['env100_ok'] else 'NOT proven: ' + str(_missing)}")

    # --- APPROVED rebuild (proof2h / long only) ---
    if approved_paper_run and mode in ("proof2h", "long"):
        ctx.say(f"\n[7] APPROVED rebuild + proof run (mode={mode})")
        if _remote_rebuild(ctx, dry_run=dry_run) != 0:
            blockers.append("approved rebuild failed (docker compose down/build/up)")
            cursor_needed = True
            return _finish(False, 1)
        if not dry_run:
            state["env100_ok"], _present, _missing = _remote_100x_proof(ctx)
            ctx.say(f"  100X runtime env    : {'OK' if state['env100_ok'] else 'NOT proven: ' + str(_missing)}")
            if not state["env100_ok"]:
                blockers.append(f"100X runtime env not proven after rebuild: {_missing}")
                cursor_needed = True
                return _finish(False, 1)
            state["paper_running"], state["container_status"] = _remote_paper_status(ctx)
            wait = (int(proof_wait_seconds) if proof_wait_seconds is not None
                    else MODE_WAIT_SECONDS.get(mode, 0))
            if wait > 0:
                ctx.say(f"  waiting {wait}s for the proof run to accumulate before collecting…")
                time.sleep(wait)

    # --- collect the canonical light report (both modes) ---
    ctx.say("\n[8] collect canonical light report + copy zip locally")
    collect_rc = cmd_collect_light_report(ctx, dry_run=dry_run)
    if dry_run:
        ctx.say("\n[DRY-RUN] mission preview complete (nothing rebuilt/collected/started).")
        return _finish(not blockers, 0 if not blockers else 1)
    if collect_rc != 0:
        blockers.append("canonical light-report collection failed / thin zip refused")
        cursor_needed = True

    art = _artifact_dir(ctx)
    z = _latest_zip(art)
    state["zip"] = z
    if z is not None:
        try:
            with zipfile.ZipFile(z) as zf:
                complete, _missing = zip_completeness(zf.namelist())
            state["zip_complete"] = complete
        except (OSError, zipfile.BadZipFile):
            state["zip_complete"] = False
    else:
        state["zip_complete"] = False
    if not state["zip_complete"]:
        blockers.append("report zip incomplete (missing required bundle markers)")
        cursor_needed = True

    ctx.say("\n[9] write ChatGPT upload handoff")
    state["instr"] = _write_upload_instructions(ctx, z)

    if cursor_needed and not cursor_file:
        cursor_file = _write_cycle_blocker_handoff(ctx, blockers or ["mission blocker"])
    return _finish(not blockers, 0 if not blockers else 1)


# --------------------------------------------------------------------------- #
# Parser & main
# --------------------------------------------------------------------------- #
COMMANDS = {
    "init-config": cmd_init_config,
    "doctor": cmd_doctor,
    "vps-smoke": cmd_vps_smoke,
    "collect-light-report": cmd_collect_light_report,
    "collect-report": cmd_collect_light_report,     # friendly alias (same behavior)
    "handoff-summary": cmd_handoff_summary,
    "operator-cycle": cmd_operator_cycle,
    "mission-control": cmd_mission_control,
    "record-chatgpt-decision": cmd_record_chatgpt_decision,
    "prepare-cursor-handoff": cmd_prepare_cursor_handoff,
    "sync-main": cmd_sync_main,
    "post-cursor-verify": cmd_post_cursor_verify,
    "start-paper-run": cmd_start_paper_run,
    "status": cmd_status,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="laptop_agent_coordinator",
        description="Laptop Hermes Agent coordinator (Phase 2): operator handoff "
                    "workflow. Coordinator tooling only — never changes trading "
                    "strategy, gates, or live-trading behavior; never prints secrets.")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    helps = {
        "init-config": "write a clean, BOM-free .laptop_agent.json (no secrets)",
        "doctor": "check the local environment (repo/plugin/git/ssh/python/artifacts)",
        "vps-smoke": "read-only VPS checks over SSH (reachable/path/docker/container)",
        "collect-light-report": "collect a light-mode report zip from the VPS",
        "collect-report": "alias of collect-light-report",
        "handoff-summary": "print the ChatGPT upload checklist for the latest zip",
        "operator-cycle": "ONE-COMMAND safe workflow: verify+sync+VPS+collect+handoff "
                          "(+ optional approved paper run)",
        "mission-control": "Phase 6 mission: inspect-only (default, never starts a run) or "
                           "approved proof2h/long rebuild + 100X proof + canonical report",
        "record-chatgpt-decision": "classify ChatGPT's saved response (no auto-execute)",
        "prepare-cursor-handoff": "extract+save a Cursor prompt from ChatGPT's response",
        "sync-main": "fast-forward-only pull of origin/main (refuses dirty tree)",
        "post-cursor-verify": "sync-main + tests + doctor + vps-smoke after a Cursor push",
        "start-paper-run": "start/restart PAPER training on the VPS (long needs approval)",
        "status": "one-glance operator status + suggested next command",
    }
    for name in COMMANDS:
        sp = sub.add_parser(name, help=helps.get(name, ""))
        sp.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"path to coordinator config (default: {DEFAULT_CONFIG})")
        if name in ("collect-light-report", "collect-report", "operator-cycle",
                    "mission-control", "start-paper-run"):
            sp.add_argument("--dry-run", action="store_true",
                            help="print the exact remote/scp commands without running them")
        if name == "operator-cycle":
            sp.add_argument("--approved-paper-run", action="store_true",
                            help="explicitly approve starting a PAPER run at the end of the "
                                 "cycle (no run starts without this flag; live stays OFF)")
            sp.add_argument("--mode", choices=["short", "long"], default="short",
                            help="approved paper run length: short=2h proof, long=approved "
                                 "longer paper run (default: short)")
        if name == "mission-control":
            sp.add_argument("--mode", choices=list(MISSION_MODES), default="inspect-only",
                            help="inspect-only (default; never starts a run) | proof2h "
                                 "(needs --approved-paper-run) | long (needs both approvals)")
            sp.add_argument("--approved-paper-run", action="store_true",
                            help="REQUIRED to rebuild/start a paper run (proof2h/long)")
            sp.add_argument("--approved-by-chatgpt", action="store_true",
                            help="additionally REQUIRED for --mode long")
            sp.add_argument("--proof-wait-seconds", type=int, default=None,
                            help="override the proof-run wait before collecting (default: "
                                 "per-mode; 0 collects immediately)")
        if name == "init-config":
            sp.add_argument("--force", action="store_true",
                            help="overwrite an existing config file")
        if name in ("record-chatgpt-decision", "prepare-cursor-handoff"):
            sp.add_argument("--file", required=True,
                            help="path to the saved ChatGPT response (.md)")
        if name == "start-paper-run":
            sp.add_argument("--mode", choices=["short", "long"], default="short",
                            help="paper run length (default: short)")
            sp.add_argument("--approved-by-chatgpt", action="store_true",
                            help="REQUIRED for --mode long (explicit human/ChatGPT approval)")
    return p


def main(argv=None, *, runner: Optional[Runner] = None,
         printer: Optional[Callable[[str], None]] = None,
         now_fn: Optional[Callable[[], _dt.datetime]] = None,
         which_fn: Optional[Callable[[str], Optional[str]]] = None,
         repo_root: Optional[Path] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    printer = printer or print
    runner = runner or default_runner
    now_fn = now_fn or _dt.datetime.now
    which_fn = which_fn or shutil.which
    if not args.command:
        parser.print_help()
        return 0

    cfg_path = Path(args.config)
    load = load_config(cfg_path)
    cfg = load.cfg
    root = repo_root or (Path(cfg.repo_root) if cfg.repo_root else Path.cwd())
    ctx = Ctx(cfg=cfg, config_found=load.found, repo_root=root, runner=runner,
              printer=printer, now_fn=now_fn, which_fn=which_fn,
              config_status=load.status, config_detail=load.detail, config_path=load.path)

    handler = COMMANDS[args.command]
    if args.command == "init-config":
        return handler(ctx, target=cfg_path, force=bool(getattr(args, "force", False)))
    if args.command == "operator-cycle":
        return handler(ctx, dry_run=bool(getattr(args, "dry_run", False)),
                       approved_paper_run=bool(getattr(args, "approved_paper_run", False)),
                       mode=getattr(args, "mode", "short"))
    if args.command == "mission-control":
        return handler(ctx, mode=getattr(args, "mode", "inspect-only"),
                       approved_paper_run=bool(getattr(args, "approved_paper_run", False)),
                       approved_by_chatgpt=bool(getattr(args, "approved_by_chatgpt", False)),
                       dry_run=bool(getattr(args, "dry_run", False)),
                       proof_wait_seconds=getattr(args, "proof_wait_seconds", None))
    if args.command in ("collect-light-report", "collect-report"):
        return handler(ctx, dry_run=bool(getattr(args, "dry_run", False)))
    if args.command in ("record-chatgpt-decision", "prepare-cursor-handoff"):
        return handler(ctx, file=args.file)
    if args.command == "start-paper-run":
        return handler(ctx, mode=getattr(args, "mode", "short"),
                       approved_by_chatgpt=bool(getattr(args, "approved_by_chatgpt", False)),
                       dry_run=bool(getattr(args, "dry_run", False)))
    return handler(ctx)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
