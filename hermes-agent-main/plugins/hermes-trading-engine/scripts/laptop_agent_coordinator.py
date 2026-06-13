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
import shlex
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_CONFIG = ".laptop_agent.json"
EXAMPLE_CONFIG = ".laptop_agent.example.json"
DEFAULT_CONTAINER = "hermes-training"
VPS_OK_MARKER = "hermes-coordinator-ok"

# Remote Python detection: a normal Ubuntu VPS often has only ``python3`` (no bare
# ``python``). This shell snippet, run over SSH, prefers python3, falls back to python,
# and STOPS early (rc 127) with a clear message if neither exists. Subsequent steps use
# the resolved ``"$PYBIN"`` so the remote workflow is operator-proof.
REMOTE_PY_DETECT = (
    'PYBIN="$(command -v python3 || command -v python || true)"; '
    'if [ -z "$PYBIN" ]; then '
    'echo "FATAL: no python3 or python on the VPS PATH (install python3)" 1>&2; '
    'exit 127; fi; '
    'echo "remote python: $PYBIN"'
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


def remote_zip_name(now: _dt.datetime) -> str:
    return f"hermes_light_report_{now.strftime('%Y%m%d_%H%M%S')}.zip"


def build_remote_collect_script(cfg: Config, remote_zip: str) -> str:
    """The exact remote shell workflow run over SSH for collect-light-report.

    cd plugin -> drop stale runtime_data -> docker cp container:/data runtime_data ->
    light inspection report -> runtime validation (tee'd) -> zip the 3 artifacts."""
    plugin = cfg.vps_remote_plugin_path
    container = cfg.hermes_training_container
    # NOTE: the report/validation steps run through the DETECTED "$PYBIN" (python3 ->
    # python), never a bare `python`, so a python3-only VPS works.
    return (
        f"set -e; cd {shlex.quote(plugin)}; "
        f"{REMOTE_PY_DETECT}; "
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
    """SSH probe that prints the remote Python path (python3 preferred), or NO_PYTHON."""
    return build_ssh_cmd(cfg, "command -v python3 || command -v python || echo NO_PYTHON")


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

    def run(self, argv, *, timeout: int = 120, redact_display: bool = False):
        shown = redact(argv, self.cfg) if redact_display else " ".join(
            shlex.quote(str(t)) for t in argv)
        self.say(f"  $ {shown}")
        return self.runner(list(argv), cwd=str(self.repo_root), timeout=timeout)


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

    rc, out, _ = ctx.run(
        build_ssh_cmd(cfg, f"test -d {shlex.quote(cfg.vps_remote_plugin_path)} "
                           f"&& echo {VPS_OK_MARKER}"),
        timeout=20, redact_display=True)
    results.append(_check(ctx, "remote plugin path exists", rc == 0 and VPS_OK_MARKER in out))

    rc, out, _ = ctx.run(build_ssh_cmd(cfg, "docker version --format '{{.Server.Version}}' "
                                            "|| docker --version"),
                         timeout=25, redact_display=True)
    results.append(_check(ctx, "Docker available on VPS", rc == 0 and bool(out.strip()),
                          out.strip().splitlines()[0] if out.strip() else ""))

    # remote Python preflight: collection runs python3 (or python) on the VPS, never a
    # bare `python` — report exactly which executable will be used.
    rc, out, _ = ctx.run(build_remote_python_probe(cfg), timeout=20, redact_display=True)
    remote_py = (out.strip().splitlines()[-1] if out.strip() else "")
    py_ok = rc == 0 and bool(remote_py) and remote_py != "NO_PYTHON"
    results.append(_check(ctx, "remote Python available", py_ok,
                          f"will use {remote_py}" if py_ok
                          else "no python3/python on VPS PATH (install python3)"))

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

    now = ctx.now_fn()
    zip_name = remote_zip_name(now)
    remote_zip = f"{cfg.vps_remote_plugin_path.rstrip('/')}/{zip_name}"
    remote_script = build_remote_collect_script(cfg, remote_zip)
    ssh_cmd = build_ssh_cmd(cfg, remote_script)
    art = (ctx.repo_root / cfg.local_artifact_dir) \
        if not Path(cfg.local_artifact_dir).is_absolute() else Path(cfg.local_artifact_dir)
    scp_cmd = build_scp_pull_cmd(cfg, remote_zip, str(art))

    ctx.say("Laptop coordinator — collect light-mode report from the VPS")
    ctx.say("  NOTE: this RUNS on the VPS: replaces remote runtime_data, regenerates the")
    ctx.say("  light report, validates, zips, then copies the zip back to this laptop.")
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
    ctx.say("-" * 60)
    ctx.say(f"COLLECTED: {local_zip}")
    ctx.say("NEXT: python scripts/laptop_agent_coordinator.py handoff-summary "
            f"--config {cfg.source_file or DEFAULT_CONFIG}")
    return 0


# --------------------------------------------------------------------------- #
# handoff-summary
# --------------------------------------------------------------------------- #
def _latest_zip(art: Path) -> Optional[Path]:
    if not art.is_dir():
        return None
    zips = [p for p in art.glob("hermes_light_report_*.zip") if p.is_file()]
    if not zips:
        zips = [p for p in art.glob("*.zip") if p.is_file()]
    return max(zips, key=lambda p: p.stat().st_mtime) if zips else None


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
    has_validation = any(n.endswith(VALIDATION_FILE) for n in names)
    has_summary = any(n.endswith("inspection_summary.json") for n in names)
    ts = _dt.datetime.fromtimestamp(z.stat().st_mtime).replace(microsecond=0).isoformat()
    ctx.say(f"  report zip            : {z}")
    ctx.say(f"  generated (mtime)     : {ts}")
    ctx.say(f"  validation file       : {'included' if has_validation else 'MISSING'} "
            f"({VALIDATION_FILE})")
    ctx.say(f"  inspection_summary    : {'included' if has_summary else 'MISSING'} "
            f"(runtime_data/inspection_summary.json)")
    ctx.say(f"  total files in zip    : {len(names)}")
    ctx.say("=" * 60)
    ctx.say("NEXT: upload the zip above to ChatGPT for inspection.")
    return 0 if (has_validation and has_summary) else 1


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
# Parser & main
# --------------------------------------------------------------------------- #
COMMANDS = {
    "init-config": cmd_init_config,
    "doctor": cmd_doctor,
    "vps-smoke": cmd_vps_smoke,
    "collect-light-report": cmd_collect_light_report,
    "handoff-summary": cmd_handoff_summary,
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
        "handoff-summary": "print the ChatGPT upload checklist for the latest zip",
    }
    for name in COMMANDS:
        sp = sub.add_parser(name, help=helps.get(name, ""))
        sp.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"path to coordinator config (default: {DEFAULT_CONFIG})")
        if name == "collect-light-report":
            sp.add_argument("--dry-run", action="store_true",
                            help="print the exact remote/scp commands without running them")
        if name == "init-config":
            sp.add_argument("--force", action="store_true",
                            help="overwrite an existing config file")
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
    if args.command == "collect-light-report":
        return handler(ctx, dry_run=bool(getattr(args, "dry_run", False)))
    return handler(ctx)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
