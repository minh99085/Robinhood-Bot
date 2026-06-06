"""Local .env loader: loads the Grok key (XAI_API_KEY) from .env/.env.env even when
saved with the wrong filename, and NEVER enables live trading. PAPER ONLY."""

from __future__ import annotations

import os

import pytest

from engine.env_loader import load_local_env, grok_key_present


@pytest.fixture(autouse=True)
def _restore_environ():
    """load_local_env writes os.environ directly (monkeypatch can't restore that),
    so snapshot + restore the full environment around every test to prevent leaks."""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def _write(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")


def test_loads_xai_key_from_dot_env(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    _write(tmp_path, ".env", "XAI_API_KEY=xai-abc123\nGROK_BRAIN_ONLINE=1\n")
    load_local_env(root=tmp_path)
    assert os.environ.get("XAI_API_KEY") == "xai-abc123"
    assert grok_key_present() is True


def test_loads_from_dot_env_dot_env_fallback(tmp_path, monkeypatch):
    # the exact ".env.env" save-as mistake the user hit
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    _write(tmp_path, ".env.env", "XAI_API_KEY=xai-fallback\n")
    load_local_env(root=tmp_path)
    assert os.environ.get("XAI_API_KEY") == "xai-fallback"


def test_overrides_empty_compose_value(tmp_path, monkeypatch):
    # docker-compose sets XAI_API_KEY="" via ${XAI_API_KEY:-}; loader must fill it
    monkeypatch.setenv("XAI_API_KEY", "")
    _write(tmp_path, ".env", "XAI_API_KEY=xai-real\n")
    load_local_env(root=tmp_path)
    assert os.environ.get("XAI_API_KEY") == "xai-real"


def test_does_not_override_real_existing_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "already-set")
    _write(tmp_path, ".env", "XAI_API_KEY=from-file\n")
    load_local_env(root=tmp_path)
    assert os.environ.get("XAI_API_KEY") == "already-set"   # existing non-empty wins


def test_live_flags_never_loaded_from_dotenv(tmp_path, monkeypatch):
    # a dotenv file can NEITHER enable live trading NOR mask an operator-set flag:
    # live flags are simply not loaded from the file (preflight still sees real env).
    for f in ("LIVE_TRADING_ENABLED", "POLYMARKET_AUTOTRADE_ENABLED", "MICRO_LIVE_ENABLED",
              "XAI_API_KEY"):
        monkeypatch.delenv(f, raising=False)
    _write(tmp_path, ".env",
           "LIVE_TRADING_ENABLED=1\nPOLYMARKET_AUTOTRADE_ENABLED=1\nMICRO_LIVE_ENABLED=true\n"
           "XAI_API_KEY=k\n")
    load_local_env(root=tmp_path)
    # the live flags were NOT loaded (so a file can't enable live)
    assert os.environ.get("LIVE_TRADING_ENABLED") is None
    assert os.environ.get("POLYMARKET_AUTOTRADE_ENABLED") is None
    assert os.environ.get("MICRO_LIVE_ENABLED") is None
    assert os.environ["XAI_API_KEY"] == "k"        # non-live vars still load


def test_does_not_mask_operator_set_live_flag(tmp_path, monkeypatch):
    # operator explicitly set a live flag in the REAL env -> loader leaves it so the
    # startup preflight can detect + refuse it (loader must not silently zero it).
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")
    _write(tmp_path, ".env", "MICRO_LIVE_ENABLED=0\nXAI_API_KEY=k\n")
    load_local_env(root=tmp_path)
    assert os.environ["MICRO_LIVE_ENABLED"] == "1"   # preserved for the preflight


def test_handles_quotes_export_and_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_MODEL", raising=False)
    _write(tmp_path, ".env",
           "# comment\nexport XAI_API_KEY=\"xai-quoted\"\nGROK_MODEL='grok-4.3'\n\n")
    load_local_env(root=tmp_path)
    assert os.environ["XAI_API_KEY"] == "xai-quoted"
    assert os.environ["GROK_MODEL"] == "grok-4.3"


def test_missing_file_is_noop(tmp_path):
    applied = load_local_env(root=tmp_path)      # empty dir
    assert applied == {}


def test_grok_key_present_false_when_unset(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    assert grok_key_present() is False
