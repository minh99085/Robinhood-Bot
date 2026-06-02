"""Phase 12 (freeze/audit) — network-isolation guard.

Proves the core unit paths (RiskEngine, micro-live locks/conformance, guarded-live
conformance, post-canary analysis, production-review) perform NO outbound network
connection. A socket-connect guard raises on any real connect attempt; if any unit
path tries to reach the network, the test fails.
"""

from __future__ import annotations

import copy
import json
import socket
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _NetworkAttempted(AssertionError):
    pass


@contextmanager
def _no_network():
    """Block real outbound socket connects for the duration of the block."""
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    attempts = []

    def _blocked(self, address, *a, **k):
        # allow loopback/unix only; everything else is a forbidden outbound call
        host = address[0] if isinstance(address, (tuple, list)) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(host)
            raise _NetworkAttempted(f"unexpected outbound connect to {host}")
        return orig_connect(self, address, *a, **k)

    def _blocked_ex(self, address, *a, **k):
        host = address[0] if isinstance(address, (tuple, list)) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(host)
            raise _NetworkAttempted(f"unexpected outbound connect_ex to {host}")
        return orig_connect_ex(self, address, *a, **k)

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked_ex
    try:
        yield attempts
    finally:
        socket.socket.connect = orig_connect
        socket.socket.connect_ex = orig_connect_ex


def test_guard_itself_trips_on_connect():
    with _no_network():
        with pytest.raises(_NetworkAttempted):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(("93.184.216.34", 80))  # example.com — must be blocked
            finally:
                s.close()


def test_risk_engine_no_network():
    from engine.risk import RiskContext, RiskEngine, RiskLimits
    from engine.schemas import TradeProposal
    with _no_network():
        eng = RiskEngine(RiskLimits(max_order_notional_abs=1.0))
        d = eng.evaluate(TradeProposal(strategy="t", market="polymarket", symbol="X", side="BUY",
                                       notional=0.5, price=0.5, edge_after_costs=0.2,
                                       mode="paper"), RiskContext(equity=100.0))
        assert d.code in ("OK",) or not d.approved  # deterministic, offline


def test_micro_live_locks_and_conformance_no_network():
    from engine.micro_live import MicroLiveConfig, all_pass, check_locks
    from engine.micro_live.conformance import MicroLiveConformanceHarness
    with _no_network():
        assert not all_pass(check_locks(MicroLiveConfig()))  # disabled by default
        assert MicroLiveConformanceHarness(MicroLiveConfig()).run()["status"] == "PASS"


def test_guarded_live_conformance_no_network():
    from engine.guarded_live import ConformanceHarness, GuardedLiveConfig
    with _no_network():
        assert ConformanceHarness(store=None, config=GuardedLiveConfig()).run().status in (
            "PASS", "FAIL")


def test_production_review_conformance_no_network():
    from engine.production_review import ProductionReviewConfig
    from engine.production_review import production_conformance as pc
    with _no_network():
        r = pc.run(ProductionReviewConfig.from_env())
        assert r.mock_only is True and r.real_network_calls == 0 and r.status == "PASS"


def test_post_canary_analysis_no_network():
    from engine.post_canary import PostCanaryConfig, analyze_context
    ctx = json.loads((_ROOT / "tests" / "fixtures" / "sample_clean_demo_canary.json").read_text())
    with _no_network():
        res = analyze_context(PostCanaryConfig.from_env(), copy.deepcopy(ctx))
        assert res.recommendation == "REPEAT_DEMO_CANARY_SAME_SIZE"


def test_production_review_run_no_network():
    from engine.production_review import ProductionReviewConfig, run_review
    ctx = json.loads(
        (_ROOT / "tests" / "fixtures" / "sample_production_review_ready_dossier.json").read_text())
    with _no_network():
        res = run_review(None, ProductionReviewConfig.from_env(), fixture=copy.deepcopy(ctx),
                         write_report=False)
        assert res.recommendation == "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"
        assert res.eligible_for_production_execution is False
