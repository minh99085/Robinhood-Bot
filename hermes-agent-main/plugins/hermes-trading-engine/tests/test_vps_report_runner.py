"""Tests for the permanent VPS light-report runner + report dependency hardening.

Proves: pytest/report subprocess uses ``sys.executable -m pytest`` (never bare
pytest/python); the dependency check reports missing modules with an explicit,
actionable message; and the runner script (scripts/vps_generate_light_report.sh)
self-bootstraps a venv, uses .report_venv python only, refreshes runtime_data, cleans
old reports, fails fast on missing deps, prints container health, and packages a unique
zip + a 'latest' zip including inspection_reports, runtime_data/metrics, and validation.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspection_collectors as ic  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
RUNNER = PLUGIN / "scripts" / "vps_generate_light_report.sh"


def _runner_text() -> str:
    return RUNNER.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Subprocess hardening
# --------------------------------------------------------------------------- #
def test_pytest_base_cmd_uses_sys_executable():
    cmd = ic.pytest_base_cmd()
    assert cmd[0] == sys.executable                # never bare "pytest"/"python"
    assert cmd[1:3] == ["-m", "pytest"]


def test_collect_tests_runs_via_sys_executable_module_pytest(tmp_path):
    seen = []

    def runner(cmd, cwd=None, timeout=None):
        seen.append(list(cmd))
        return (0, "1 passed", "")
    ic.collect_tests(str(tmp_path), runner=runner, selectors={"full": []})
    assert seen and seen[0][0] == sys.executable and seen[0][1:3] == ["-m", "pytest"]
    assert "pytest" not in seen[0][0]              # not a bare pytest binary


# --------------------------------------------------------------------------- #
# Dependency status
# --------------------------------------------------------------------------- #
def test_dependency_status_ok_when_all_present():
    st = ic.report_dependency_status(finder=lambda m: object())
    assert st["ok"] is True and st["missing"] == [] and st["message"] == ""


def test_dependency_status_reports_missing_explicitly():
    fake = lambda m: None if m in ("pydantic", "pytest", "numpy") else object()  # noqa: E731
    st = ic.report_dependency_status(finder=fake)
    assert st["ok"] is False
    assert set(st["missing"]) == {"pydantic", "pytest", "numpy"}
    assert "missing module(s)" in st["message"]
    assert "vps_generate_light_report.sh" in st["message"]   # actionable fix
    assert "pip-install by hand" in st["message"]            # tells operator NOT to do it by hand


def test_required_modules_cover_the_known_failures():
    for mod in ("pydantic", "pytest", "numpy"):
        assert mod in ic.REQUIRED_REPORT_MODULES


# --------------------------------------------------------------------------- #
# Runner script invariants (static; the script does real VPS/docker work)
# --------------------------------------------------------------------------- #
def test_runner_script_exists_and_executable():
    assert RUNNER.is_file()
    mode = RUNNER.stat().st_mode
    assert mode & stat.S_IXUSR                      # executable bit set


def test_runner_uses_venv_python_not_system():
    t = _runner_text()
    assert ".report_venv" in t
    assert 'VPY="${VENV}/bin/python"' in t
    # report + validation are invoked through the venv python, never bare `python`
    assert '"${VPY}" scripts/generate_bot_inspection_report.py' in t
    assert '"${VPY}" scripts/validate_training_runtime.py' in t
    assert "python scripts/generate_bot_inspection_report.py" not in t
    assert "python scripts/validate_training_runtime.py" not in t


def test_runner_installs_required_dependencies():
    t = _runner_text()
    assert "-m venv" in t                            # self-bootstraps the venv
    assert "requirements.txt" in t and "requirements-dev.txt" in t
    for dep in ("pytest", "pydantic", "numpy"):
        assert dep in t                              # explicit must-have installs
    assert "report_dependency_status" in t           # fail-fast dependency check


def test_runner_refreshes_runtime_data_and_cleans_reports():
    t = _runner_text()
    assert "rm -rf runtime_data" in t
    assert "docker cp" in t and ":/data" in t
    assert "chown" in t
    assert "rm -rf inspection_reports" in t


def test_runner_prints_container_health_before_report():
    t = _runner_text()
    assert "docker compose ps" in t
    assert "docker logs" in t                        # tail of hermes-training logs
    assert "State.Status" in t                       # stale/stopped/unhealthy detection


def test_runner_packages_unique_and_latest_zip_with_required_inputs():
    t = _runner_text()
    assert 'ZIP_NAME="vps_light_report_${TS}.zip"' in t          # unique timestamped
    assert 'ZIP_LATEST="vps_light_report_latest.zip"' in t       # latest pointer
    assert 'cp -f "${ZIP_NAME}" "${ZIP_LATEST}"' in t
    assert "inspection_reports" in t
    assert "runtime_data/metrics" in t                           # metrics included
    assert "validation_light_latest.txt" in t                    # validation included
    assert "report_logs" in t                                    # report logs included


def test_runner_surfaces_report_exit_code_does_not_hide_failures():
    t = _runner_text()
    # the script exits with the report generator's own rc (run-ready gating preserved)
    assert 'REPORT_RC=${PIPESTATUS[0]}' in t
    assert 'exit "${REPORT_RC}"' in t
    assert "set -euo pipefail" in t
