"""Tests for the laptop coordinator CLI (scripts/laptop_agent_coordinator.py).

All subprocess calls go through an INJECTED runner, so these tests never touch the
network, SSH, Docker, or the VPS. They prove: config loading + required-field
validation, exact ssh/scp command construction + the remote light-report workflow,
secret redaction, artifact-directory creation, and the handoff-summary checklist.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import laptop_agent_coordinator as co  # noqa: E402

_FIXED = _dt.datetime(2026, 6, 11, 14, 30, 0)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class SpyRunner:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        for marker, resp in self._responses.items():
            if marker in joined:
                return resp
        return (0, "", "")

    def ran(self, token) -> bool:
        return any(token in " ".join(str(a) for a in c) for c in self.calls)


def collector():
    lines = []
    return lines, lines.append


def _cfg_dict(tmp_path, **over):
    d = {"repo_root": str(tmp_path), "plugin_path": str(tmp_path),
         "vps_host": "SECRET-HOST-1.2.3.4", "vps_user": "ubuntu", "vps_port": 2222,
         "vps_ssh_key": str(tmp_path / "id_ed25519_secret"),
         "vps_remote_plugin_path": "/opt/hermes/plugins/hermes-trading-engine",
         "local_artifact_dir": "artifacts"}
    d.update(over)
    return d


def write_cfg(tmp_path, **over) -> Path:
    d = _cfg_dict(tmp_path, **over)
    # make the DEFAULT private-key path a real file so validate_ssh_key passes; tests
    # that override vps_ssh_key (public-key text / missing path) intentionally do not.
    if "vps_ssh_key" not in over:
        Path(d["vps_ssh_key"]).write_text("-private-key-body-", encoding="utf-8")
    p = tmp_path / co.DEFAULT_CONFIG
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def _plugin(tmp_path):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "generate_bot_inspection_report.py").write_text("#", encoding="utf-8")


def _which(*avail):
    s = set(avail)
    return lambda n: (f"/usr/bin/{n}" if n in s else None)


# The canonical complete light bundle members (what the VPS bundler produces). A zip
# missing any of these markers is THIN and must be refused.
def _write_full_bundle(z: Path) -> None:
    z.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inspection_reports/bot_inspection_x/report.json", "{}")
        zf.writestr("inspection_reports/bot_inspection_x/report.md", "# report")
        zf.writestr("inspection_reports/bot_inspection_x/metrics/run_ready.json", "{}")
        zf.writestr("inspection_reports/bot_inspection_x/samples/events_tail.jsonl", "{}\n")
        zf.writestr("validation_light_latest.txt", "SAFE TO RUN: True")
        zf.writestr("git_commit_proof.txt", "HEAD: deadbeef")
        zf.writestr("runtime_data/metrics/bregman_funnel.json", "{}")
        zf.writestr("runtime_data/inspection_summary.json", "{}")


def _scp_bundle_runner(tmp_path, name=None):
    """A runner that records calls and, on scp, writes a COMPLETE canonical bundle to the
    local artifact dir (so collect's thin-zip guard passes)."""
    art = tmp_path / "artifacts"
    spy = SpyRunner()
    base_call = spy.__call__

    def runner(argv, cwd=None, timeout=None):
        rc, out, err = base_call(argv, cwd=cwd, timeout=timeout)
        if argv and argv[0] == "scp":
            _write_full_bundle(art / (name or co.CANONICAL_REPORT_ZIP))
        return rc, out, err
    runner.spy = spy           # type: ignore[attr-defined]
    return runner


def _run(args, tmp_path, runner=None, which=None, lines_sink=None):
    lines, printer = collector()
    if lines_sink is not None:
        lines = lines_sink
        printer = lines_sink.append
    rc = co.main(args, runner=runner or SpyRunner(), printer=printer,
                 which_fn=which or _which("git", "ssh", "scp"), now_fn=lambda: _FIXED)
    return rc, lines


# --------------------------------------------------------------------------- #
# Config loading + validation
# --------------------------------------------------------------------------- #
def test_config_loads_and_reports_set_without_secret_values(tmp_path):
    write_cfg(tmp_path)
    load = co.load_config(tmp_path / co.DEFAULT_CONFIG)
    assert load.found and load.status == co.LOAD_OK and not load.cfg.missing_required()
    summ = load.cfg.public_summary()
    assert summ["vps_host"] == "set"             # secret -> only set/unset, not the value
    assert summ["vps_user"] == "set"
    assert "SECRET-HOST-1.2.3.4" not in json.dumps(summ)
    assert summ["vps_remote_plugin_path"] == "/opt/hermes/plugins/hermes-trading-engine"


def test_missing_config_reports_missing(tmp_path):
    load = co.load_config(tmp_path / "nope.json")
    assert load.status == co.LOAD_MISSING and load.found is False
    assert isinstance(load.cfg, co.Config)


def test_missing_required_fields_detected(tmp_path):
    write_cfg(tmp_path, vps_remote_plugin_path="")
    load = co.load_config(tmp_path / co.DEFAULT_CONFIG)
    assert "vps_remote_plugin_path" in load.cfg.missing_required()


# --------------------------------------------------------------------------- #
# Config error-state handling (BOM / parse error / key validation)
# --------------------------------------------------------------------------- #
def test_config_with_utf8_bom_loads(tmp_path):
    p = tmp_path / co.DEFAULT_CONFIG
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(_cfg_dict(tmp_path)).encode("utf-8"))
    load = co.load_config(p)
    assert load.status == co.LOAD_OK and load.found      # BOM no longer breaks loading
    assert load.cfg.repo_root == str(tmp_path)


def test_malformed_json_reports_parse_error_not_missing(tmp_path):
    p = tmp_path / co.DEFAULT_CONFIG
    p.write_text("{ not valid json", encoding="utf-8")
    load = co.load_config(p)
    assert load.status == co.LOAD_PARSE_ERROR        # NOT 'missing'
    assert "parse error" in load.detail.lower()
    assert co.DEFAULT_CONFIG in co.load_message(load) or load.path in co.load_message(load)


def test_public_key_text_in_ssh_key_fails_clearly(tmp_path):
    write_cfg(tmp_path, vps_ssh_key="ssh-ed25519 AAAAC3NzaC1lZDI1 user@host")
    load = co.load_config(tmp_path / co.DEFAULT_CONFIG)
    ok, msg = co.validate_ssh_key(load.cfg)
    assert ok is False and "PUBLIC key" in msg and "PRIVATE key" in msg


def test_missing_private_key_path_fails_clearly(tmp_path):
    write_cfg(tmp_path, vps_ssh_key=str(tmp_path / "does_not_exist_key"))
    load = co.load_config(tmp_path / co.DEFAULT_CONFIG)
    ok, msg = co.validate_ssh_key(load.cfg)
    assert ok is False and "does not point to an existing file" in msg


def test_valid_config_and_real_key_passes(tmp_path):
    keyf = tmp_path / "id_ed25519"
    keyf.write_text("-private-key-body-", encoding="utf-8")
    write_cfg(tmp_path, vps_ssh_key=str(keyf))
    load = co.load_config(tmp_path / co.DEFAULT_CONFIG)
    assert load.found
    ok, _ = co.validate_ssh_key(load.cfg)
    assert ok is True


def test_doctor_reports_parse_error_not_missing(tmp_path):
    _plugin(tmp_path)
    p = tmp_path / co.DEFAULT_CONFIG
    p.write_bytes(b"\xef\xbb\xbf{ broken")             # BOM + broken JSON
    rc, lines = _run(["doctor", "--config", str(p)], tmp_path)
    out = "\n".join(lines)
    assert rc == 2
    assert "config parsed             : NO" in out
    assert "could not be parsed" in out and "missing" not in out.split("could not be parsed")[0].lower()


def test_doctor_fails_on_public_key(tmp_path):
    _plugin(tmp_path)
    keyf = tmp_path / "id_ed25519"
    keyf.write_text("x", encoding="utf-8")
    write_cfg(tmp_path, vps_ssh_key="ssh-ed25519 AAAA user@host")
    rc, lines = _run(["doctor", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=SpyRunner({"rev-parse": (0, "main\n", "")}))
    out = "\n".join(lines)
    assert rc == 1 and "[FAIL] vps_ssh_key valid" in out


# --------------------------------------------------------------------------- #
# init-config
# --------------------------------------------------------------------------- #
def test_init_config_writes_bom_free_secret_free(tmp_path):
    target = tmp_path / co.DEFAULT_CONFIG
    rc, lines = _run(["init-config", "--config", str(target)], tmp_path)
    assert rc == 0 and target.is_file()
    raw = target.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf"                # NO BOM
    data = json.loads(raw.decode("utf-8"))
    assert data["vps_host"] == "" and data["vps_ssh_key"] == ""   # no secrets seeded
    # the written file round-trips cleanly through the loader
    assert co.load_config(target).status == co.LOAD_OK


def test_init_config_refuses_overwrite_without_force(tmp_path):
    target = tmp_path / co.DEFAULT_CONFIG
    target.write_text("{}", encoding="utf-8")
    rc, _ = _run(["init-config", "--config", str(target)], tmp_path)
    assert rc == 1
    rc2, _ = _run(["init-config", "--config", str(target), "--force"], tmp_path)
    assert rc2 == 0


# --------------------------------------------------------------------------- #
# Command construction (exact)
# --------------------------------------------------------------------------- #
def test_ssh_and_scp_command_construction(tmp_path):
    cfg = co.load_config(write_cfg(tmp_path)).cfg
    ssh = co.build_ssh_cmd(cfg, "echo hi")
    assert ssh[0] == "ssh" and ssh[-2] == "ubuntu@SECRET-HOST-1.2.3.4" and ssh[-1] == "echo hi"
    assert "-i" in ssh and cfg.vps_ssh_key in ssh and "2222" in ssh
    scp = co.build_scp_pull_cmd(cfg, "/remote/x.zip", "artifacts")
    assert scp[0] == "scp" and "-P" in scp           # scp uses uppercase -P
    assert scp[-2] == "ubuntu@SECRET-HOST-1.2.3.4:/remote/x.zip"
    assert scp[-1].endswith("artifacts/")


def test_remote_collect_script_uses_canonical_runner(tmp_path):
    cfg = co.load_config(write_cfg(tmp_path)).cfg
    s = co.build_remote_collect_script(cfg)
    assert "cd /opt/hermes/plugins/hermes-trading-engine" in s
    # canonical: runs the self-bootstrapping VPS bundler, not a custom inline workflow
    assert "bash scripts/vps_generate_light_report.sh" in s
    assert "docker cp" not in s            # old inline workflow is gone
    assert "zip -r" not in s


def test_remote_python_selection_prefers_venv_then_requires_pydantic():
    s = co.remote_python_select(on_fail_exit=True)
    # venv candidates are tried BEFORE bare python3/python
    assert s.index(".report_venv/bin/python") < s.index(" python3 ")
    assert s.index(".venv/bin/python") < s.index(" python3 ")
    assert s.index(" python3 ") < s.index(" python;") or "python3 python" in co.REMOTE_PY_CANDIDATES
    # every candidate must import pydantic to qualify
    assert 'import pydantic' in s


def test_remote_collect_script_delegates_to_canonical_runner():
    # The canonical VPS runner (vps_generate_light_report.sh) owns dependency-capable
    # Python selection + bundling; the coordinator just invokes it (no inline pip).
    cfg = co.Config(vps_remote_plugin_path="/opt/p")
    s = co.build_remote_collect_script(cfg)
    assert "bash scripts/vps_generate_light_report.sh" in s
    assert "pip install" not in s.lower()               # never auto-installs


def test_remote_python_probe_checks_pydantic_and_does_not_exit():
    cfg = co.Config(vps_host="h", vps_user="u", vps_remote_plugin_path="/opt/p")
    probe = co.build_remote_python_probe(cfg)
    cmd = probe[-1]
    assert probe[0] == "ssh"
    assert "import pydantic" in cmd
    assert "NO_DEP_PYTHON" in cmd and "exit 12" not in cmd   # probe never exits non-zero
    assert ".report_venv/bin/python" in cmd                  # venv preferred


def _smoke_runner(*, py_out="remote python: /opt/p/.report_venv/bin/python\n",
                  path_rc=0, path_out="hermes-coordinator-ok\n", path_err=""):
    # ORDER matters: the path check ("cd ... && echo MARKER") must match BEFORE the
    # bare SSH-connection echo ("echo MARKER").
    return SpyRunner({"&& echo hermes-coordinator-ok": (path_rc, path_out, path_err),
                      "echo hermes-coordinator-ok": (0, "hermes-coordinator-ok\n", ""),
                      "import pydantic": (0, py_out, ""),
                      "docker version": (0, "27.0\n", ""),
                      "docker inspect": (0, "running\n", "")})


def test_vps_smoke_reports_dependency_capable_python(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    rc, lines = _run(["vps-smoke", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=_smoke_runner())
    out = "\n".join(lines)
    assert rc == 0
    assert "[PASS] remote Python can import pydantic" in out
    assert "/opt/p/.report_venv/bin/python" in out
    assert "[PASS] remote plugin path exists" in out


def test_vps_smoke_fails_when_no_dependency_capable_python(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    rc, lines = _run(["vps-smoke", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=_smoke_runner(py_out="NO_DEP_PYTHON\n"))
    out = "\n".join(lines)
    assert rc == 1 and "[FAIL] remote Python can import pydantic" in out
    assert "vps_generate_light_report.sh" in out          # exact fix shown


def test_vps_smoke_plugin_path_uses_cd_and_shows_detail_on_failure(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    # path-check failure surfaces the remote stderr detail (no false-negative parsing)
    runner = _smoke_runner(path_rc=1, path_out="",
                           path_err="bash: cd: /nope: No such file or directory\n")
    rc, lines = _run(["vps-smoke", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    out = "\n".join(lines)
    assert "[FAIL] remote plugin path exists" in out
    assert "No such file or directory" in out             # explicit detail included


def test_remote_zip_name_is_canonical():
    assert co.remote_zip_name(_FIXED) == co.CANONICAL_REPORT_ZIP == "vps_light_report_latest.zip"


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #
def test_redaction_masks_host_user_key(tmp_path):
    cfg = co.load_config(write_cfg(tmp_path)).cfg
    shown = co.redact(co.build_ssh_cmd(cfg, "echo hi"), cfg)
    assert "SECRET-HOST-1.2.3.4" not in shown
    assert cfg.vps_ssh_key not in shown
    assert "ubuntu@SECRET-HOST" not in shown
    assert "<redacted>" in shown


def test_vps_smoke_output_has_no_secrets(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    runner = SpyRunner({"echo hermes-coordinator-ok": (0, "hermes-coordinator-ok\n", ""),
                        "docker version": (0, "27.0\n", ""),
                        "import pydantic": (0, "remote python: /usr/bin/python3\n", ""),
                        "docker inspect": (0, "running\n", "")})
    rc, lines = _run(["vps-smoke", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    out = "\n".join(lines)
    assert rc == 0
    assert "SECRET-HOST-1.2.3.4" not in out and "SECRET_KEY" not in out
    assert "SAFE TO CONTINUE" in out


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def test_doctor_passes_and_creates_artifact_dir(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    runner = SpyRunner({"rev-parse": (0, "main\n", "")})
    rc, lines = _run(["doctor", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    out = "\n".join(lines)
    assert rc == 0 and "SAFE TO CONTINUE" in out
    assert (tmp_path / "artifacts").is_dir()         # artifact dir created
    assert "SECRET-HOST-1.2.3.4" not in out


def test_doctor_missing_config_shows_setup(tmp_path):
    rc, lines = _run(["doctor", "--config", str(tmp_path / "nope.json")], tmp_path)
    assert rc == 2
    assert co.EXAMPLE_CONFIG in "\n".join(lines)


def test_doctor_stops_on_missing_tools(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    rc, lines = _run(["doctor", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, which=_which("git"))       # no ssh/scp
    out = "\n".join(lines)
    assert rc == 1 and "STOP" in out
    assert "[FAIL] ssh available" in out


# --------------------------------------------------------------------------- #
# collect-light-report
# --------------------------------------------------------------------------- #
def test_collect_dry_run_does_not_execute(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    spy = SpyRunner()
    rc, lines = _run(["collect-light-report", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--dry-run"], tmp_path, runner=spy)
    out = "\n".join(lines)
    assert rc == 0 and "DRY-RUN" in out
    assert not spy.ran("docker cp")                  # nothing executed
    assert not (tmp_path / "artifacts").exists() or not any((tmp_path / "artifacts").iterdir())


def test_collect_execute_runs_canonical_script_then_scp(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    runner = _scp_bundle_runner(tmp_path)
    rc, lines = _run(["collect-light-report", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    spy = runner.spy
    assert rc == 0
    # the canonical VPS bundler ran over SSH, then the scp pull ran
    assert spy.ran("bash scripts/vps_generate_light_report.sh")
    assert any(c[0] == "scp" for c in spy.calls)
    assert (tmp_path / "artifacts").is_dir()
    out = "\n".join(lines)
    assert "vps_light_report_latest.zip" in out
    assert "COLLECTED (complete bundle)" in out
    assert "SECRET-HOST-1.2.3.4" not in out


def test_collect_refuses_thin_zip(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    art = tmp_path / "artifacts"

    def thin_runner(argv, cwd=None, timeout=None):
        if argv and argv[0] == "scp":
            art.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(art / co.CANONICAL_REPORT_ZIP, "w") as zf:
                zf.writestr("inspection_reports/report.json", "{}")   # thin: 1 file only
            return (0, "", "")
        return (0, "", "")
    rc, lines = _run(["collect-report", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=thin_runner)
    out = "\n".join(lines)
    assert rc == 1
    assert "THIN/incomplete" in out


def test_collect_missing_field_stops(tmp_path):
    write_cfg(tmp_path, vps_remote_plugin_path="")
    rc, lines = _run(["collect-light-report", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path)
    assert rc == 2 and "STOP" in "\n".join(lines)


# --------------------------------------------------------------------------- #
# handoff-summary
# --------------------------------------------------------------------------- #
def _make_zip(tmp_path, *, complete=True):
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    z = art / co.CANONICAL_REPORT_ZIP
    if complete:
        _write_full_bundle(z)
    else:
        with zipfile.ZipFile(z, "w") as zf:               # thin: missing markers
            zf.writestr("inspection_reports/report.json", "{}")
    return z


def test_handoff_summary_complete_zip(tmp_path):
    write_cfg(tmp_path)
    _make_zip(tmp_path, complete=True)
    rc, lines = _run(["handoff-summary", "--config", str(tmp_path / co.DEFAULT_CONFIG)], tmp_path)
    out = "\n".join(lines)
    assert rc == 0
    assert "vps_light_report_latest.zip" in out
    assert "validation file       : included" in out
    assert "git_commit_proof      : included" in out
    assert "COMPLETE BUNDLE" in out


def test_handoff_summary_refuses_thin_zip(tmp_path):
    write_cfg(tmp_path)
    _make_zip(tmp_path, complete=False)
    rc, lines = _run(["handoff-summary", "--config", str(tmp_path / co.DEFAULT_CONFIG)], tmp_path)
    out = "\n".join(lines)
    assert rc == 1                                   # thin/incomplete handoff refused
    assert "THIN/incomplete" in out


def test_handoff_summary_no_zip_guides_operator(tmp_path):
    write_cfg(tmp_path)
    (tmp_path / "artifacts").mkdir()
    rc, lines = _run(["handoff-summary", "--config", str(tmp_path / co.DEFAULT_CONFIG)], tmp_path)
    assert rc == 2
    assert "collect-light-report" in "\n".join(lines)


def test_help_lists_all_commands():
    p = co.build_parser()
    for name in ("doctor", "vps-smoke", "collect-light-report", "handoff-summary",
                 "operator-cycle", "record-chatgpt-decision", "prepare-cursor-handoff",
                 "sync-main", "post-cursor-verify", "start-paper-run", "status"):
        assert name in co.COMMANDS
    ns = p.parse_args(["collect-light-report", "--dry-run"])
    assert ns.command == "collect-light-report" and ns.dry_run is True


# --------------------------------------------------------------------------- #
# Phase 5: autonomous operator loop + approval gates
# --------------------------------------------------------------------------- #
import zipfile as _zf  # noqa: E402


def _cycle_runner(tmp_path, *, dirty="", branch="main", started=None):
    """Runner that satisfies doctor + vps-smoke + collect; records docker-compose runs;
    creates the pulled zip on scp."""
    art = tmp_path / "artifacts"

    def runner(argv, cwd=None, timeout=None):
        s = " ".join(str(a) for a in argv)
        if "docker compose up" in s or "docker compose down" in s:
            if started is not None:
                started.append(s)
            return (0, "started\n", "")
        if argv and argv[0] == "git":
            if "rev-parse" in argv:
                return (0, (branch + "\n" if "--abbrev-ref" in argv else "abc123commit\n"), "")
            if "status" in argv:
                return (0, dirty, "")
            if "fetch" in argv or "pull" in argv:
                return (0, "", "")
            return (0, "", "")
        if argv and argv[0] == "scp":
            _write_full_bundle(art / co.CANONICAL_REPORT_ZIP)    # canonical complete bundle
            return (0, "", "")
        if "&& echo hermes-coordinator-ok" in s or "echo hermes-coordinator-ok" in s:
            return (0, "hermes-coordinator-ok\n", "")
        if "import pydantic" in s:
            return (0, "remote python: /usr/bin/python3\n", "")
        if "docker version" in s:
            return (0, "27.0\n", "")
        if "docker inspect" in s:
            return (0, "running\n", "")
        if "-m" in argv and "pytest" in argv:
            return (0, "10 passed", "")
        if "generate_bot_inspection_report" in s or "validate_training_runtime" in s:
            return (0, "report ok", "")
        return (0, "", "")
    return runner


def test_operator_cycle_collects_and_stops_for_handoff(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    started = []
    rc, lines = _run(["operator-cycle", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=_cycle_runner(tmp_path, started=started))
    out = "\n".join(lines)
    assert rc == 0
    assert "SAFE TO CONTINUE" in out
    assert "UPLOAD TO CHATGPT" in out                                # exact upload instruction
    assert (tmp_path / "artifacts" / co.UPLOAD_INSTRUCTIONS).is_file()      # handoff written
    assert not started                                               # never started a run
    # ledger appended with the cycle record
    ledger = (tmp_path / "artifacts" / co.LEDGER_NAME).read_text().splitlines()
    rec = json.loads(ledger[-1])
    assert rec["event"] == "operator-cycle" and rec["report_zip"].endswith(".zip")
    assert rec["validation_present"] and rec["inspection_summary_present"]


def test_record_decision_classifies_and_never_executes(tmp_path):
    write_cfg(tmp_path)
    d = tmp_path / "decision.md"
    d.write_text("Please STOP — the bot is unsafe to run right now.", encoding="utf-8")
    spy = SpyRunner({"rev-parse": (0, "c1\n", "")})
    rc, lines = _run(["record-chatgpt-decision", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--file", str(d)], tmp_path, runner=spy)
    out = "\n".join(lines)
    assert rc == 3 and "STOP_REQUIRED" in out                        # conservative stop
    assert not spy.ran("ssh") and not spy.ran("docker")              # no risky execution
    assert list((tmp_path / "artifacts").glob("chatgpt_decision_*.md"))    # saved copy
    rec = json.loads((tmp_path / "artifacts" / co.LEDGER_NAME).read_text().splitlines()[-1])
    assert rec["decision_classification"] == "STOP_REQUIRED"


def test_record_decision_long_run_and_cursor(tmp_path):
    write_cfg(tmp_path)
    for text, label, code in [("Long run approved — go ahead.", "LONG_RUN_APPROVED", 0),
                              ("This needs a code fix; paste into Cursor.",
                               "CURSOR_PROMPT_REQUIRED", 0),
                              ("Run a short test only first.", "SHORT_TEST_ONLY", 0),
                              ("Hmm, interesting results.", "UNKNOWN_REVIEW_REQUIRED", 1)]:
        d = tmp_path / "dec.md"
        d.write_text(text, encoding="utf-8")
        rc, lines = _run(["record-chatgpt-decision", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                          "--file", str(d)], tmp_path, runner=SpyRunner({"rev-parse": (0, "c\n", "")}))
        assert label in "\n".join(lines) and rc == code


def test_prepare_cursor_handoff_writes_prompt_without_executing(tmp_path):
    write_cfg(tmp_path)
    d = tmp_path / "decision.md"
    d.write_text("Fix it.\n```\nDo X\nThen Y\n```\nthanks", encoding="utf-8")
    spy = SpyRunner({"rev-parse": (0, "c\n", "")})
    rc, lines = _run(["prepare-cursor-handoff", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--file", str(d)], tmp_path, runner=spy)
    out = "\n".join(lines)
    assert rc == 0
    saved = list((tmp_path / co.CURSOR_HANDOFF_DIR).glob("cursor_prompt_*.md"))
    assert saved and saved[0].read_text() == "Do X\nThen Y"          # extracted, not executed
    assert "push to GitHub `main`" in out
    assert not spy.ran("ssh")


def test_sync_main_refuses_dirty_tree(tmp_path):
    write_cfg(tmp_path)
    runner = _cycle_runner(tmp_path, dirty=" M somefile.py\n")
    rc, lines = _run(["sync-main", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    out = "\n".join(lines)
    assert rc == 3 and "uncommitted changes" in out
    assert "Refusing to pull" in out


def test_sync_main_refuses_non_main_branch(tmp_path):
    write_cfg(tmp_path)
    runner = _cycle_runner(tmp_path, branch="feature-x")
    rc, lines = _run(["sync-main", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    assert rc == 2 and "not 'main'" in "\n".join(lines)


def test_sync_main_fast_forwards_clean_main(tmp_path):
    write_cfg(tmp_path)
    rc, lines = _run(["sync-main", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=_cycle_runner(tmp_path))
    out = "\n".join(lines)
    assert rc == 0 and "before :" in out and "after  :" in out


def test_start_paper_run_long_requires_approval(tmp_path):
    write_cfg(tmp_path)
    started = []
    rc, lines = _run(["start-paper-run", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--mode", "long"], tmp_path, runner=_cycle_runner(tmp_path, started=started))
    out = "\n".join(lines)
    assert rc == 3 and "requires explicit ChatGPT approval" in out
    assert not started                                               # nothing started


def _mission_capable_runner(tmp_path, *, started=None, dirty="", branch="main"):
    """A cycle runner that ALSO satisfies the mission-control safe path that
    start-paper-run now routes through: clean VPS git status + 100X env present."""
    art = tmp_path / "artifacts"
    base = _cycle_runner(tmp_path, dirty=dirty, branch=branch, started=started)

    def runner(argv, cwd=None, timeout=None):
        s = " ".join(str(a) for a in argv)
        if argv and argv[0] == "ssh" and "git status --porcelain" in s:
            return (0, "", "")                                   # VPS clean (no dirty tree)
        if "Config.Env" in s:                                    # 100X env proof
            return (0, "\n".join(f"{k}={v}" for k, v in co.REQUIRED_100X_ENV.items()) + "\n", "")
        return base(argv, cwd=cwd, timeout=timeout)
    return runner


def test_start_paper_run_long_with_approval_routes_through_mission_control(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    started = []
    rc, lines = _run(["start-paper-run", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--mode", "long", "--approved-by-chatgpt"],
                     tmp_path, runner=_mission_capable_runner(tmp_path, started=started))
    out = "\n".join(lines)
    assert rc == 0
    # routed through mission-control's unified safe rebuild (BOTH services), not a
    # divergent "up -d --build hermes-training" legacy path.
    assert started and any("docker compose down --remove-orphans" in s for s in started)
    assert any("docker compose build --no-cache" in s for s in started)
    assert any("docker compose up -d" in s for s in started)
    assert not any("up -d --build hermes-training" in s for s in started)
    assert "routing through mission-control" in out
    assert "MISSION-CONTROL — FINAL STATUS" in out
    assert "trading run started : YES" in out


def test_start_paper_run_short_is_default_and_dry_run_safe(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    started = []
    rc, lines = _run(["start-paper-run", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--dry-run"], tmp_path,
                     runner=_mission_capable_runner(tmp_path, started=started))
    assert rc == 0 and not started                                   # dry-run executes nothing
    assert "DRY-RUN" in "\n".join(lines)


def test_status_prints_sections_and_next_command(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    rc, lines = _run(["status", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=_cycle_runner(tmp_path))
    out = "\n".join(lines)
    assert rc == 0
    for label in ("local branch", "local commit", "config parsed", "latest report zip",
                  "VPS SSH", "hermes-training", "remote Python deps", "NEXT SUGGESTED"):
        assert label in out
    assert "mission-control" in out                                  # recommends mission-control


def test_post_cursor_verify_runs_chain(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    rc, lines = _run(["post-cursor-verify", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=_cycle_runner(tmp_path))
    out = "\n".join(lines)
    assert "sync-main" in out and "local tests" in out
    assert "SAFE TO COLLECT" in out and rc == 0


# --------------------------------------------------------------------------- #
# sync-vps : git-bundle fallback when the passphrase deploy key blocks a pull
# --------------------------------------------------------------------------- #
_LOCAL_HEAD = "a" * 40
_VPS_OLD_HEAD = "b" * 40


def test_sync_vps_dry_run_executes_nothing(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    calls = []

    def runner(argv, cwd=None, timeout=None):
        calls.append(" ".join(str(a) for a in argv))
        return (0, "", "")
    rc, lines = _run(["sync-vps", "--config", str(tmp_path / co.DEFAULT_CONFIG), "--dry-run"],
                     tmp_path, runner=runner)
    out = "\n".join(lines)
    assert rc == 0 and "DRY-RUN" in out
    assert not any(c.startswith("ssh") or c.startswith("scp") for c in calls)
    assert not any("git push" in c for c in calls)


def test_sync_vps_uses_git_bundle_fallback(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    calls = []

    def runner(argv, cwd=None, timeout=None):
        s = " ".join(str(a) for a in argv)
        calls.append(s)
        if argv and argv[0] == "ssh":
            if "git pull --ff-only origin main" in s:
                return (255, "", "git@github.com: Permission denied (publickey).")
            if "git fetch /tmp/vps_sync_main.bundle main" in s:
                return (0, _LOCAL_HEAD + "\n", "")        # VPS now at local main
            if "git rev-parse HEAD" in s:
                return (0, _VPS_OLD_HEAD + "\n", "")        # VPS behind
            return (0, "", "")
        if argv and argv[0] == "scp":
            return (0, "", "")
        if argv and argv[0] == "git":
            if "rev-parse" in argv:
                return (0, _LOCAL_HEAD + "\n", "")
            if "merge-base" in argv:
                return (0, "", "")                          # VPS head is an ancestor
            if "bundle" in argv or "push" in argv:
                return (0, "", "")
            return (0, "", "")
        return (0, "", "")
    rc, lines = _run(["sync-vps", "--config", str(tmp_path / co.DEFAULT_CONFIG)],
                     tmp_path, runner=runner)
    out = "\n".join(lines)
    assert rc == 0
    assert "git-bundle fallback" in out and "via bundle" in out
    assert any("git bundle create" in c for c in calls)
    assert any(c.startswith("scp") and "vps_sync_main.bundle" in c for c in calls)
    assert any("git fetch /tmp/vps_sync_main.bundle main" in c for c in calls)


# --------------------------------------------------------------------------- #
# collect-full-report : full bundle -> extract into vps_full_reports/latest -> commit
# --------------------------------------------------------------------------- #
def test_collect_full_report_dry_run(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    rc, lines = _run(["collect-full-report", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--save-to-repo", "--commit", "--dry-run"], tmp_path)
    out = "\n".join(lines)
    assert rc == 0 and "DRY-RUN" in out
    assert co.FULL_REPORT_SCRIPT in out
    assert "vps_full_reports/latest" in out


def test_collect_full_report_saves_and_commits(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    art = tmp_path / "artifacts"
    calls = []

    def runner(argv, cwd=None, timeout=None):
        s = " ".join(str(a) for a in argv)
        calls.append(s)
        if argv and argv[0] == "scp":
            _write_full_bundle(art / co.FULL_REPORT_ZIP)   # pulled full report zip
            return (0, "", "")
        if "save_full_report_to_repo.py" in s:
            return (0, "saved 31 files -> vps_full_reports/latest\n", "")
        if argv and argv[0] == "git":
            if "rev-parse" in argv:
                return (0, "deadbeefdeadbeef\n", "")
            return (0, "", "")               # add / commit / push all succeed
        return (0, "", "")
    rc, lines = _run(["collect-full-report", "--config", str(tmp_path / co.DEFAULT_CONFIG),
                      "--commit"], tmp_path, runner=runner)
    out = "\n".join(lines)
    assert rc == 0
    assert "COLLECTED full report" in out
    assert "extracted into vps_full_reports/latest" in out
    assert "committed + pushed" in out
    assert any("save_full_report_to_repo.py" in c for c in calls)
    assert any(c.startswith("git") and "commit" in c for c in calls)
    assert any("git push origin main" in c for c in calls)


def test_auto_deploy_dry_run_inspect_only_never_rebuilds(tmp_path):
    _plugin(tmp_path)
    write_cfg(tmp_path)
    started = []
    rc, lines = _run(["auto-deploy", "--config", str(tmp_path / co.DEFAULT_CONFIG), "--dry-run"],
                     tmp_path, runner=_cycle_runner(tmp_path, started=started))
    out = "\n".join(lines)
    assert rc == 0
    assert "AUTO-DEPLOY" in out
    assert "rebuild skipped (inspect-only" in out
    assert not started                                   # no docker compose rebuild in dry/inspect
    assert "AUTO-DEPLOY complete" in out
