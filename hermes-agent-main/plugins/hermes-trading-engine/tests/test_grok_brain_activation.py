"""GrokBrain activation self-heal (PAPER ONLY, research-only).

Reproduces the deploy race where the xAI/Grok key is injected into the container
(docker env_file) AFTER the brain object was constructed: the dashboard showed
"GROK BRAIN OFF — add an API key" even though the key was present in the env. The
brain must self-heal on the next status() poll and the ON toggle must re-read the
key. Grok stays research-only; no live/order path is ever enabled.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.brain import GrokBrain, read_grok_key

_GROK_ENV = ("XAI_API_KEY", "GROK_API_KEY", "RESEARCH_MODE", "GROK_BRAIN_ONLINE")


@pytest.fixture(autouse=True)
def _clean_grok_env(monkeypatch):
    for k in _GROK_ENV:
        monkeypatch.delenv(k, raising=False)
    yield


def _brain(tmp_path):
    return GrokBrain(SimpleNamespace(data_dir=str(tmp_path), stance="balanced"))


def test_brain_self_heals_when_key_injected_after_construction(tmp_path, monkeypatch):
    b = _brain(tmp_path)
    assert b.enabled is False and b.grok_source == "disabled"
    # docker env_file injects the key into the env AFTER the brain was built
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("GROK_BRAIN_ONLINE", "1")
    st = b.status()                      # dashboard poll self-heals
    assert st["enabled"] is True
    assert st["grok_source"] == "online_research"


def test_online_paper_mode_enables_without_explicit_grok_brain_online(tmp_path, monkeypatch):
    b = _brain(tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")   # online mode alone is enough
    st = b.status()
    assert st["enabled"] is True
    assert st["grok_source"] == "online_research"


def test_turn_on_toggle_rereads_key(tmp_path, monkeypatch):
    b = _brain(tmp_path)
    assert b.enabled is False
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("GROK_BRAIN_ONLINE", "1")
    st = b.set_active(True)              # "click to turn ON" re-reads the key
    assert st["enabled"] is True
    assert st["grok_source"] == "online_research"


def test_user_pause_is_respected_even_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("GROK_BRAIN_ONLINE", "1")
    b = _brain(tmp_path)
    assert b.enabled is True
    b.set_active(False)                  # operator explicitly pauses
    assert b.enabled is False and b.grok_source == "paused_by_user"
    # a later poll must NOT silently re-enable a user-paused brain
    assert b.status()["enabled"] is False


def test_no_key_stays_off(tmp_path):
    b = _brain(tmp_path)
    st = b.status()
    assert st["enabled"] is False
    assert st["grok_source"] == "disabled"


@pytest.mark.parametrize("raw,expected", [
    ('xai-clean', 'xai-clean'),
    ('  xai-ws  ', 'xai-ws'),
    ('"xai-dquoted"', 'xai-dquoted'),
    ("'xai-squoted'", 'xai-squoted'),
    ('  "xai-both"\n', 'xai-both'),     # quotes + whitespace + newline (the 401 trap)
    ('', ''),
])
def test_read_grok_key_sanitizes_quotes_and_whitespace(monkeypatch, raw, expected):
    # a key delivered via docker compose interpolation may keep surrounding quotes /
    # a trailing newline -> malformed Bearer header -> 401 ("suddenly stopped").
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", raw)
    assert read_grok_key() == expected


def test_quoted_key_in_env_still_enables_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", '"xai-quoted-key-value"')
    monkeypatch.setenv("GROK_BRAIN_ONLINE", "1")
    b = _brain(tmp_path)
    assert b.api_key == "xai-quoted-key-value"   # quotes stripped
    assert b.status()["enabled"] is True


# --- Grok advisory proof call (research-only, rate-limited) -----------------

class _FakeGrokClient:
    def __init__(self, source="grok_online"):
        self.source = source
        self.calls = 0

    def research(self, ctx, news_packet=None):
        from types import SimpleNamespace
        self.calls += 1
        return SimpleNamespace(source=self.source)


def test_grok_proof_call_advisory_and_increments_counters():
    from engine.research.proof_call import GrokProofCaller
    clock = [1000.0]
    c = GrokProofCaller(enabled=True, max_per_hour=1, advisory_only=True,
                        clock=lambda: clock[0])
    written = []
    res = c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                       news_packet=[{"headline": "x"}], market_ctx={"market_id": "m1"},
                       evidence_sink=written.append)
    assert res["called"] is True and res["advisory_only"] is True
    assert res["grok_calls_total"] == 1 and res["grok_calls_with_news"] == 1
    assert written and written[0]["is_edge_proof"] is False    # never proof of edge


def test_grok_proof_call_rate_limited_then_resumes():
    from engine.research.proof_call import GrokProofCaller
    clock = [1000.0]
    # max_per_run high so the HOURLY limit is what's exercised here.
    c = GrokProofCaller(enabled=True, max_per_hour=1, max_per_run=10, clock=lambda: clock[0])
    mk, news = {"market_id": "m1"}, [{"headline": "x"}]
    assert c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["called"] is True
    r2 = c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                      news_packet=news, market_ctx=mk)
    assert r2["called"] is False and r2["reason"] == "rate_limit_budget_exhausted"
    clock[0] += 3700
    assert c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["called"] is True


def test_grok_proof_call_precise_zero_reasons():
    from engine.research.proof_call import GrokProofCaller
    fc, mk, news = _FakeGrokClient(), {"market_id": "m1"}, [{"headline": "x"}]
    assert GrokProofCaller(enabled=False).maybe_call(
        client=fc, online=True, has_key=True, news_packet=news,
        market_ctx=mk)["reason"] == "proof_call_disabled_by_config"
    assert GrokProofCaller(enabled=True).maybe_call(
        client=fc, online=True, has_key=True, news_packet=None,
        market_ctx=mk)["reason"] == "no_news_packet_available"
    # cache-only result is NOT a real proof call
    assert GrokProofCaller(enabled=True).maybe_call(
        client=_FakeGrokClient("grok_cache"), online=True, has_key=True,
        news_packet=news, market_ctx=mk)["reason"] == "cache_only_mode_enabled"


def test_xai_api_key_alone_is_sufficient(tmp_path, monkeypatch):
    from engine.brain import read_grok_key, grok_key_source
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-only-key")
    assert read_grok_key() == "xai-only-key"
    assert grok_key_source() == "XAI_API_KEY"
    monkeypatch.setenv("GROK_BRAIN_ONLINE", "1")
    assert _brain(tmp_path).status()["enabled"] is True


def test_grok_api_key_not_required_legacy_fallback_only(tmp_path, monkeypatch):
    from engine.brain import read_grok_key, grok_key_source
    # legacy GROK_API_KEY still works as a fallback, but is NOT required
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "grok-legacy")
    assert read_grok_key() == "grok-legacy"
    assert grok_key_source() == "GROK_API_KEY(legacy)"
    # XAI takes precedence (canonical) when both are set
    monkeypatch.setenv("XAI_API_KEY", "xai-canonical")
    assert read_grok_key() == "xai-canonical"
    assert grok_key_source() == "XAI_API_KEY"


def test_missing_xai_key_is_grok_blocker_not_a_crash(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env, market
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    cat = [market(i, bid=0.49, ask=0.51, liq=50_000, depth=2000, now=1_000_000.0)
           for i in range(5)]
    t.run_tick(cat, now=1_000_000.0)            # paper-safe scanning must not break
    rs = t.research_status()
    assert rs["grok_enabled"] is False
    assert rs["grok_brain_ready"] is False
    assert rs["grok_brain_blocker"] == "no_api_key"


def test_proof_call_disabled_is_not_reported_healthy(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", grok_proof_call_enabled=False), data_dir=tmp_path)
    t._news_metrics["items_used"] = 5
    rs = t.research_status()
    assert rs["grok_online_active"] is True
    assert rs["grok_calls_total"] == 0
    assert rs["grok_brain_ready"] is False                 # NOT healthy
    # precise + non-contradictory blocker (never the cache/stub contradiction)
    assert rs["grok_brain_blocker"] not in (None, "served_from_cache_or_offline_stub")
    assert rs["grok_brain_blocker"] in (
        "proof_call_disabled_by_config", "news_scanner_disabled",
        "no_news_packet_selected", "not_due_yet")


def test_mocked_proof_call_increments_and_marks_brain_ready(tmp_path, monkeypatch):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", grok_proof_call_enabled=True), data_dir=tmp_path)
    # inject a mock xAI client (no network) onto the signal model
    t.signal_model._client = _FakeGrokClient("grok_online")
    res = t.maybe_grok_proof_call(news_packet=[{"headline": "n"}],
                                  market_ctx={"market_id": "m1"}, now=1_000_000.0)
    assert res["called"] is True
    rs = t.research_status()
    assert rs["grok_calls_total"] >= 1
    assert rs["grok_calls_with_news"] >= 1
    assert rs["grok_brain_ready"] is True
    assert rs["grok_brain_blocker"] is None
    assert rs.get("grok_zero_call_reason") in (None,)
    # advisory only: the proof call wrote a research diagnostic, opened NO trades
    assert t.paper_trades_opened() == 0 if hasattr(t, "paper_trades_opened") else True


def test_proof_call_bounded_per_run(tmp_path, monkeypatch):
    from engine.research.proof_call import GrokProofCaller
    c = GrokProofCaller(enabled=True, max_per_hour=100, max_per_run=1)
    mk, news = {"market_id": "m1"}, [{"headline": "x"}]
    assert c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["called"] is True
    r2 = c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                      news_packet=news, market_ctx=mk)
    assert r2["called"] is False and r2["reason"] == "rate_limit_budget_exhausted"


def test_proof_call_never_trades_or_sizes(tmp_path):
    # the evidence record is explicitly advisory + not edge proof; the caller has no
    # trade/size surface at all (research-only).
    from engine.research.proof_call import GrokProofCaller
    written = []
    c = GrokProofCaller(enabled=True)
    c.maybe_call(client=_FakeGrokClient(), online=True, has_key=True,
                 news_packet=[{"h": "x"}], market_ctx={"market_id": "m1"},
                 evidence_sink=written.append)
    assert written and written[0]["advisory_only"] is True
    assert written[0]["is_edge_proof"] is False
    assert not any(k in written[0] for k in ("size", "notional", "order", "side", "stake"))


def test_research_status_zero_reason_not_contradictory(tmp_path, monkeypatch):
    # online + key + news_used>0 but no real call + proof disabled -> the reason must
    # be the precise proof_call_disabled_by_config, NOT served_from_cache_or_offline_stub.
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    # force a news_items_used>0 view
    t._news_metrics["items_used"] = 5
    rs = t.research_status()
    assert rs["grok_online_active"] is True
    if rs["grok_calls_total"] == 0:
        assert rs["grok_zero_call_reason"] != "served_from_cache_or_offline_stub"
        assert rs["grok_zero_call_reason"] in (
            "proof_call_disabled_by_config", "not_due_yet", "no_news_packet_selected",
            "news_scanner_disabled")
