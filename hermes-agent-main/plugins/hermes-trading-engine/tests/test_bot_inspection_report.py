"""End-to-end tests for the bot inspection report generator.

All subprocess + HTTP calls are mocked via injectable runner/opener, so these
tests need no Docker, no network, and no real pytest subprocess.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_bot_inspection_report as gen  # noqa: E402
import inspection_collectors as collectors  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def make_runner(pytest_result=None, docker_ok=False, docker_status_json=None):
    def runner(cmd, cwd, timeout):
        if cmd[:1] == ["git"]:
            return (0, "git output line", "")
        if cmd[:1] == ["docker"]:
            if not docker_ok:
                return (127, "", "docker: command not found")
            has_status = any("polymarket_training_status.py" in c for c in cmd)
            if has_status and "--json" in cmd:
                return (0, docker_status_json or "{}", "")
            return (0, "docker ok", "")
        if "pytest" in cmd:
            if pytest_result is None:
                return (127, "", "No module named pytest")
            return pytest_result
        return (0, "", "")
    return runner


def unreachable_opener(url, timeout):
    return (0, "connection refused")


def make_opener(payloads):
    def opener(url, timeout):
        for frag, body in payloads.items():
            if frag in url:
                return (200, json.dumps(body))
        return (0, "connection refused")
    return opener


def _write_status(data_dir: Path, status: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "polymarket_training.json").write_text(json.dumps(status), encoding="utf-8")


def _healthy_status():
    return {
        "mode": "paper", "runtime_seconds": 7200,
        "pnl": {"open_positions": 1, "trades_closed": 50, "equity": 520.0,
                "total_pnl": 20.0, "win_rate": 0.6},
        "scan_metrics": {"scanned": 1000, "kept": 80},
        "safety": {"ok": True, "live_detected": False},
        "monitoring": {"bregman_opportunities": 10, "certified_bregman_profit": 2.0},
        "btc_pulse": {"btc_pulse_enabled": True, "btc_pulse_oracle_required": True,
                      "btc_pulse_paper_trades": 5, "btc_pulse_after_cost_pnl": 1.0},
        "news": {"news_scanner_enabled": True, "news_provider_mode": "offline_cache",
                 "news_items_fetched": 50, "news_items_used": 20},
        "btc_fast_price": {"enabled": True, "valid": True, "age_seconds": 1.0},
        "campaign_safety": {"realistic_fill_enabled": True},
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_report_creates_folder_zip_and_valid_json(tmp_path):
    out = tmp_path / "inspection_reports"
    res = gen.generate_report(
        output_dir=str(out), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        runner=make_runner(), opener=unreachable_opener)
    bundle = Path(res["bundle_dir"])
    zip_path = Path(res["zip_path"])
    assert bundle.is_dir()
    assert zip_path.is_file()
    assert (bundle / "report.md").is_file()
    rj = json.loads((bundle / "report.json").read_text())
    assert rj["classification"] in (
        "PASS", "PASS_WITH_WARNINGS", "FAIL", "REGRESSION", "CRITICAL_SAFETY_FAIL")
    # zip contains report.json + report.md
    names = zipfile.ZipFile(zip_path).namelist()
    assert any(n.endswith("report.json") for n in names)
    assert any(n.endswith("report.md") for n in names)


def test_report_continues_without_docker(tmp_path):
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=True, include_api=False, include_artifacts=False,
        runner=make_runner(docker_ok=False), opener=unreachable_opener)
    assert Path(res["report_json"]).is_file()
    rj = json.loads(Path(res["report_json"]).read_text())
    assert rj["runtime"]["docker_available"] is False


def test_report_continues_without_api(tmp_path):
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=True, include_artifacts=False,
        runner=make_runner(), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    assert all(not e.get("ok") for e in rj["api"].values())
    # Unreachable endpoints recorded, not fatal.
    assert (Path(res["bundle_dir"]) / "api" / "health.json").is_file()


def test_report_continues_when_tests_missing(tmp_path):
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=False,
        include_docker=False, include_api=False, include_artifacts=False,
        runner=make_runner(pytest_result=None), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    assert rj["tests"]["present"] is not True  # pytest missing → not present
    assert Path(res["bundle_dir"], "test_results_full.txt").is_file()


def test_pass_with_warnings_when_feature_gaps(tmp_path):
    data_dir = tmp_path / "data"
    _write_status(data_dir, _healthy_status())
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(data_dir), runner=make_runner(), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    # Safety OK + runtime available + skipped tests, but chainlink missing (no API)
    # → PASS_WITH_WARNINGS, never PASS.
    assert rj["safety"]["status"] == "OK"
    assert rj["classification"] == "PASS_WITH_WARNINGS"


def test_critical_safety_fail_when_live_flag_enabled(tmp_path):
    (tmp_path / ".env").write_text("HTE_MODE=paper\nMICRO_LIVE_ENABLED=1\n", encoding="utf-8")
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        runner=make_runner(), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    assert rj["classification"] == "CRITICAL_SAFETY_FAIL"
    assert "MICRO_LIVE_ENABLED" in rj["safety"]["summary"]["forbidden_enabled"]


def test_no_secret_leaks_in_bundle(tmp_path):
    (tmp_path / ".env").write_text(
        "GROK_API_KEY=xai-SUPERSECRETKEY1234567890\nHTE_MODE=paper\n", encoding="utf-8")
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        runner=make_runner(), opener=unreachable_opener)
    bundle = Path(res["bundle_dir"])
    for p in bundle.rglob("*"):
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            assert "xai-SUPERSECRETKEY1234567890" not in text, p


def test_regression_classification_with_baseline(tmp_path):
    data_dir = tmp_path / "data"
    _write_status(data_dir, _healthy_status())
    # Baseline with materially better metrics → current is a regression.
    baseline = {"features": {"equity": 2000.0, "total_pnl": 500.0,
                             "after_cost_pnl": 50.0, "tests_passing": True,
                             "chainlink_valid": True, "sharpe": 2.0,
                             "win_rate_traded_only": 0.9, "max_drawdown": 0.01,
                             "btc_pulse_after_cost_pnl": 50.0,
                             "bregman_certified_profit": 50.0}}
    base_path = tmp_path / "baseline.json"
    base_path.write_text(json.dumps(baseline), encoding="utf-8")
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(data_dir), baseline_path=str(base_path),
        runner=make_runner(), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    assert rj["performance_comparison"]["available"] is True
    assert rj["performance_comparison"]["regression"] is True
    assert rj["classification"] == "REGRESSION"


def test_optional_pr_context_not_required_but_used_in_name(tmp_path):
    # Without --pr: works and name has no pr token.
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        runner=make_runner(), opener=unreachable_opener)
    assert "bot_inspection_pr" not in Path(res["bundle_dir"]).name
    rj = json.loads(Path(res["report_json"]).read_text())
    assert rj["optional_pr_context"] is None
    # With --pr: name includes prNN and context recorded.
    res2 = gen.generate_report(
        output_dir=str(tmp_path / "out2"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False, pr="34",
        runner=make_runner(), opener=unreachable_opener)
    assert "bot_inspection_pr34_" in Path(res2["zip_path"]).name
    rj2 = json.loads(Path(res2["report_json"]).read_text())
    assert rj2["optional_pr_context"] == {"pr": "34"}


def test_artifact_missing_folders_recorded(tmp_path):
    # One artifact dir present, the rest missing → recorded, not fatal.
    (tmp_path / "paper_artifacts").mkdir()
    (tmp_path / "paper_artifacts" / "x.json").write_text("{}", encoding="utf-8")
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=True,
        runner=make_runner(), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    found = {a["name"] for a in rj["artifacts"]["host_found"]}
    assert "paper_artifacts" in found
    assert "shadow_artifacts" in rj["artifacts"]["host_missing"]


def test_scorecard_present_and_bounded(tmp_path):
    data_dir = tmp_path / "data"
    _write_status(data_dir, _healthy_status())
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(data_dir), runner=make_runner(), opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    assert 0 <= rj["scorecard"]["score"] <= 100
    assert set(rj["scorecard"]["components"]) == {
        "safety", "tests", "runtime", "feature_completeness",
        "performance_trend", "observability"}


def test_report_includes_benchmarks_consistency_quant(tmp_path):
    data_dir = tmp_path / "data"
    _write_status(data_dir, _healthy_status())
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=False, include_artifacts=False,
        data_dir=str(data_dir), runner=make_runner(), opener=unreachable_opener)
    bundle = Path(res["bundle_dir"])
    rj = json.loads((bundle / "report.json").read_text())
    # report.json keys
    assert "benchmarks" in rj and "benchmarks" in rj["benchmarks"]
    assert "consistency" in rj
    assert "quant_responsibilities" in rj and "data_ingestion" in rj["quant_responsibilities"]
    # bundle artifacts
    assert (bundle / "metrics" / "benchmarks.json").is_file()
    assert (bundle / "consistency.json").is_file()
    assert (bundle / "quant_responsibilities.json").is_file()
    # report.md sections
    md = (bundle / "report.md").read_text()
    assert "Algorithmic Benchmarks" in md
    assert "Cross-Surface Consistency" in md
    assert "Quant Responsibilities" in md


def test_report_flags_equity_inconsistency(tmp_path):
    data_dir = tmp_path / "data"
    _write_status(data_dir, _healthy_status())  # paper equity 520
    opener = make_opener({
        "/api/health": {"ok": True, "mode": "paper"},
        "/api/state": {"equity": 100.0, "mode": "paper"},  # dashboard equity differs
    })
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=True,
        include_docker=False, include_api=True, include_artifacts=False,
        data_dir=str(data_dir), runner=make_runner(), opener=opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    checks = {c["check"] for c in rj["consistency"]}
    assert "equity_mismatch" in checks


def test_pytest_base_cmd_is_platform_safe():
    cmd = collectors.pytest_base_cmd()
    assert cmd[0] == sys.executable
    assert "pytest" in cmd
    assert "no:cacheprovider" in cmd
    # Never force the signal method — SIGALRM crashes pytest on Windows.
    assert "--timeout-method=signal" not in cmd
    import importlib.util
    if importlib.util.find_spec("pytest_timeout") is not None:
        assert "--timeout-method=thread" in cmd


def test_passing_tests_via_docker_json_status(tmp_path):
    # Docker available + emits JSON status + pytest passes → status collected.
    status_json = json.dumps(_healthy_status())
    res = gen.generate_report(
        output_dir=str(tmp_path / "out"), repo_root=str(tmp_path), skip_tests=False,
        include_docker=True, include_api=False, include_artifacts=False,
        runner=make_runner(pytest_result=(0, "100 passed", ""), docker_ok=True,
                           docker_status_json=status_json),
        opener=unreachable_opener)
    rj = json.loads(Path(res["report_json"]).read_text())
    assert rj["runtime"]["available"] is True
    assert rj["tests"]["passing"] is True
