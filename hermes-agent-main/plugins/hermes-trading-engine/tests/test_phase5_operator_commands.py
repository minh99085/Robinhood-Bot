"""Discoverable Phase 5 coordinator safety-gate tests (PAPER ONLY).

These cover the autonomous operator commands so `python -m pytest tests -q` always runs
real Phase 5 tests (post-cursor-verify must never pass on zero collected tests). All
subprocess/SSH/git calls go through an INJECTED runner — no network, Docker, or VPS.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import laptop_agent_coordinator as co  # noqa: E402

_FIXED = _dt.datetime(2026, 6, 13, 6, 0, 0)


def _write_cfg(tmp_path, **over):
    keyf = tmp_path / "id_key"
    keyf.write_text("priv", encoding="utf-8")
    cfg = {"repo_root": str(tmp_path), "plugin_path": str(tmp_path),
           "vps_host": "SECRET-HOST", "vps_user": "ubuntu", "vps_port": 22,
           "vps_ssh_key": str(keyf),
           "vps_remote_plugin_path": "/opt/hermes/plugins/hermes-trading-engine",
           "local_artifact_dir": "artifacts"}
    cfg.update(over)
    p = tmp_path / co.DEFAULT_CONFIG
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def _runner(tmp_path, *, dirty="", branch="main", started=None):
    art = tmp_path / "artifacts"

    def runner(argv, cwd=None, timeout=None):
        s = " ".join(str(a) for a in argv)
        if "docker compose up" in s or "docker compose down" in s:
            if started is not None:
                started.append(s)
            return (0, "started\n", "")
        if argv and argv[0] == "git":
            if "rev-parse" in argv:
                return (0, (branch + "\n" if "--abbrev-ref" in argv else "deadbeef\n"), "")
            if "status" in argv:
                return (0, dirty, "")
            return (0, "", "")
        if argv and argv[0] == "scp":
            art.mkdir(parents=True, exist_ok=True)
            z = art / ("hermes_light_report_" + _FIXED.strftime("%Y%m%d_%H%M%S") + ".zip")
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("inspection_reports/report.json", "{}")
                zf.writestr("runtime_data/inspection_summary.json", "{}")
                zf.writestr("validation_light_latest.txt", "ok")
            return (0, "", "")
        if "echo hermes-coordinator-ok" in s:
            return (0, "hermes-coordinator-ok\n", "")
        if "import pydantic" in s:
            return (0, "remote python: /usr/bin/python3\n", "")
        if "docker version" in s:
            return (0, "27.0\n", "")
        if "docker inspect" in s:
            return (0, "running\n", "")
        if "-m" in argv and "pytest" in argv:
            return (0, "20 passed", "")
        return (0, "report ok", "")
    return runner


def _plugin(tmp_path):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "generate_bot_inspection_report.py").write_text("#", encoding="utf-8")


def _run(args, tmp_path, runner):
    lines = []
    rc = co.main(args, runner=runner, printer=lines.append,
                 which_fn=lambda n: f"/usr/bin/{n}" if n in {"git", "ssh", "scp"} else None,
                 now_fn=lambda: _FIXED)
    return rc, "\n".join(lines)


def _cfg_arg(tmp_path):
    return str(tmp_path / co.DEFAULT_CONFIG)


# --------------------------------------------------------------------------- #
# the 7 required Phase 5 safety-gate tests
# --------------------------------------------------------------------------- #
def test_operator_cycle_stops_after_report_handoff(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    started = []
    rc, out = _run(["operator-cycle", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path, started=started))
    assert rc == 0
    assert "SAFE TO CONTINUE" in out
    assert "UPLOAD TO CHATGPT" in out                        # exact upload instruction
    assert (tmp_path / "artifacts" / co.UPLOAD_INSTRUCTIONS).is_file()
    assert not started                                       # never starts a run


def test_record_chatgpt_decision_classifies_conservatively(tmp_path):
    _write_cfg(tmp_path)
    cases = [("Please STOP, unsafe.", "STOP_REQUIRED", 3),
             ("Needs a fix — paste into Cursor.", "CURSOR_PROMPT_REQUIRED", 0),
             ("Long run approved.", "LONG_RUN_APPROVED", 0),
             ("Run a short test only.", "SHORT_TEST_ONLY", 0),
             ("Interesting, hmm.", "UNKNOWN_REVIEW_REQUIRED", 1)]
    for text, label, code in cases:
        d = tmp_path / "dec.md"
        d.write_text(text, encoding="utf-8")
        rc, out = _run(["record-chatgpt-decision", "--config", _cfg_arg(tmp_path),
                        "--file", str(d)], tmp_path, _runner(tmp_path))
        assert label in out and rc == code


# --------------------------------------------------------------------------- #
# Phase 5B: explicit decision-token classification (the exact reported failure)
# --------------------------------------------------------------------------- #
# The exact decision file from the reported PowerShell failure.
_EXACT_LONG_DECISION = (
    "# ChatGPT Decision\n"
    "LONG_RUN_APPROVED\n"
    "Safe to continue with long paper training.\n"
    "Do not enable live trading.\n"
    "Do not loosen execution gates.\n"
    "Do not change paper realism.\n"
    "Do not use real money.\n"
    "Approved command:\n"
    "python scripts/laptop_agent_coordinator.py start-paper-run "
    "--config .laptop_agent.json --mode long --approved-by-chatgpt\n")


def _record(tmp_path, text):
    d = tmp_path / "chatgpt_decision_test.md"
    d.write_text(text, encoding="utf-8")
    return _run(["record-chatgpt-decision", "--config", _cfg_arg(tmp_path),
                 "--file", str(d)], tmp_path, _runner(tmp_path))


def test_explicit_long_run_approved_with_command_classifies(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path, _EXACT_LONG_DECISION)
    assert "classification    : LONG_RUN_APPROVED" in out
    assert rc == 0
    # only classifies + recommends; never starts a run
    assert "--mode long --approved-by-chatgpt" in out
    assert co.classify_chatgpt_decision_detail(_EXACT_LONG_DECISION)["source"] == "explicit_token"


def test_explicit_long_run_approved_without_flag_is_review(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path, "# Decision\nLONG_RUN_APPROVED\nlooks good to me\n")
    assert "classification    : UNKNOWN_REVIEW_REQUIRED" in out
    assert rc == 1


def test_explicit_stop_required_classifies(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path, "# Decision\nSTOP_REQUIRED\nfound a problem\n")
    assert "classification    : STOP_REQUIRED" in out
    assert rc == 3


def test_explicit_cursor_prompt_required_classifies(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path, "# Decision\nCURSOR_PROMPT_REQUIRED\nplease fix this code\n")
    assert "classification    : CURSOR_PROMPT_REQUIRED" in out
    assert rc == 0
    assert "prepare-cursor-handoff" in out          # supports later prompt extraction


def test_explicit_short_test_only_classifies(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path, "# Decision\nSHORT_TEST_ONLY\n")
    assert "classification    : SHORT_TEST_ONLY" in out
    assert rc == 0


def test_conflicting_explicit_tokens_are_review(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path,
                      "LONG_RUN_APPROVED\n--mode long --approved-by-chatgpt\nSTOP_REQUIRED\n")
    assert "classification    : UNKNOWN_REVIEW_REQUIRED" in out
    assert "CONFLICT" in out
    assert rc == 1


def test_no_token_is_review(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _record(tmp_path, "# Decision\nSome neutral commentary with no decision token.\n")
    assert "classification    : UNKNOWN_REVIEW_REQUIRED" in out
    assert rc == 1


def test_explicit_token_takes_priority_over_fuzzy(tmp_path):
    # fuzzy text says "short test" but the explicit token (with safety language) wins.
    _write_cfg(tmp_path)
    text = ("LONG_RUN_APPROVED\nIgnore the short test wording below.\n"
            "Safe to continue with long paper training; do not enable live trading.\n")
    rc, out = _record(tmp_path, text)
    assert "classification    : LONG_RUN_APPROVED" in out
    assert rc == 0


def test_prepare_cursor_handoff_writes_prompt_without_executing(tmp_path):
    _write_cfg(tmp_path)
    d = tmp_path / "dec.md"
    d.write_text("Fix.\n```\nStep 1\nStep 2\n```\n", encoding="utf-8")
    started = []
    rc, out = _run(["prepare-cursor-handoff", "--config", _cfg_arg(tmp_path), "--file", str(d)],
                   tmp_path, _runner(tmp_path, started=started))
    assert rc == 0 and not started
    saved = list((tmp_path / co.CURSOR_HANDOFF_DIR).glob("cursor_prompt_*.md"))
    assert saved and saved[0].read_text() == "Step 1\nStep 2"


def test_sync_main_refuses_dirty_tree(tmp_path):
    _write_cfg(tmp_path)
    rc, out = _run(["sync-main", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path, dirty=" M f.py\n"))
    assert rc == 3 and "uncommitted changes" in out and "Refusing to pull" in out


def test_start_paper_run_long_requires_approval(tmp_path):
    _write_cfg(tmp_path)
    started = []
    rc, out = _run(["start-paper-run", "--config", _cfg_arg(tmp_path), "--mode", "long"],
                   tmp_path, _runner(tmp_path, started=started))
    assert rc == 3 and "requires explicit ChatGPT approval" in out and not started


def test_status_prints_useful_next_command(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    rc, out = _run(["status", "--config", _cfg_arg(tmp_path)], tmp_path, _runner(tmp_path))
    assert rc == 0 and "NEXT SUGGESTED" in out
    assert "laptop_agent_coordinator.py" in out


def test_artifact_index_appends_cycle_records(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    _run(["operator-cycle", "--config", _cfg_arg(tmp_path)], tmp_path, _runner(tmp_path))
    ledger = tmp_path / "artifacts" / co.LEDGER_NAME
    assert ledger.is_file()
    rec = json.loads(ledger.read_text().splitlines()[-1])
    assert rec["event"] == "operator-cycle" and "report_zip" in rec


# --------------------------------------------------------------------------- #
# post-cursor-verify must NOT pass on zero collected tests (no weakening)
# --------------------------------------------------------------------------- #
def test_post_cursor_verify_fails_on_no_tests_collected(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)

    def runner(argv, cwd=None, timeout=None):
        base = _runner(tmp_path)
        if "-m" in argv and "pytest" in argv:
            return (5, "", "no tests ran in 0.00s")     # pytest exit 5 = none collected
        return base(argv, cwd=cwd, timeout=timeout)
    rc, out = _run(["post-cursor-verify", "--config", _cfg_arg(tmp_path)], tmp_path, runner)
    assert rc == 1                                          # NOT safe
    assert "NO TESTS COLLECTED" in out
    assert "NOT SAFE TO COLLECT" in out


def test_post_cursor_verify_safe_when_tests_pass(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    rc, out = _run(["post-cursor-verify", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path))
    assert rc == 0 and "SAFE TO COLLECT" in out


# --------------------------------------------------------------------------- #
# laptop operator UX: collect-report alias + one-command operator-cycle
# --------------------------------------------------------------------------- #
def test_collect_report_alias_works(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    rc, out = _run(["collect-report", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path))
    assert rc == 0
    assert "collect light-mode report" in out.lower()
    assert list((tmp_path / "artifacts").glob("hermes_light_report_*.zip"))   # zip pulled


def test_collect_light_report_still_works(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    rc, out = _run(["collect-light-report", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path))
    assert rc == 0
    assert list((tmp_path / "artifacts").glob("hermes_light_report_*.zip"))


def test_operator_cycle_runs_safe_sequence(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    rc, out = _run(["operator-cycle", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path))
    assert rc == 0
    # the safe mechanical steps appear, in order, without internal command names required
    for marker in ("verify local repo path + config", "sync GitHub main",
                   "verify VPS access + Docker", "verify VPS commit + paper/live safety",
                   "collect / generate the VPS light report", "ChatGPT upload handoff"):
        assert marker in out, marker
    # final status block fields
    for marker in ("OPERATOR CYCLE — FINAL STATUS", "RESULT", "local commit", "VPS commit",
                   "paper training", "report zip (local)", "UPLOAD TO CHATGPT",
                   "Cursor needed"):
        assert marker in out, marker


def test_operator_cycle_does_not_start_run_without_approval(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    started = []
    rc, out = _run(["operator-cycle", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path, started=started))
    assert rc == 0
    assert not started                                       # NO run started


def test_operator_cycle_starts_approved_paper_run(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    started = []
    rc, out = _run(["operator-cycle", "--config", _cfg_arg(tmp_path),
                    "--approved-paper-run", "--mode", "short"], tmp_path,
                   _runner(tmp_path, started=started))
    assert rc == 0
    assert started                                          # approved -> run started
    assert any("docker compose up" in s for s in started)
    assert "approved paper run  : STARTED" in out


def test_operator_cycle_refuses_live_flags(tmp_path, monkeypatch):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")           # a live flag is on
    started = []
    rc, out = _run(["operator-cycle", "--config", _cfg_arg(tmp_path),
                    "--approved-paper-run", "--mode", "long"], tmp_path,
                   _runner(tmp_path, started=started))
    assert rc == 2
    assert "STOP" in out and "MICRO_LIVE_ENABLED" in out
    assert not started                                      # never starts under a live flag


def test_operator_cycle_prints_report_path_and_upload_instruction(tmp_path):
    _plugin(tmp_path)
    _write_cfg(tmp_path)
    rc, out = _run(["operator-cycle", "--config", _cfg_arg(tmp_path)], tmp_path,
                   _runner(tmp_path))
    assert rc == 0
    assert "report zip (local)" in out
    assert "hermes_light_report_" in out                    # the actual local zip path
    assert "UPLOAD TO CHATGPT" in out
    assert (tmp_path / "artifacts" / co.UPLOAD_INSTRUCTIONS).is_file()


def test_local_repo_root_hermes_agent_supported(tmp_path):
    # config-driven repo_root is honored verbatim; no old path is hardcoded anywhere.
    load = co.load_config(_seed_repo_root_cfg(tmp_path, r"C:\hermes-agent\x\plugins\hte"))
    assert load.found
    assert load.cfg.repo_root == r"C:\hermes-agent\x\plugins\hte"
    example = (Path(__file__).resolve().parents[1] / ".laptop_agent.example.json").read_text()
    assert "hermes-agent-cursor" not in example
    assert r"C:\\hermes-agent\\" in example


def _seed_repo_root_cfg(tmp_path, repo_root: str) -> Path:
    p = tmp_path / co.DEFAULT_CONFIG
    p.write_text(json.dumps({
        "repo_root": repo_root, "plugin_path": repo_root, "vps_host": "h", "vps_user": "u",
        "vps_remote_plugin_path": "/opt/hermes", "local_artifact_dir": "artifacts"}),
        encoding="utf-8")
    return p
    # it targets the PLUGIN's own tests dir explicitly (robust discovery)
    assert "pytest" in out and "tests" in out
