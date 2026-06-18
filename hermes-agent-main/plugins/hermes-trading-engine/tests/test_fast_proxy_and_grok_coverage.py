"""Option 1 (fast proxy labels) + Option 2 (broader Grok coverage).

Option 1: a decision with a far-future final-settlement label ALSO gets a short-horizon
proxy label so the closed loop gets fast DISCOVERY feedback within a run; the proxy never
feeds the settlement calibration (counts_for_calibration=False). Option 2: the aggressive
profile raises the Grok advisory budget + tightens spacing so far more directional
candidates get a real Grok probability (coverage was the bottleneck).
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.training import TrainingConfig
from engine.training.closed_loop import ClosedLoopLearning

_NOW = 1_000_000.0


def _cll(tmp_path, **cfgkw):
    cfg = TrainingConfig(mode="paper_train", **cfgkw)
    return ClosedLoopLearning("run-test", tmp_path, cfg, now=_NOW)


def _rec(mid="m0", *, end_ts=None):
    return SimpleNamespace(market_id=mid, group_key=f"market:{mid}", cluster_id="sem:x",
                           category="crypto", question="Will event 0 resolve YES?",
                           top_depth_usd=2000.0, book_age_s=2.0, end_ts=end_ts,
                           raw={"conditionId": "c0"})


def _est(mid_p=0.50):
    return SimpleNamespace(p_market_mid=mid_p, spread=0.02, ambiguity_score=0.05,
                           calibrated_probability=None)


def _edge(net=0.005, px=0.51, pf=0.55):
    return SimpleNamespace(net_edge=net, executable_price=px, p_final=pf)


# ---------------- Option 1: fast proxy labels ----------------

def test_far_settlement_also_gets_fast_proxy_label(tmp_path):
    cl = _cll(tmp_path, closed_loop_fast_proxy_enabled=True, closed_loop_proxy_horizon_s=300.0)
    cl.begin_tick()
    end = _NOW + 7 * 86400          # final settlement 7 days out
    cl.record(_rec(end_ts=end), _est(), _edge(), decision="no_trade_label",
              reason="edge_too_low", tick=1, now=_NOW)
    types = sorted(p["label_type"] for p in cl.pending)
    assert types == ["final_settlement", "proxy"]


def test_fast_proxy_resolves_within_run_final_stays_pending(tmp_path):
    cl = _cll(tmp_path, closed_loop_fast_proxy_enabled=True, closed_loop_proxy_horizon_s=300.0)
    cl.begin_tick()
    end = _NOW + 7 * 86400
    cl.record(_rec(end_ts=end), _est(0.50), _edge(), decision="no_trade_label",
              reason="x", tick=1, now=_NOW)
    # resolve after the proxy horizon but long before final settlement
    n = cl.resolve_labels({"m0": 0.60}, now=_NOW + 400.0)
    assert n == 1                                  # only the proxy resolved
    assert any(p["label_type"] == "final_settlement" for p in cl.pending)  # final still pending
    proxies = [c for c in cl.completed if c["label_type"] == "proxy"]
    assert proxies and proxies[0]["counts_for_calibration"] is False  # never settlement calib
    assert proxies[0]["not_final_settlement"] is True


def test_fast_proxy_disabled_only_final(tmp_path):
    cl = _cll(tmp_path, closed_loop_fast_proxy_enabled=False)
    cl.begin_tick()
    end = _NOW + 7 * 86400
    cl.record(_rec(end_ts=end), _est(), _edge(), decision="no_trade_label",
              reason="x", tick=1, now=_NOW)
    assert len(cl.pending) == 1 and cl.pending[0]["label_type"] == "final_settlement"


def test_no_end_ts_still_single_proxy(tmp_path):
    cl = _cll(tmp_path, closed_loop_fast_proxy_enabled=True, closed_loop_proxy_horizon_s=300.0)
    cl.begin_tick()
    cl.record(_rec(end_ts=None), _est(), _edge(), decision="no_trade_label",
              reason="x", tick=1, now=_NOW)
    assert len(cl.pending) == 1 and cl.pending[0]["label_type"] == "proxy"


# ---------------- Option 2: broader Grok coverage ----------------

def test_aggressive_profile_raises_grok_budget():
    cfg = TrainingConfig.aggressive_paper()
    assert cfg.grok_advisory_max_calls_per_hour >= 30      # was 4
    assert cfg.grok_advisory_min_interval_seconds <= 120   # was 900 (15 min)
    assert cfg.grok_advisory_max_calls_per_run >= 500      # was 48
    assert cfg.grok_advisory_require_news is False          # broaden coverage


def test_base_profile_keeps_conservative_grok_budget():
    base = TrainingConfig()
    assert base.grok_advisory_max_calls_per_hour == 4
    assert base.grok_advisory_min_interval_seconds == 900
    assert base.grok_advisory_require_news is True


class _FakeRes:                       # estimate bundle (no failure status) => real call
    status = None
    source = "grok_online"


class _FakeClient:
    model = "grok-x"

    def research(self, ctx, mode=None, news_packet=None):
        return _FakeRes()


def test_require_news_false_allows_newsless_call():
    from engine.research.proof_call import GrokProofCaller
    c = GrokProofCaller(enabled=True, max_per_hour=60, max_per_run=2000,
                        min_interval_seconds=0, require_news=False)
    r = c.maybe_call(client=_FakeClient(), online=True, has_key=True,
                     news_packet=None, market_ctx={"market_id": "m1"})
    assert r["called"] is True and r["reason"] is None


def test_require_news_true_skips_newsless_call():
    from engine.research.proof_call import GrokProofCaller
    c = GrokProofCaller(enabled=True, max_per_hour=60, max_per_run=2000,
                        min_interval_seconds=0, require_news=True)
    r = c.maybe_call(client=_FakeClient(), online=True, has_key=True,
                     news_packet=None, market_ctx={"market_id": "m1"})
    assert r["called"] is False and r["reason"] == "no_news_packet_available"
