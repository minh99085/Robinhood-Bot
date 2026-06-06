"""Data collectors for the bot inspection report.

Inspection/reporting ONLY — read-only commands, read-only HTTP GETs, file copies.
Nothing here changes trading behavior, flags, wallets, or submits orders.

Every collector captures stdout/stderr/exit-code/error and NEVER raises on a
failed command — a missing Docker, unreachable API, or absent folder is recorded
in the result, not fatal. Runners/openers are injectable so unit tests need no
Docker, no network, and no real subprocesses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Runner: (cmd_list, cwd, timeout) -> (exit_code, stdout, stderr)
Runner = Callable[[list, Optional[str], Optional[float]], tuple]
# Opener: (url, timeout) -> (status_code, body_text)
Opener = Callable[[str, float], tuple]


def default_runner(cmd: list, cwd: Optional[str] = None,
                   timeout: Optional[float] = 120.0) -> tuple:
    """Run a command, returning (exit_code, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", f"command not found: {exc}"
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or ""), f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — collectors must never crash the report
        return 1, "", f"{type(exc).__name__}: {exc}"


def default_opener(url: str, timeout: float = 5.0) -> tuple:
    """HTTP GET returning (status_code, body_text). Never raises."""
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-inspection/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback only)
            body = resp.read().decode("utf-8", errors="replace")
            return getattr(resp, "status", 200), body
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def run_cmd(cmd: list, cwd: Optional[str] = None, timeout: Optional[float] = 120.0,
            runner: Optional[Runner] = None) -> dict:
    """Run one command, returning a structured record (never raises)."""
    runner = runner or default_runner
    code, out, err = runner(cmd, cwd, timeout)
    return {
        "cmd": " ".join(cmd),
        "exit_code": code,
        "ok": code == 0,
        "stdout": out or "",
        "stderr": err or "",
        "error": (err or "").strip() if code != 0 else "",
    }


# ----------------------------------------------------------------------------- #
# Git
# ----------------------------------------------------------------------------- #
def collect_git(repo_root: str, runner: Optional[Runner] = None) -> dict:
    """Collect read-only git context."""
    def g(args):
        return run_cmd(["git", *args], cwd=repo_root, timeout=30, runner=runner)

    return {
        "status": g(["status", "--porcelain=v1", "-b"]),
        "branch": g(["rev-parse", "--abbrev-ref", "HEAD"]),
        "log_recent": g(["log", "-n", "20", "--pretty=format:%h %ad %an %s", "--date=short"]),
        "diff_stat": g(["diff", "--stat", "HEAD~5..HEAD"]),
        "changed_files": g(["diff", "--name-only", "HEAD~5..HEAD"]),
    }


# ----------------------------------------------------------------------------- #
# Docker
# ----------------------------------------------------------------------------- #
def collect_docker(repo_root: str, runner: Optional[Runner] = None,
                   tail_training: int = 1000, tail_engine: int = 500) -> dict:
    """Collect read-only docker compose status + logs + training status."""
    def dc(args, timeout=120):
        return run_cmd(["docker", "compose", *args], cwd=repo_root, timeout=timeout, runner=runner)

    available = run_cmd(["docker", "version", "--format", "{{.Server.Version}}"],
                        cwd=repo_root, timeout=20, runner=runner)
    res = {
        "available": available["ok"],
        "version": available,
        "ps": dc(["ps"]),
        "config": dc(["config"]),
        "images": run_cmd(["docker", "images"], cwd=repo_root, timeout=30, runner=runner),
        "volumes": run_cmd(["docker", "volume", "ls"], cwd=repo_root, timeout=30, runner=runner),
        "training_status": dc(["exec", "-T", "hermes-training", "python",
                               "scripts/polymarket_training_status.py"]),
        "training_status_json": dc(["exec", "-T", "hermes-training", "python",
                                    "scripts/polymarket_training_status.py", "--json"]),
        "logs_training": dc(["logs", "hermes-training", "--tail", str(tail_training)], timeout=120),
        "logs_engine": dc(["logs", "hermes-trading-engine", "--tail", str(tail_engine)], timeout=120),
    }
    return res


# ----------------------------------------------------------------------------- #
# API snapshots
# ----------------------------------------------------------------------------- #
API_ENDPOINTS = {
    "health": "/api/health",
    "state": "/api/state",
    "venues_status": "/api/venues/status",
    "chainlink_status": "/api/chainlink/status",
    "news_status": "/api/news/status",
    "research_status": "/api/research/status",
    "micro_live_status": "/api/micro-live/status",
    "guarded_live_status": "/api/guarded-live/status",
    "production_review_status": "/api/production-review/status",
}


def collect_api(base_url: str = "http://localhost:8800",
                opener: Optional[Opener] = None, timeout: float = 5.0) -> dict:
    """Snapshot read-only API endpoints. Each entry records ok/status/json/error."""
    opener = opener or default_opener
    out: dict[str, Any] = {}
    for name, path in API_ENDPOINTS.items():
        url = base_url.rstrip("/") + path
        status, body = opener(url, timeout)
        entry: dict[str, Any] = {"endpoint": path, "url": url, "status": status}
        if status and 200 <= status < 300:
            try:
                entry["json"] = json.loads(body)
                entry["ok"] = True
            except (json.JSONDecodeError, TypeError):
                entry["ok"] = False
                entry["error"] = "non-JSON response"
                entry["raw"] = (body or "")[:2000]
        else:
            entry["ok"] = False
            entry["error"] = f"http_status={status}" if status else (body or "unreachable")
        out[name] = entry
    return out


def api_json_map(api: dict) -> dict:
    """Reduce a collect_api() result to ``{name: json_or_{}}`` for feature use."""
    out = {}
    for name, entry in (api or {}).items():
        if isinstance(entry, dict) and entry.get("ok") and isinstance(entry.get("json"), dict):
            out[name] = entry["json"]
        else:
            out[name] = {}
    return out


# ----------------------------------------------------------------------------- #
# Tests
# ----------------------------------------------------------------------------- #
TEST_SELECTORS = {
    "full": [],
    "chainlink": ["tests", "-k", "chainlink"],
    "btc_pulse": ["tests", "-k", "btc_pulse"],
    "fast_price": ["tests", "-k", "fast_price"],
    "news": ["tests", "-k", "news"],
    "bregman": ["tests", "-k", "bregman"],
    "paper_attribution": ["tests", "-k", "paper_attribution"],
    "inspection": ["tests", "-k", "inspection"],
}


def _parse_pytest_summary(stdout: str, stderr: str) -> dict:
    """Pull pass/fail counts from a pytest run's output (best-effort)."""
    import re
    text = (stdout or "") + "\n" + (stderr or "")
    summary = {"passed": None, "failed": None, "errors": None, "skipped": None,
               "deselected": None, "collected": None}
    for key, pat in (
        ("passed", r"(\d+)\s+passed"),
        ("failed", r"(\d+)\s+failed"),
        ("errors", r"(\d+)\s+error"),
        ("skipped", r"(\d+)\s+skipped"),
        ("deselected", r"(\d+)\s+deselected"),
        ("collected", r"(\d+)\s+(?:tests? )?collected"),
    ):
        m = re.search(pat, text)
        if m:
            summary[key] = int(m.group(1))
    return summary


def pytest_base_cmd() -> list:
    """Build a platform-safe ``python -m pytest`` base command.

    - Uses ``sys.executable`` (correct interpreter on Windows/venvs).
    - Disables the cache plugin (read-only inspection; avoids .pytest_cache).
    - If ``pytest-timeout`` is installed, forces ``--timeout-method=thread``.
      The default/`signal` method uses ``SIGALRM``, which does NOT exist on
      Windows and crashes pytest. The thread method is cross-platform. This CLI
      flag also overrides any inherited ``--timeout-method=signal`` from a parent
      pyproject's ``addopts`` (the cause of the Windows SIGALRM failure).
    """
    import importlib.util
    base = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
    if importlib.util.find_spec("pytest_timeout") is not None:
        base += ["--timeout-method=thread"]
    return base


def collect_tests(repo_root: str, runner: Optional[Runner] = None,
                  selectors: Optional[dict] = None, skip: bool = False,
                  timeout: float = 600.0) -> dict:
    """Run pytest selectors locally. Captures output without crashing the report.

    Platform-safe: see ``pytest_base_cmd`` (avoids the Windows SIGALRM crash)."""
    selectors = selectors if selectors is not None else TEST_SELECTORS
    if skip:
        return {"skipped": True, "present": None, "passing": None, "runs": {}}

    base = pytest_base_cmd()
    runs: dict[str, dict] = {}
    for name, extra in selectors.items():
        cmd = [*base, *extra, "-q"]
        rec = run_cmd(cmd, cwd=repo_root, timeout=timeout, runner=runner)
        rec["summary"] = _parse_pytest_summary(rec["stdout"], rec["stderr"])
        runs[name] = rec

    full = runs.get("full", {})
    full_summary = full.get("summary", {}) if full else {}
    # "present" = pytest itself ran and collected something (not exit 127/no-tests).
    no_pytest = full.get("exit_code") == 127 or "No module named pytest" in (
        full.get("stderr", "") + full.get("stdout", ""))
    collected = full_summary.get("collected")
    present = None
    if full:
        present = (not no_pytest) and (collected is None or collected > 0) and full.get("exit_code") != 4
    # passing = full suite exit 0.
    passing = full.get("ok") if full else None
    return {"skipped": False, "present": present, "passing": passing, "runs": runs}


# ----------------------------------------------------------------------------- #
# Artifacts
# ----------------------------------------------------------------------------- #
ARTIFACT_DIRS = (
    # Pass-8/closed-loop: include metrics/ so inspection_summary.json,
    # closed_loop_learning.json, paper_realism.json, bregman_execution.json, etc.
    # are bundled (data/ already carries data/training/*.jsonl + learning_state.json).
    "metrics", "data", "paper_artifacts", "training_artifacts", "shadow_artifacts",
    "post_canary_artifacts", "reports", "replay_artifacts",
    "production_review_artifacts", "guarded_live_artifacts", "micro_live_artifacts",
)

CONTAINER_ARTIFACT_PATHS = (
    "hermes-training:/data",
    "hermes-training:/app/data",
    "hermes-training:/app/paper_artifacts",
    "hermes-training:/app/training_artifacts",
    "hermes-training:/app/shadow_artifacts",
    "hermes-training:/app/post_canary_artifacts",
    "hermes-training:/app/reports",
)


def collect_artifacts(repo_root: str, dest_dir: Path, data_dir: Optional[str] = None,
                      names: tuple = ARTIFACT_DIRS, max_bytes: int = 25 * 1024 * 1024,
                      include_container: bool = False,
                      runner: Optional[Runner] = None) -> dict:
    """Copy present artifact folders into ``dest_dir`` (size-capped). Missing
    folders are recorded, never fatal. Returns a manifest dict."""
    repo = Path(repo_root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    found: list[dict] = []
    missing: list[str] = []
    copied_bytes = 0

    search_roots = [repo]
    if data_dir:
        search_roots.append(Path(data_dir))

    seen = set()
    for name in names:
        located = None
        for root in search_roots:
            cand = root / name
            if cand.exists() and cand.is_dir() and str(cand.resolve()) not in seen:
                located = cand
                break
        if located is None:
            missing.append(name)
            continue
        seen.add(str(located.resolve()))
        size = _dir_size(located)
        target = dest_dir / name
        try:
            if copied_bytes + size > max_bytes:
                found.append({"name": name, "path": str(located), "bytes": size,
                              "copied": False, "reason": "skipped: bundle size cap reached"})
                continue
            shutil.copytree(located, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            copied_bytes += size
            found.append({"name": name, "path": str(located), "bytes": size, "copied": True})
        except Exception as exc:  # noqa: BLE001
            found.append({"name": name, "path": str(located), "bytes": size,
                          "copied": False, "reason": f"{type(exc).__name__}: {exc}"})

    container = {}
    if include_container:
        container = _export_container_artifacts(repo_root, dest_dir / "container", runner)

    return {
        "host_found": found,
        "host_missing": missing,
        "copied_bytes": copied_bytes,
        "container": container,
        "any_found": bool(found) or bool(container.get("found")),
    }


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return total


def _export_container_artifacts(repo_root: str, dest: Path,
                                runner: Optional[Runner] = None) -> dict:
    """Best-effort ``docker compose cp`` of container artifact paths. Never fatal."""
    dest.mkdir(parents=True, exist_ok=True)
    found, missing = [], []
    for src in CONTAINER_ARTIFACT_PATHS:
        label = src.replace(":", "_").replace("/", "_").strip("_")
        rec = run_cmd(["docker", "compose", "cp", src, str(dest / label)],
                      cwd=repo_root, timeout=120, runner=runner)
        if rec["ok"]:
            found.append({"src": src, "dest": str(dest / label)})
        else:
            missing.append({"src": src, "error": rec["error"]})
    return {"found": found, "missing": missing}


# ----------------------------------------------------------------------------- #
# Local status JSON (no Docker needed)
# ----------------------------------------------------------------------------- #
def read_local_status(data_dir: Optional[str], repo_root: str) -> dict:
    """Read polymarket_training.json from the data dir / common locations.

    Returns ``{"available": bool, "status": {...}, "source": path|None}``.
    """
    candidates = []
    if data_dir:
        candidates.append(Path(data_dir) / "polymarket_training.json")
    candidates += [
        Path(repo_root) / "data" / "polymarket_training.json",
        Path(repo_root) / "polymarket_training.json",
    ]
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {"available": True, "status": data, "source": str(path)}
        except Exception:  # noqa: BLE001
            continue
    return {"available": False, "status": {}, "source": None}


def extract_status_from_docker(docker: dict) -> dict:
    """Parse the JSON training status emitted by the docker exec (if any)."""
    rec = (docker or {}).get("training_status_json") or {}
    out = (rec.get("stdout") or "").strip()
    if rec.get("ok") and out.startswith("{"):
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {}
    return {}
