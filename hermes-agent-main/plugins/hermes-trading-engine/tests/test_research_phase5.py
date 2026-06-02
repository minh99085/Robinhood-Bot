"""Phase 5 tests: controlled Grok research/probability engine.

All Grok/xAI calls are mocked. No network. No secrets required. These verify the
research-only contract: Grok may estimate probability but can never execute,
size, or bypass the RiskEngine.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from engine.research import (
    AmbiguityScorer,
    ForecastEnsemble,
    GrokResearchClient,
    MarketRuleParser,
    ProbabilityEstimator,
    ReplayResearchCache,
    ResearchBudget,
    SourceCache,
    forbidden_execution_keys,
    redact,
    validate_probability_output,
)
from engine.research.evidence_store import EvidenceStore
from engine.research.prompts import build_messages, prompt_hash
from engine.research.schemas import (
    ONLINE_MODES,
    EvidenceItem,
    GrokProbabilityOutput,
    ProbabilityEstimateBundle,
    ResearchFailure,
)
from engine.risk import RiskContext, RiskEngine, RiskLimits, ResearchSnapshot, RiskCode
from engine.schemas import TradeProposal, parse_grok_action
from engine.storage import Store

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PLUGIN_ROOT / "scripts"


def _valid_output(market_id="m1", n_evidence=2, prob=0.62, **extra) -> dict:
    ev = [{"claim": f"fact {i}", "source_type": "news", "direction": "supports_yes",
           "credibility": 0.8, "relevance": 0.8, "freshness": 0.7, "weight": 0.9,
           "source_url": f"https://example.com/a{i}"} for i in range(n_evidence)]
    out = {"market_id": market_id, "outcome": "YES", "fair_probability": prob,
           "confidence": 0.7, "evidence": ev, "ambiguity_score": 0.1,
           "source_coverage_score": 0.6, "no_trade_recommendation": False}
    out.update(extra)
    return out


def _client(store, mode="online_paper", budget=None, raw=None, usage=None, raise_exc=None):
    c = GrokResearchClient(store=store, mode=mode, budget=budget)
    if raise_exc is not None:
        def _boom(messages):
            raise raise_exc
        c._call_model = _boom
    elif raw is not None:
        c._call_model = lambda messages: (raw, usage)
    return c


def _ok_proposal(notional=10.0) -> TradeProposal:
    return TradeProposal(
        strategy="t", market="polymarket", symbol="m1", side="YES", notional=notional,
        price=0.5, edge_after_costs=0.0, spread=0.0, ambiguity_score=0.0,
        allow_duplicate=False, mode="paper", rationale="test", meta={})


def _ok_ctx() -> RiskContext:
    return RiskContext(equity=1000.0, total_exposure=0.0, market_exposure=0.0,
                       open_orders=0, day_pnl=0.0)


# 1
def test_grok_output_schema_accepts_valid_probability():
    out = validate_probability_output(_valid_output())
    assert isinstance(out, GrokProbabilityOutput)
    assert out.fair_probability == pytest.approx(0.62)
    assert len(out.evidence) == 2


# 2
def test_grok_output_schema_rejects_missing_evidence(tmp_path):
    store = Store(tmp_path / "r.db")
    c = _client(store, raw=_valid_output(n_evidence=0))
    res = c.research({"venue": "polymarket", "market_id": "m1", "outcome": "YES"})
    assert isinstance(res, ResearchFailure)
    assert res.status == "NO_EVIDENCE"


# 3
def test_grok_output_schema_rejects_out_of_range_probability():
    assert validate_probability_output(_valid_output(prob=1.5)) is None
    assert validate_probability_output(_valid_output(prob=-0.2)) is None


# 4
def test_grok_output_schema_rejects_execution_instruction(tmp_path):
    raw = _valid_output()
    raw["order_size"] = 100
    raw["place_order"] = True
    assert forbidden_execution_keys(raw) == ["order_size", "place_order"]
    store = Store(tmp_path / "r.db")
    c = _client(store, raw=raw)
    res = c.research({"venue": "polymarket", "market_id": "m1", "outcome": "YES"})
    # Output is accepted but size/execution stripped; never an order field.
    assert isinstance(res, (ProbabilityEstimateBundle, ResearchFailure))
    assert not hasattr(res, "order_size")
    n = store._conn.execute("SELECT COUNT(*) FROM research_validation_events").fetchone()[0]
    assert n >= 1


# 5
def test_research_budget_blocks_when_daily_cost_exceeded(tmp_path):
    store = Store(tmp_path / "r.db")
    budget = ResearchBudget(max_daily_cost_usd=Decimal("0"))
    c = _client(store, budget=budget, raise_exc=AssertionError("must not call network"))
    res = c.research({"venue": "polymarket", "market_id": "m1", "outcome": "YES"})
    assert isinstance(res, ResearchFailure)
    assert res.status == "BUDGET_BLOCKED"


# 6
def test_research_budget_rate_limit():
    budget = ResearchBudget(per_minute=1, clock=lambda: 1_000_000)
    ok, _ = budget.check("m1")
    assert ok
    budget.record("m1")
    ok, reason = budget.check("m1")
    assert not ok and reason == "rate_limit_per_minute"


# 7
def test_source_cache_deduplicates_url():
    cache = SourceCache()
    a = cache.add_source(url="https://www.example.com/x/", source_type="news", excerpt="a")
    b = cache.add_source(url="https://example.com/x", source_type="news", excerpt="b")
    assert a["source_id"] == b["source_id"]
    assert cache.size() == 1


# 8
def test_evidence_store_links_evidence_to_estimate(tmp_path):
    store = Store(tmp_path / "r.db")
    es = EvidenceStore(store)
    item = EvidenceItem(claim="c", source_type="news", source_url="https://e.com/1",
                        credibility=0.7, relevance=0.8, weight=0.9)
    es.persist_evidence(item, research_run_id="rr1", estimate_id="est1",
                        venue="polymarket", market_id="m1", asset_id="a1")
    rows = store.get_research_evidence(estimate_id="est1")
    assert len(rows) == 1
    assert rows[0]["research_run_id"] == "rr1" and rows[0]["estimate_id"] == "est1"


# 9
def test_market_rule_parser_extracts_ambiguity():
    meta = {"market_id": "m1", "venue": "polymarket",
            "question": "Will the price be approximately right at the discretion of admins?",
            "description": "Resolves based on a tweet and subjective judgment."}
    summary = MarketRuleParser().parse(meta)
    assert summary.ambiguity_score > 0
    assert summary.ambiguity_categories


# 10
def test_ambiguity_high_blocks_risk():
    eng = RiskEngine(RiskLimits())
    ctx = _ok_ctx()
    ctx.research = ResearchSnapshot(required=True, p_ensemble=0.6, p_market=0.5,
                                    evidence_score=0.9, source_count=5, confidence=0.9,
                                    ambiguity_score=0.9)
    d = eng.evaluate(_ok_proposal(), ctx)
    assert not d.approved and d.code == RiskCode.RESEARCH_HIGH_AMBIGUITY


# 11
def test_low_evidence_blocks_risk():
    eng = RiskEngine(RiskLimits())
    ctx = _ok_ctx()
    ctx.research = ResearchSnapshot(required=True, p_ensemble=0.6, p_market=0.5,
                                    evidence_score=0.1, source_count=5, confidence=0.9,
                                    ambiguity_score=0.0)
    d = eng.evaluate(_ok_proposal(), ctx)
    assert not d.approved and d.code == RiskCode.RESEARCH_LOW_EVIDENCE


# 12
def test_stale_research_estimate_blocks_risk():
    eng = RiskEngine(RiskLimits())
    ctx = _ok_ctx()
    ctx.research = ResearchSnapshot(required=True, p_ensemble=0.6, p_market=0.5,
                                    evidence_score=0.9, source_count=5, confidence=0.9,
                                    ambiguity_score=0.0, stale=True)
    d = eng.evaluate(_ok_proposal(), ctx)
    assert not d.approved and d.code == RiskCode.RESEARCH_STALE


# 13
def test_probability_ensemble_conservative_weighting():
    r = ForecastEnsemble().combine(p_market=0.5, p_llm=0.99, p_model=None,
                                   confidence=0.1, evidence_score=0.3, ambiguity_score=0.0)
    p = r["p_ensemble"]
    assert abs(p - 0.5) < abs(p - 0.99)  # pulled toward the market


# 14
def test_probability_ensemble_clamps_extreme_probability():
    r = ForecastEnsemble(clamp_high=0.95).combine(
        p_market=0.99, p_llm=0.99, p_model=0.99, confidence=0.5,
        evidence_score=0.5, ambiguity_score=0.0)
    assert r["p_ensemble"] <= 0.95
    assert r["clamped"] is True


# 15
def test_replay_research_cache_no_network(tmp_path, monkeypatch):
    store = Store(tmp_path / "r.db")
    store.add_probability_estimate({
        "estimate_id": "e1", "venue": "polymarket", "market_id": "m1", "asset_id": "a1",
        "outcome": "YES", "ts_ms": 100, "p_ensemble": "0.6", "stale_after_ts_ms": 10_000_000})
    monkeypatch.setattr(GrokResearchClient, "_call_model",
                        lambda self, m: (_ for _ in ()).throw(AssertionError("network!")))
    cache = ReplayResearchCache(store)
    est = cache.latest_estimate(venue="polymarket", market_id="m1", asset_id="a1", at_ts_ms=200)
    assert est is not None and est["p_ensemble"] == "0.6"
    assert cache.is_tradeable(est, 200)


# 16
def test_replay_research_cache_uses_latest_before_timestamp(tmp_path):
    store = Store(tmp_path / "r.db")
    for eid, ts, p in [("e1", 100, "0.6"), ("e2", 200, "0.7"), ("e3", 300, "0.8")]:
        store.add_probability_estimate({
            "estimate_id": eid, "venue": "polymarket", "market_id": "m1", "asset_id": "a1",
            "outcome": "YES", "ts_ms": ts, "p_ensemble": p, "stale_after_ts_ms": 10_000_000})
    cache = ReplayResearchCache(store)
    assert cache.latest_estimate(venue="polymarket", market_id="m1", asset_id="a1",
                                 at_ts_ms=150)["ts_ms"] == 100
    assert cache.latest_estimate(venue="polymarket", market_id="m1", asset_id="a1",
                                 at_ts_ms=250)["ts_ms"] == 200


# 17
def test_research_failure_does_not_create_trade_proposal():
    eng = RiskEngine(RiskLimits())
    ctx = _ok_ctx()
    # A failed research run yields an absent/invalid estimate -> RiskEngine blocks.
    ctx.research = ResearchSnapshot(required=True, present=False, p_ensemble=None)
    d = eng.evaluate(_ok_proposal(), ctx)
    assert not d.approved and d.code == RiskCode.RESEARCH_MISSING


# 18
def test_strategy_research_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RESEARCH_USE_IN_STRATEGY", raising=False)
    # No research snapshot -> research gates skipped entirely, proposal approved.
    d = RiskEngine(RiskLimits()).evaluate(_ok_proposal(), _ok_ctx())
    assert d.approved


# 19
def test_online_research_endpoint_never_places_order(tmp_path):
    store = Store(tmp_path / "r.db")
    c = _client(store, raw=_valid_output())
    res = c.research({"venue": "polymarket", "market_id": "m1", "outcome": "YES",
                      "p_market_mid": 0.5})
    assert isinstance(res, (ProbabilityEstimateBundle, ResearchFailure))
    # research must never write orders/fills
    assert store._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


# 20
def test_research_endpoint_disabled_in_offline_cache_mode(tmp_path):
    assert "offline_cache" not in ONLINE_MODES and "disabled" not in ONLINE_MODES
    store = Store(tmp_path / "r.db")
    c = _client(store, mode="offline_cache", raise_exc=AssertionError("no network offline"))
    res = c.research({"venue": "polymarket", "market_id": "m1", "outcome": "YES"})
    assert isinstance(res, ResearchFailure)
    assert res.reason == "research_mode_not_online"


# 21
def test_no_secrets_in_research_logs(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-SECRETKEY1234567890")
    out = redact("calling with Authorization: Bearer xai-SECRETKEY1234567890 now")
    assert "xai-SECRETKEY1234567890" not in out
    assert "[REDACTED]" in out


# 22
def test_prompt_hash_stable():
    ctx = {"venue": "polymarket", "market_id": "m1", "outcome": "YES", "question": "q?"}
    h1 = prompt_hash(build_messages(ctx), {"model": "grok-4.3"})
    h2 = prompt_hash(build_messages(ctx), {"model": "grok-4.3"})
    h3 = prompt_hash(build_messages({**ctx, "market_id": "m2"}), {"model": "grok-4.3"})
    assert h1 == h2 and h1 != h3


# 23
def test_research_storage_migrations_idempotent(tmp_path):
    p = tmp_path / "r.db"
    Store(p)
    s2 = Store(p)  # re-init same file: must not crash
    row = s2._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='probability_estimates'"
    ).fetchone()
    assert row is not None


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "eval_research_cal", _SCRIPTS / "evaluate_research_calibration.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# 24
def test_evaluate_research_calibration_brier():
    mod = _load_eval_module()
    assert mod.brier_score([(1.0, 1), (0.0, 0)]) == pytest.approx(0.0)
    assert mod.brier_score([(0.7, 1)]) == pytest.approx(0.09)


# 25
def test_evaluate_research_calibration_unresolved_excluded(tmp_path):
    mod = _load_eval_module()
    store = Store(tmp_path / "r.db")
    for eid, mid in [("e1", "m1"), ("e2", "m2")]:
        store.add_probability_estimate({
            "estimate_id": eid, "venue": "polymarket", "market_id": mid, "asset_id": None,
            "outcome": "YES", "ts_ms": 1, "p_ensemble": "0.6", "stale_after_ts_ms": 0})
    store.upsert_market_outcome({"venue": "polymarket", "market_id": "m1", "asset_id": None,
                                 "outcome": "YES", "realized_outcome": 1})
    res = mod.evaluate(store)
    assert res["resolved"] == 1 and res["unresolved"] == 1


# 26
def test_xai_client_timeout_returns_failure(tmp_path):
    store = Store(tmp_path / "r.db")
    c = _client(store, raise_exc=TimeoutError("timed out"))
    res = c.research({"venue": "polymarket", "market_id": "m1", "outcome": "YES"})
    assert isinstance(res, ResearchFailure)
    assert res.status == "FAILED" and res.retryable


# 27
def test_invalid_json_from_legacy_brain_forces_wait():
    assert parse_grok_action(None).action == "WAIT"
    # low-confidence BUY must be coerced to WAIT by the legacy path
    assert parse_grok_action({"action": "BUY", "confidence": 0.2}, min_confidence=0.4).action == "WAIT"


# 28
def test_research_run_once_cli_no_order_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(GrokResearchClient, "_call_model",
                        lambda self, m: (_valid_output(), None))
    spec = importlib.util.spec_from_file_location(
        "run_research_once", _SCRIPTS / "run_research_once.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    db = tmp_path / "r.db"
    rc = mod.main(["--venue", "polymarket", "--market-id", "m1", "--outcome", "YES",
                   "--mode", "online_paper", "--db", str(db)])
    assert rc == 0
    store = Store(db)
    assert store._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


# 29
def test_export_research_dataset(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "export_research_dataset", _SCRIPTS / "export_research_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    store = Store(tmp_path / "r.db")
    store.add_probability_estimate({
        "estimate_id": "e1", "venue": "polymarket", "market_id": "m1", "asset_id": None,
        "outcome": "YES", "ts_ms": 1, "p_ensemble": "0.6", "stale_after_ts_ms": 0})
    out = tmp_path / "export"
    mod.export(store, out)
    for name in ("probability_estimates.csv", "research_evidence.csv",
                 "market_rule_summaries.csv", "research_runs.csv"):
        assert (out / name).exists()


# 30
def test_compile_and_import_research_modules():
    import importlib
    for name in ("schemas", "validators", "budget", "source_cache", "ambiguity",
                 "market_rules", "ensemble", "probability", "calibration_adapter",
                 "evidence_store", "replay_cache", "prompts", "grok_client"):
        importlib.import_module(f"engine.research.{name}")
