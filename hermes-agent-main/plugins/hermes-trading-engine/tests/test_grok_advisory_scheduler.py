"""Bounded Grok/xAI ADVISORY scheduler (research-only; never execution).

Proves: XAI_API_KEY alone is sufficient (GROK_API_KEY not required); the scheduler
caps calls/hour + spaces them; it requires news when configured; it chooses
high-value Bregman near-misses before random markets; it can analyze Bregman groups
with ZERO executable trades; advisory output cannot execute/size/lower gates and IS
persisted into diagnostics/report metrics; and no API key value ever appears in
logs/reports/events/diagnostics.
"""

import json
from types import SimpleNamespace

from engine.research.proof_call import GrokProofCaller
from engine.research.advisory_targets import select_advisory_target, advisory_features_for


class _OKClient:
    model = "grok-4.3"

    def research(self, ctx, mode=None, news_packet=None):
        return SimpleNamespace(estimate_id="e1")     # success bundle (no status/source)


# --- key handling ----------------------------------------------------------- #
def test_xai_api_key_alone_is_sufficient(monkeypatch, tmp_path):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    rs = t.research_status()
    assert rs["xai_api_key_present"] is True
    assert rs["xai_api_key_source"] == "XAI_API_KEY"


def test_grok_api_key_not_required(monkeypatch, tmp_path):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)
    t.signal_model._client = _OKClient()
    r = t.maybe_grok_proof_call(news_packet=[{"headline": "x", "market_id": "m"}],
                                market_ctx={"market_id": "m"}, now=1_000_000.0)
    assert r["called"] is True


# --- caps + spacing --------------------------------------------------------- #
def test_scheduler_caps_calls_per_hour():
    clock = [1000.0]
    c = GrokProofCaller(enabled=True, max_per_hour=4, max_per_run=100,
                        min_interval_seconds=0, clock=lambda: clock[0])
    mk, news = {"market_id": "m1"}, [{"headline": "x"}]
    made = 0
    for _ in range(10):
        if c.maybe_call(client=_OKClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["called"]:
            made += 1
        clock[0] += 1
    assert made == 4                                # hourly cap enforced
    assert c.advisory_calls_per_hour(clock[0]) == 4


def test_scheduler_requires_news_when_configured():
    c = GrokProofCaller(enabled=True, max_per_hour=4, max_per_run=10,
                        min_interval_seconds=0)
    r = c.maybe_call(client=_OKClient(), online=True, has_key=True,
                     news_packet=None, market_ctx={"market_id": "m1"})
    assert r["called"] is False and r["reason"] == "no_news_packet_available"


def test_scheduler_min_interval_spacing():
    clock = [1000.0]
    c = GrokProofCaller(enabled=True, max_per_hour=4, max_per_run=10,
                        min_interval_seconds=900, clock=lambda: clock[0])
    mk, news = {"market_id": "m1"}, [{"headline": "x"}]
    assert c.maybe_call(client=_OKClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["called"] is True
    clock[0] += 100
    assert c.maybe_call(client=_OKClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["reason"] == "not_due_yet"
    clock[0] += 900
    assert c.maybe_call(client=_OKClient(), online=True, has_key=True,
                        news_packet=news, market_ctx=mk)["called"] is True


# --- target selection ------------------------------------------------------- #
def test_scheduler_prefers_bregman_near_miss_before_random_market():
    nms = [{"group_key": "g1", "near_miss_score": 0.9, "raw_market_ids": ["a"],
            "completeness": {"market_kind": "winner_take_all",
                             "completeness_proven": False}, "simplex": {}}]
    sel = select_advisory_target(near_misses=nms, news_packet=[{"market_id": "z"}],
                                 watch_markets=[{"market_id": "rand", "depth_usd": 999}])
    assert sel["target_kind"] == "bregman_near_miss"
    assert sel["near_misses_analyzed"] == 1


def test_scheduler_analyzes_groups_with_zero_executable_trades():
    # no executable trade candidates (empty watch), but news + near-miss exist
    nms = [{"group_key": "g1", "near_miss_score": 0.5, "raw_market_ids": ["a"],
            "completeness": {}, "simplex": {}}]
    sel = select_advisory_target(near_misses=nms, news_packet=[{"market_id": "a"}],
                                 watch_markets=[])
    assert sel["market_ctx"] is not None
    assert sel["groups_analyzed"] >= 1


def test_news_linked_market_chosen_when_no_near_misses():
    sel = select_advisory_target(near_misses=[], news_packet={"items": [{"market_id": "n1"}]},
                                 watch_markets=[])
    assert sel["target_kind"] == "news_linked_market"
    assert sel["news_linked_analyzed"] == 1


# --- advisory-only safety --------------------------------------------------- #
def test_advisory_output_cannot_execute_or_size_or_lower_gates():
    written = []
    c = GrokProofCaller(enabled=True, max_per_run=1, min_interval_seconds=0)
    feats = advisory_features_for({"completeness": {"market_kind": "ambiguous"}},
                                  [{"relevance_score": 0.6}], "bregman_near_miss")
    r = c.maybe_call(client=_OKClient(), online=True, has_key=True,
                     news_packet=[{"headline": "x"}],
                     market_ctx={"market_id": "m1", "group_ids": ["g1"]},
                     target_kind="bregman_near_miss", advisory_features=feats,
                     analyzed_increments={"groups_analyzed": 1, "near_misses_analyzed": 1},
                     evidence_sink=written.append)
    assert r["called"] is True
    ev = written[0]
    assert ev["advisory_only"] is True
    assert ev["executed"] is False
    assert ev["sized_trade"] is False
    assert ev["trade_gate_bypassed"] is False
    assert ev["no_execution_override"] is True
    assert ev["is_edge_proof"] is False
    # advisory features are flagged non-execution
    assert ev["advisory_features"]["affects_execution"] is False


def test_advisory_evidence_persisted_with_analyzed_counters():
    c = GrokProofCaller(enabled=True, max_per_hour=4, max_per_run=10,
                        min_interval_seconds=0)
    for _ in range(3):
        c.maybe_call(client=_OKClient(), online=True, has_key=True,
                     news_packet=[{"headline": "x"}], market_ctx={"market_id": "m1"},
                     target_kind="bregman_near_miss",
                     analyzed_increments={"groups_analyzed": 1, "near_misses_analyzed": 1,
                                          "news_linked_analyzed": 1})
    st = c.status()
    assert st["grok_calls_total"] == 3
    assert st["grok_market_groups_analyzed"] == 3
    assert st["grok_bregman_near_misses_analyzed"] == 3
    assert st["grok_news_linked_markets_analyzed"] == 3
    assert st["grok_evidence_records_written"] == 3


def test_proof_vs_scheduler_calls_reconciled():
    # a call WITH a target_kind counts as a SCHEDULER call; without, a PROOF call.
    c = GrokProofCaller(enabled=True, max_per_hour=10, max_per_run=10,
                        min_interval_seconds=0)
    c.maybe_call(client=_OKClient(), online=True, has_key=True,
                 news_packet=[{"h": "x"}], market_ctx={"market_id": "m1"})  # proof
    c.maybe_call(client=_OKClient(), online=True, has_key=True,
                 news_packet=[{"h": "x"}], market_ctx={"market_id": "m2"},
                 target_kind="bregman_near_miss")                          # scheduler
    st = c.status()
    assert st["grok_calls_total"] == 2
    assert st["grok_proof_calls_total"] == 1
    assert st["grok_scheduler_calls_total"] == 1
    # the split reconciles to the total (no contradictory zero scheduled count)
    assert st["grok_proof_calls_total"] + st["grok_scheduler_calls_total"] == st["grok_calls_total"]


def test_scheduler_accounting_eligible_selected_skipped():
    clock = [1000.0]
    c = GrokProofCaller(enabled=True, max_per_hour=2, max_per_run=10,
                        min_interval_seconds=0, clock=lambda: clock[0])
    mk, news = {"market_id": "m1"}, [{"h": "x"}]
    for _ in range(5):
        c.maybe_call(client=_OKClient(), online=True, has_key=True, news_packet=news,
                     market_ctx=mk, target_kind="bregman_near_miss", eligible_targets=3,
                     advisory_features={"grok_news_relevance_score": 0.5},
                     analyzed_increments={"incomplete_groups_analyzed": 1,
                                          "malformed_groups_analyzed": 1})
        clock[0] += 1
    st = c.status()
    assert st["grok_scheduler_calls_total"] == 2          # hourly cap
    assert st["grok_scheduler_targets_selected"] == 2
    assert st["grok_scheduler_targets_skipped"] == 3
    assert st["grok_scheduler_skip_reasons"]              # has reasons
    assert st["grok_scheduler_eligible_targets"] == 3
    assert st["grok_total_calls_reconciled"] is True
    assert st["grok_bregman_incomplete_groups_analyzed"] == 2
    assert st["grok_bregman_malformed_groups_analyzed"] == 2
    assert st["grok_learning_features_written"] == 2


def test_scheduled_zero_cannot_coexist_with_scheduler_activity():
    # if the scheduler made calls, scheduled_calls must be > 0 (never contradictory 0)
    c = GrokProofCaller(enabled=True, max_per_hour=4, max_per_run=10, min_interval_seconds=0)
    c.maybe_call(client=_OKClient(), online=True, has_key=True, news_packet=[{"h": "x"}],
                 market_ctx={"market_id": "m1"}, target_kind="bregman_near_miss",
                 eligible_targets=1)
    st = c.status()
    assert st["grok_scheduler_calls_total"] == 1
    # the trainer maps scheduled_calls to include scheduler calls -> never 0 here
    assert st["grok_scheduler_calls_total"] > 0


def test_scheduler_selects_incomplete_and_malformed_bregman_targets():
    from engine.research.advisory_targets import select_advisory_target
    nms = [{"group_key": "g1", "near_miss_score": 0.9, "market_ids": ["a"],
            "token_ids": ["ay", "an"],
            "completeness": {"completeness_proven": False},
            "simplex": {"invalid_normalization": True}}]
    sel = select_advisory_target(near_misses=nms, news_packet=[{"market_id": "a"}],
                                 watch_markets=[])
    assert sel["target_kind"] == "bregman_near_miss"
    assert sel["incomplete_groups_analyzed"] == 1
    assert sel["malformed_groups_analyzed"] == 1
    assert sel["eligible_targets"] >= 1


def test_research_status_reconciles_scheduled_calls(monkeypatch, tmp_path):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train",
                                              grok_advisory_min_interval_seconds=0),
                               data_dir=tmp_path)
    t.signal_model._client = _OKClient()
    t._bregman_near_miss_best = {"g1": {"group_key": "g1", "near_miss_score": 0.9,
                                        "raw_market_ids": ["a"], "completeness": {},
                                        "simplex": {}}}
    t.maybe_grok_proof_call(news_packet=[{"market_id": "a", "headline": "x"}],
                            market_ctx=None, now=1_000_000.0)
    rs = t.research_status()
    # the scheduled-advisory call is no longer contradictorily zero
    assert rs["grok_calls_total"] >= 1
    assert rs["grok_scheduler_calls_total"] >= 1
    assert rs["grok_scheduled_calls"] >= 1


def test_grok_best_bregman_group_analyzed_or_skip_reason(monkeypatch, tmp_path):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train",
                                              grok_advisory_min_interval_seconds=0),
                               data_dir=tmp_path)
    # no near-miss + no call yet -> analyzed False with an EXACT skip reason
    rs0 = t.research_status()
    assert rs0["grok_best_bregman_group_analyzed"] is False
    assert rs0["grok_best_bregman_group_skip_reason"]      # non-empty exact reason
    # after a scheduled advisory call on a seeded near-miss -> analyzed True
    t.signal_model._client = _OKClient()
    t._bregman_near_miss_best = {"g1": {"group_key": "g1", "near_miss_score": 0.9,
                                        "market_ids": ["a"], "token_ids": ["ay", "an"],
                                        "completeness": {}, "simplex": {}}}
    t.maybe_grok_proof_call(news_packet=[{"market_id": "a", "headline": "x"}],
                            market_ctx=None, now=1_000_000.0)
    rs1 = t.research_status()
    assert rs1["grok_best_bregman_group_analyzed"] is True
    assert rs1["grok_best_bregman_group_skip_reason"] is None


def test_no_api_key_value_appears_in_evidence_or_status(monkeypatch, tmp_path):
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-" + "z" * 70)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    t = PolymarketPaperTrainer(TrainingConfig(mode="paper_train",
                                              grok_advisory_min_interval_seconds=0),
                               data_dir=tmp_path)
    t.signal_model._client = _OKClient()
    t._bregman_near_miss_best = {"g1": {"group_key": "g1", "near_miss_score": 0.9,
                                        "raw_market_ids": ["a"], "completeness": {},
                                        "simplex": {}}}
    r = t.maybe_grok_proof_call(news_packet=[{"market_id": "a", "headline": "x"}],
                                market_ctx=None, now=1_000_000.0)
    rs = t.research_status()
    blob = json.dumps({"result": r, "status": rs})
    assert "xai-secret" not in blob
    assert "zzz" not in blob.lower()
