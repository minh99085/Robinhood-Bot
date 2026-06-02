"""Replay CLI: runs on the fixture without network; fails closed on no events."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = _PLUGIN_ROOT / "tests" / "fixtures" / "sample_polymarket_replay.jsonl"


def _load_cli():
    path = _PLUGIN_ROOT / "scripts" / "run_replay.py"
    spec = importlib.util.spec_from_file_location("run_replay_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_replay_cli_fixture_no_network(tmp_path, monkeypatch, capsys):
    # any network use must blow up -> proves replay is offline
    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("network call attempted during replay")

    monkeypatch.setattr(httpx, "Client", _Boom)
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))

    cli = _load_cli()
    rc = cli.main([
        "--from-jsonl", str(FIXTURE), "--policy", "noop",
        "--initial-cash", "10000", "--seed", "42",
        "--out", str(tmp_path / "artifacts"), "--db", str(tmp_path / "op.sqlite3"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Replay summary" in out
    # artifacts written
    art = list((tmp_path / "artifacts").glob("*/summary.json"))
    assert art, "summary.json should be written"


def test_run_replay_cli_fails_closed_on_no_events(tmp_path):
    cli = _load_cli()
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    rc = cli.main(["--from-jsonl", str(empty), "--policy", "noop",
                   "--db", str(tmp_path / "op.sqlite3"), "--out", str(tmp_path / "a")])
    assert rc != 0  # fail closed


def test_run_replay_cli_dry_run_config(tmp_path, capsys):
    cli = _load_cli()
    rc = cli.main(["--from-jsonl", str(FIXTURE), "--policy", "simple_edge",
                   "--dry-run-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "config_hash" in out
