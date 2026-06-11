"""Tests for the laptop operator CLI (scripts/laptop_agent.py).

All subprocess calls go through an INJECTED runner, so these tests never touch the
network, Docker, git remotes, or the VPS. They prove:
  * command construction is EXACTLY the documented form,
  * config loads from JSON and from an env file,
  * dry-run is the default and never executes destructive (VPS/runtime_data) commands,
  * secrets from local config are never printed,
  * a missing config produces a clear setup message (no crash),
  * status renders SAFE / STOP correctly.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import laptop_agent as la  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class SpyRunner:
    """Records every command it is asked to run and returns scripted results."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        key = argv[0] if argv else ""
        # allow keying by first token OR by a 'contains' marker
        for marker, resp in self._responses.items():
            if marker in argv or any(marker in str(t) for t in argv):
                return resp
        return self._responses.get(key, (0, "", ""))

    def ran(self, *tokens) -> bool:
        return any(all(t in call or any(t in str(c) for c in call) for t in tokens)
                   for call in self.calls)


def collector():
    lines = []
    return lines, lines.append


def write_json_config(tmp_path: Path, **kw) -> Path:
    p = tmp_path / la.CONFIG_JSON
    p.write_text(json.dumps(kw), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Parser / help
# --------------------------------------------------------------------------- #
def test_parser_builds_and_lists_commands():
    p = la.build_parser()
    for name in ("status", "verify-sync", "local-head", "remote-head",
                 "check-docker", "check-vps", "collect", "report",
                 "validate", "package", "repo-status"):
        assert name in la.COMMANDS
    ns = p.parse_args(["status"])
    assert ns.command == "status"
    assert ns.execute is False           # dry-run is the default


def test_no_command_prints_help_returns_zero():
    lines, printer = collector()
    rc = la.main([], runner=SpyRunner(), printer=printer)
    assert rc == 0


# --------------------------------------------------------------------------- #
# Exact command construction (acceptance #7)
# --------------------------------------------------------------------------- #
def test_inspection_report_command_is_exact():
    cfg = la.Config()
    assert la.build_inspection_report_cmd(cfg, python_bin="python") == [
        "python", "scripts/generate_bot_inspection_report.py",
        "--output", "inspection_reports",
        "--data-dir", "runtime_data",
        "--bundle-mode", "light"]


def test_validate_command_is_exact():
    cfg = la.Config()
    assert la.build_validate_cmd(cfg, python_bin="python") == [
        "python", "scripts/validate_training_runtime.py",
        "--data-dir", "runtime_data"]


def test_report_command_honours_config_dirs():
    cfg = la.Config(runtime_data_dir="rd2", inspection_output_dir="out2")
    assert la.build_inspection_report_cmd(cfg, python_bin="python") == [
        "python", "scripts/generate_bot_inspection_report.py",
        "--output", "out2", "--data-dir", "rd2", "--bundle-mode", "light"]


# --------------------------------------------------------------------------- #
# Config loading (acceptance #4 + env support)
# --------------------------------------------------------------------------- #
def test_config_loads_from_json(tmp_path):
    write_json_config(tmp_path, vps_host="h", vps_user="u", vps_port=2222,
                      runtime_source="u@h:/p/")
    cfg, found = la.load_config(repo_root=tmp_path, env={})
    assert found is True
    assert cfg.vps_host == "h" and cfg.vps_user == "u" and cfg.vps_port == 2222
    assert cfg.vps_configured() and cfg.collect_configured()


def test_config_loads_from_env_file(tmp_path):
    (tmp_path / la.CONFIG_ENV).write_text(
        "LAPTOP_AGENT_VPS_HOST=eh\nLAPTOP_AGENT_VPS_USER=eu\n"
        "LAPTOP_AGENT_VPS_PORT=2200\n# comment\n", encoding="utf-8")
    cfg, found = la.load_config(repo_root=tmp_path, env={})
    assert found is True
    assert cfg.vps_host == "eh" and cfg.vps_user == "eu" and cfg.vps_port == 2200


def test_missing_config_is_not_an_error(tmp_path):
    cfg, found = la.load_config(repo_root=tmp_path, env={})
    assert found is False
    assert isinstance(cfg, la.Config) and not cfg.vps_configured()


def test_missing_config_check_vps_shows_setup_message_no_crash(tmp_path):
    lines, printer = collector()
    spy = SpyRunner()
    rc = la.main(["check-vps"], runner=spy, printer=printer, repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 2
    assert "No local operator config found" in out
    assert la.EXAMPLE_CONFIG in out
    assert not spy.ran("ssh")            # never attempted SSH without config


# --------------------------------------------------------------------------- #
# Dry-run safety (acceptance #3, #6)
# --------------------------------------------------------------------------- #
def test_dry_run_collect_does_not_execute_rsync(tmp_path):
    write_json_config(tmp_path, vps_host="h", vps_user="u",
                      runtime_source="u@h:/data/")
    lines, printer = collector()
    spy = SpyRunner()
    rc = la.main(["collect"], runner=spy, printer=printer, repo_root=tmp_path, env={})
    assert rc == 0
    assert not spy.ran("rsync")          # DESTRUCTIVE command never executed
    assert any("DRY-RUN" in ln for ln in lines)


def test_dry_run_check_vps_does_not_execute_ssh(tmp_path):
    write_json_config(tmp_path, vps_host="h", vps_user="u", vps_ssh_key="k")
    lines, printer = collector()
    spy = SpyRunner()
    rc = la.main(["check-vps"], runner=spy, printer=printer, repo_root=tmp_path, env={})
    assert rc == 0
    assert not spy.ran("ssh")
    assert any("DRY-RUN" in ln for ln in lines)


def test_dry_run_report_does_not_execute_generator(tmp_path):
    write_json_config(tmp_path)
    (tmp_path / "runtime_data").mkdir()
    (tmp_path / "runtime_data" / "x.json").write_text("{}", encoding="utf-8")
    lines, printer = collector()
    spy = SpyRunner()
    rc = la.main(["report"], runner=spy, printer=printer, repo_root=tmp_path, env={})
    assert rc == 0
    assert not spy.ran("generate_bot_inspection_report.py")
    assert any("DRY-RUN" in ln for ln in lines)


def test_execute_collect_runs_rsync(tmp_path):
    write_json_config(tmp_path, vps_host="h", vps_user="u",
                      runtime_source="u@h:/data/")
    lines, printer = collector()
    spy = SpyRunner({"rsync": (0, "", "")})
    rc = la.main(["collect", "--execute"], runner=spy, printer=printer,
                 repo_root=tmp_path, env={})
    assert rc == 0
    assert spy.ran("rsync")              # destructive command runs ONLY with --execute


def test_dry_run_flag_overrides_execute(tmp_path):
    write_json_config(tmp_path, vps_host="h", vps_user="u",
                      runtime_source="u@h:/data/")
    spy = SpyRunner({"rsync": (0, "", "")})
    rc = la.main(["collect", "--execute", "--dry-run"], runner=spy,
                 printer=lambda *_: None, repo_root=tmp_path, env={})
    assert rc == 0
    assert not spy.ran("rsync")          # explicit --dry-run keeps it safe


# --------------------------------------------------------------------------- #
# Secret safety (acceptance #8)
# --------------------------------------------------------------------------- #
def test_secrets_never_printed_in_status(tmp_path):
    secret_host = "SECRET-HOST-10.20.30.40"
    secret_key = "C:\\secret\\KEY-DO-NOT-LEAK"
    secret_src = "ubuntu@SECRET-HOST-10.20.30.40:/opt/secret/runtime_data/"
    write_json_config(tmp_path, vps_host=secret_host, vps_user="ubuntu",
                      vps_ssh_key=secret_key, runtime_source=secret_src)
    lines, printer = collector()
    spy = SpyRunner({"rev-parse": (0, "abc123\n", ""),
                     "ls-remote": (0, "abc123\trefs/heads/main\n", ""),
                     "status": (0, "", ""), "version": (0, "27.0\n", "")})
    la.main(["status"], runner=spy, printer=printer, repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert secret_host not in out
    assert secret_key not in out
    assert secret_src not in out


def test_secrets_never_printed_in_dry_run_collect(tmp_path):
    secret_src = "ubuntu@SECRET-HOST:/opt/secret/runtime_data/"
    secret_key = "SECRET-KEY-PATH"
    write_json_config(tmp_path, vps_host="SECRET-HOST", vps_user="ubuntu",
                      vps_ssh_key=secret_key, runtime_source=secret_src)
    lines, printer = collector()
    la.main(["collect"], runner=SpyRunner(), printer=printer,
            repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert "SECRET-HOST" not in out
    assert secret_key not in out
    assert secret_src not in out
    assert "<redacted" in out            # proves the command WAS shown, but masked


# --------------------------------------------------------------------------- #
# status SAFE / STOP + works without config (acceptance #2)
# --------------------------------------------------------------------------- #
def _sync_runner(local="aaa\n", remote="aaa\trefs/heads/main\n", dirty="", docker="27\n"):
    return SpyRunner({"rev-parse": (0, local, ""),
                      "ls-remote": (0, remote, ""),
                      "status": (0, dirty, ""),
                      "version": (0, docker, "")})


def test_status_safe_when_clean_and_in_sync(tmp_path):
    lines, printer = collector()
    rc = la.main(["status"], runner=_sync_runner(), printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 0
    assert "SAFE TO CONTINUE" in out
    assert "NEXT COMMAND:" in out
    assert "UPLOAD REPORT TO CHATGPT:" in out


def test_status_stop_when_out_of_sync(tmp_path):
    lines, printer = collector()
    runner = _sync_runner(local="aaa\n", remote="bbb\trefs/heads/main\n")
    rc = la.main(["status"], runner=runner, printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 3
    assert "STOP" in out
    assert "git pull" in out


def test_status_stop_when_repo_dirty(tmp_path):
    lines, printer = collector()
    runner = _sync_runner(dirty=" M somefile.py\n")
    rc = la.main(["status"], runner=runner, printer=printer,
                 repo_root=tmp_path, env={})
    assert rc == 3
    assert "STOP" in "\n".join(lines)


def test_status_works_without_config(tmp_path):
    lines, printer = collector()
    rc = la.main(["status"], runner=_sync_runner(), printer=printer,
                 repo_root=tmp_path, env={})    # no config file present
    out = "\n".join(lines)
    assert rc == 0
    assert "vps_configured             : False" in out or "vps_configured" in out


# --------------------------------------------------------------------------- #
# validate never hides failure
# --------------------------------------------------------------------------- #
def test_validate_failure_prints_stop(tmp_path):
    write_json_config(tmp_path)
    lines, printer = collector()
    spy = SpyRunner({"validate_training_runtime.py": (1, "FAIL details", "boom")})
    rc = la.main(["validate", "--execute"], runner=spy, printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 3
    assert "STOP" in out and "FAILED" in out


# --------------------------------------------------------------------------- #
# packaging
# --------------------------------------------------------------------------- #
def test_package_dry_run_does_not_create_zip(tmp_path):
    _seed_report(tmp_path, local_head="aaa")              # fresh, valid provenance
    lines, printer = collector()
    rc = la.main(["package"], runner=_sync_runner(local="aaa\n"), printer=printer,
                 repo_root=tmp_path, env={})
    assert rc == 0
    assert any("DRY-RUN" in ln for ln in lines)
    assert not list((tmp_path / "inspection_reports").rglob("*.zip"))


def test_package_execute_creates_zip(tmp_path):
    _seed_report(tmp_path, local_head="aaa")
    fixed = _dt.datetime(2026, 6, 11, 1, 2, 3)
    lines, printer = collector()
    rc = la.main(["package", "--execute"], runner=_sync_runner(local="aaa\n"),
                 printer=printer, now_fn=lambda: fixed, repo_root=tmp_path, env={})
    assert rc == 0
    zips = list((tmp_path / "inspection_reports").rglob("*.zip"))
    assert len(zips) == 1
    assert "20260611_010203" in zips[0].name


def test_package_missing_report_guides_operator(tmp_path):
    lines, printer = collector()
    rc = la.main(["package", "--execute"], runner=_sync_runner(), printer=printer,
                 repo_root=tmp_path, env={})
    assert rc == 3                       # STOP: nothing fresh to package
    assert "fresh-package --execute" in "\n".join(lines)


# --------------------------------------------------------------------------- #
# Phase 2 — fresh report/package provenance guardrails
# --------------------------------------------------------------------------- #
def _seed_report(tmp_path, *, local_head="aaa", with_provenance=True,
                 validated=True, report_epoch=100.0, validation_epoch=200.0):
    """Create inspection_reports/run1 with report.json and (optionally) provenance."""
    rd = tmp_path / "inspection_reports" / "run1"
    rd.mkdir(parents=True)
    (rd / "report.json").write_text("{}", encoding="utf-8")
    if with_provenance:
        prov = {"report_generated_at": "2026-06-11T04:00:00",
                "report_generated_epoch": report_epoch,
                "local_head": local_head, "remote_main_head": local_head,
                "repo_clean": True, "in_sync_with_main": True,
                "report_dir": "inspection_reports/run1"}
        if validated:
            prov["validation_completed_at"] = "2026-06-11T04:01:00"
            prov["validation_epoch"] = validation_epoch
            prov["validation_result_path"] = la.VALIDATION_RESULT
        (rd / la.REPORT_PROVENANCE).write_text(json.dumps(prov), encoding="utf-8")
    return rd


def test_package_refuses_stale_report_without_provenance(tmp_path):
    _seed_report(tmp_path, with_provenance=False)
    lines, printer = collector()
    rc = la.main(["package", "--execute"], runner=_sync_runner(), printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 3 and "STOP" in out
    assert "no provenance" in out
    assert not list((tmp_path / "inspection_reports").rglob("*.zip"))


def test_package_refuses_dirty_repo(tmp_path):
    _seed_report(tmp_path, local_head="aaa")
    lines, printer = collector()
    runner = _sync_runner(dirty=" M f.py\n")          # provenance valid, but repo dirty
    rc = la.main(["package", "--execute"], runner=runner, printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 3 and "STOP" in out and "dirty" in out
    assert not list((tmp_path / "inspection_reports").rglob("*.zip"))


def test_package_refuses_head_mismatch(tmp_path):
    _seed_report(tmp_path, local_head="OLD_HEAD")
    lines, printer = collector()
    runner = _sync_runner(local="NEW_HEAD\n", remote="NEW_HEAD\trefs/heads/main\n")
    rc = la.main(["package", "--execute"], runner=runner, printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 3 and "does not match current local HEAD" in out


def test_package_refuses_when_validation_before_report(tmp_path):
    _seed_report(tmp_path, report_epoch=300.0, validation_epoch=100.0)
    lines, printer = collector()
    rc = la.main(["package", "--execute"], runner=_sync_runner(local="aaa\n"),
                 printer=printer, repo_root=tmp_path, env={})
    assert rc == 3
    assert "validation ran before the report" in "\n".join(lines)


def test_package_allow_stale_override_proceeds(tmp_path):
    _seed_report(tmp_path, with_provenance=False)
    lines, printer = collector()
    rc = la.main(["package", "--execute", "--allow-stale-package"],
                 runner=_sync_runner(), printer=printer, repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 0 and "WARNING" in out
    zips = list((tmp_path / "inspection_reports").rglob("*.zip"))
    assert len(zips) == 1                # override DID create a package


def test_package_fresh_report_succeeds(tmp_path):
    _seed_report(tmp_path, local_head="aaa", report_epoch=100.0, validation_epoch=200.0)
    fixed = _dt.datetime(2026, 6, 11, 4, 5, 6)
    lines, printer = collector()
    rc = la.main(["package", "--execute"], runner=_sync_runner(local="aaa\n"),
                 printer=printer, now_fn=lambda: fixed, repo_root=tmp_path, env={})
    assert rc == 0
    zips = list((tmp_path / "inspection_reports").rglob("*.zip"))
    assert len(zips) == 1
    # package provenance written into the report dir (and thus the zip)
    pkg_prov = tmp_path / "inspection_reports" / "run1" / la.PACKAGE_PROVENANCE
    assert pkg_prov.is_file()


# ---- fresh-package -------------------------------------------------------- #
class FreshRunner:
    """Simulates git probes + the report/validate scripts. The report script
    side-effect creates a fresh bundle dir so find_latest_report_dir works."""

    def __init__(self, repo_root, *, local="aaa", remote="aaa", dirty="",
                 make_report=True, validate_rc=0):
        self.repo_root = Path(repo_root)
        self.local, self.remote, self.dirty = local, remote, dirty
        self.make_report, self.validate_rc = make_report, validate_rc
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        s = " ".join(str(a) for a in argv)
        if "generate_bot_inspection_report.py" in s:
            if self.make_report:
                d = self.repo_root / "inspection_reports" / "bot_inspection_NEW"
                d.mkdir(parents=True, exist_ok=True)
                (d / "report.json").write_text("{}", encoding="utf-8")
            return (0, "report built", "")
        if "validate_training_runtime.py" in s:
            return (self.validate_rc, "validation output", "")
        if "rev-parse" in argv:
            return (0, self.local + "\n", "")
        if "ls-remote" in argv:
            return (0, self.remote + "\trefs/heads/main\n", "")
        if "status" in argv:
            return (0, self.dirty, "")
        if "version" in argv:
            return (0, "27\n", "")
        return (0, "", "")

    def index(self, marker):
        for i, c in enumerate(self.calls):
            if any(marker in str(t) for t in c):
                return i
        return -1


def _prep_runtime(tmp_path):
    (tmp_path / "runtime_data").mkdir(exist_ok=True)
    (tmp_path / "runtime_data" / "x.json").write_text("{}", encoding="utf-8")


def test_fresh_package_dry_run_shows_sequence_and_mutates_nothing(tmp_path):
    _prep_runtime(tmp_path)
    runner = FreshRunner(tmp_path)        # clean + in sync
    lines, printer = collector()
    rc = la.main(["fresh-package", "--dry-run"], runner=runner, printer=printer,
                 repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 0
    assert "DRY-RUN" in out
    assert "generate_bot_inspection_report.py" in out
    assert "validate_training_runtime.py" in out
    assert la.PACKAGE_PROVENANCE in out
    # mutated nothing: no report dir, no zip, no stale archive, no report script call
    assert not (tmp_path / "inspection_reports").exists()
    assert not list(tmp_path.glob(la.STALE_DIR_PREFIX + "*"))
    assert runner.index("generate_bot_inspection_report.py") == -1


def test_fresh_package_refuses_dirty_repo(tmp_path):
    _prep_runtime(tmp_path)
    runner = FreshRunner(tmp_path, dirty=" M f.py\n")
    lines, printer = collector()
    rc = la.main(["fresh-package", "--execute"], runner=runner, printer=printer,
                 repo_root=tmp_path, env={})
    assert rc == 3 and "STOP" in "\n".join(lines)
    assert runner.index("generate_bot_inspection_report.py") == -1   # never generated


def test_fresh_package_execute_sequence_is_correct(tmp_path):
    _prep_runtime(tmp_path)
    runner = FreshRunner(tmp_path, local="aaa", remote="aaa")
    fixed = _dt.datetime(2026, 6, 11, 4, 0, 0)
    lines, printer = collector()
    rc = la.main(["fresh-package", "--execute"], runner=runner, printer=printer,
                 now_fn=lambda: fixed, repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 0 and "SAFE TO CONTINUE" in out
    # exact order: report BEFORE validate
    i_report = runner.index("generate_bot_inspection_report.py")
    i_valid = runner.index("validate_training_runtime.py")
    assert 0 <= i_report < i_valid
    # a single fresh zip exists, plus provenance files inside the bundle
    zips = list((tmp_path / "inspection_reports").rglob("*.zip"))
    assert len(zips) == 1
    bundle = tmp_path / "inspection_reports" / "bot_inspection_NEW"
    assert (bundle / la.REPORT_PROVENANCE).is_file()
    assert (bundle / la.PACKAGE_PROVENANCE).is_file()
    assert (bundle / la.VALIDATION_RESULT).is_file()
    # the zip carries the provenance proof
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
    assert la.PACKAGE_PROVENANCE in names and la.REPORT_PROVENANCE in names


def test_fresh_package_archives_stale_reports(tmp_path):
    _prep_runtime(tmp_path)
    old = tmp_path / "inspection_reports" / "old_run"
    old.mkdir(parents=True)
    (old / "old_report.json").write_text('{"old":true}', encoding="utf-8")
    runner = FreshRunner(tmp_path)
    fixed = _dt.datetime(2026, 6, 11, 4, 0, 0)
    lines, printer = collector()
    rc = la.main(["fresh-package", "--execute"], runner=runner, printer=printer,
                 now_fn=lambda: fixed, repo_root=tmp_path, env={})
    assert rc == 0
    stale = list(tmp_path.glob(la.STALE_DIR_PREFIX + "*"))
    assert len(stale) == 1
    assert (stale[0] / "old_run" / "old_report.json").is_file()   # old evidence archived
    # the live report dir is the FRESH one, not the old one
    assert not (tmp_path / "inspection_reports" / "old_run").exists()
    assert (tmp_path / "inspection_reports" / "bot_inspection_NEW").is_file() is False
    assert (tmp_path / "inspection_reports" / "bot_inspection_NEW").is_dir()


def test_fresh_package_validation_failure_stops_without_zip(tmp_path):
    _prep_runtime(tmp_path)
    runner = FreshRunner(tmp_path, validate_rc=1)
    fixed = _dt.datetime(2026, 6, 11, 4, 0, 0)
    lines, printer = collector()
    rc = la.main(["fresh-package", "--execute"], runner=runner, printer=printer,
                 now_fn=lambda: fixed, repo_root=tmp_path, env={})
    out = "\n".join(lines)
    assert rc == 3 and "validation FAILED" in out.lower() or "validation failed" in out.lower()
    assert not list((tmp_path / "inspection_reports").rglob("*.zip"))


def test_fresh_package_provenance_has_no_secrets(tmp_path):
    _prep_runtime(tmp_path)
    secret_host = "SECRET-HOST-9.9.9.9"
    secret_key = "C:\\secret\\KEYFILE"
    secret_src = "ubuntu@SECRET-HOST-9.9.9.9:/opt/secret/runtime_data/"
    write_json_config(tmp_path, vps_host=secret_host, vps_user="ubuntu",
                      vps_ssh_key=secret_key, runtime_source=secret_src)
    runner = FreshRunner(tmp_path)
    fixed = _dt.datetime(2026, 6, 11, 4, 0, 0)
    rc = la.main(["fresh-package", "--execute"], runner=runner,
                 printer=lambda *_: None, now_fn=lambda: fixed,
                 repo_root=tmp_path, env={})
    assert rc == 0
    bundle = tmp_path / "inspection_reports" / "bot_inspection_NEW"
    for fname in (la.PACKAGE_PROVENANCE, la.REPORT_PROVENANCE):
        text = (bundle / fname).read_text(encoding="utf-8")
        assert secret_host not in text
        assert secret_key not in text
        assert secret_src not in text
    # provenance still carries the required non-secret evidence
    pkg = json.loads((bundle / la.PACKAGE_PROVENANCE).read_text(encoding="utf-8"))
    for key in ("package_created_at", "local_head", "remote_main_head",
                "repo_clean", "in_sync_with_main", "report_dir", "package_path"):
        assert key in pkg


def test_help_lists_fresh_package():
    p = la.build_parser()
    assert "fresh-package" in la.COMMANDS
    ns = p.parse_args(["fresh-package", "--execute"])
    assert ns.command == "fresh-package" and ns.execute is True
