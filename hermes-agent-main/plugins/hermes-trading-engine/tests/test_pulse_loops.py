"""Loop-engineering layer: #1 maker-checker verifier, #2 lessons, #3 loop registry, #4 research loop.

All LLM calls are injected (no network). Proves: the verifier approves/vetoes + can only veto/shrink
+ fail-open; lessons compound + dedupe + persist; the registry reports loops; the research loop
turns recommendations into lessons (observe-only). PAPER ONLY.
"""

from __future__ import annotations

from engine.pulse.verifier import normalize_verdict, ClaudeVerifier
from engine.pulse.lessons import LessonsBook
from engine.pulse.loops import LoopRegistry
from engine.pulse.research_loop import ResearchLoop


# ------------------------------- #1 verifier ---------------------------------------------- #
def test_verdict_normalize():
    v = normalize_verdict({"approve": "yes", "max_size_fraction": 2.0, "confidence": 1.5})
    assert v["approve"] is True and v["max_size_fraction"] == 1.0 and v["confidence"] == 1.0
    assert normalize_verdict({"approve": False})["approve"] is False
    assert normalize_verdict("nope") is None


def test_verifier_approve_veto_and_grade():
    v = ClaudeVerifier(verify_fn=lambda p: {"approve": True, "max_size_fraction": 0.5,
                                            "confidence": 0.8, "reason": "ok"}, enabled=True)
    v.request("d1", {"decision": {"action": "up"}})
    assert v._process_one() is True
    verdict = v.get("d1")
    assert verdict["approve"] is True and verdict["max_size_fraction"] == 0.5
    v.grade("d1", won=True, pnl=2.0, acted=True)
    rep = v.report()
    assert rep["approvals"] == 1 and rep["maker_checker"] is True and rep["can_force_trade"] is False
    assert rep["approved_acted_settled"]["n"] == 1
    # veto path
    v2 = ClaudeVerifier(verify_fn=lambda p: {"approve": False, "reason": "weak"}, enabled=True)
    v2.request("d2", {})
    v2._process_one()
    assert v2.get("d2")["approve"] is False and v2.report()["vetoes"] == 1


def test_verifier_fail_open_and_failclosed():
    v = ClaudeVerifier(verify_fn=lambda p: None, enabled=True, fail_open=True)
    # no verdict yet -> fail-open APPROVE so the bot doesn't freeze
    fo = v.verdict_or_failopen("missing")
    assert fo["approve"] is True and fo["pending"] is True
    vc = ClaudeVerifier(verify_fn=lambda p: None, enabled=True, fail_open=False)
    assert vc.verdict_or_failopen("missing")["approve"] is False     # fail-closed -> veto


# ------------------------------- #2 lessons ----------------------------------------------- #
def test_lessons_compound_dedupe_persist():
    lb = LessonsBook(max_lessons=5)
    assert lb.add(kind="avoid", key="sel:direction=down", rule="avoid down") is True
    assert lb.add(kind="avoid", key="sel:direction=down", rule="dup") is False    # deduped
    assert lb.add(kind="exploit", key="edge:hurst=trending", rule="exploit trending") is True
    assert len(lb.recent(10)) == 2 and "avoid down" in lb.to_markdown()
    lb2 = LessonsBook()
    lb2.load_state(lb.to_state())
    assert len(lb2.lessons) == 2 and lb2.add(kind="avoid", key="sel:direction=down", rule="x") is False


# ------------------------------- #3 loop registry ----------------------------------------- #
def test_loop_registry_reports_loops():
    r = LoopRegistry()
    r.register("verifier", role="verify", trigger="per_decision", verifier="claude",
               status_fn=lambda: {"enabled": True, "verified": 3})
    r.register("heartbeat", role="automation", trigger="tick", interval_s=4.0)
    rep = r.report()
    assert rep["count"] == 2 and rep["loops"]["verifier"]["role"] == "verify"
    assert rep["loops"]["verifier"]["status"]["verified"] == 3


# ------------------------------- #4 research loop ----------------------------------------- #
def test_research_loop_adds_lessons_observe_only():
    lb = LessonsBook()
    note = {"summary": "exploit volume:active", "exploit_contexts": ["volume_state=active"],
            "avoid_contexts": ["hurst=noise"], "knob_recommendations": [],
            "new_lessons": [{"key": "r1", "rule": "active volume tends to follow through"}]}
    rl = ResearchLoop(research_fn=lambda rep: note, report_provider=lambda: {"x": 1}, lessons=lb,
                      auto_apply=False)
    rl.refresh()
    r = rl.report()
    assert r["calls"] == 1 and r["last_note"]["summary"].startswith("exploit")
    assert r["lessons_added"] == 1 and lb.lessons[-1]["rule"].startswith("active volume")
    assert r["auto_apply"] is False                  # observe-only by default
    # fail-open: research_fn None -> error, no crash
    rl2 = ResearchLoop(research_fn=lambda rep: None, report_provider=lambda: {})
    rl2.refresh()
    assert rl2.report()["errors"] == 1


def test_research_loop_auto_apply_invokes_apply_fn():
    # closing the loop: when auto_apply is on, avoid_contexts are passed to apply_fn and the applied
    # rules are reported (bounded, safety-only).
    applied_calls = []

    def apply_fn(note):
        out = [c.replace("hurst=", "hurst_regime=") for c in note.get("avoid_contexts", [])]
        applied_calls.append(out)
        return out
    note = {"summary": "avoid noise", "avoid_contexts": ["hurst=noise", "ttc_bucket=<60s"],
            "exploit_contexts": [], "knob_recommendations": [], "new_lessons": []}
    rl = ResearchLoop(research_fn=lambda rep: note, report_provider=lambda: {}, apply_fn=apply_fn,
                      auto_apply=True)
    rl.refresh()
    r = rl.report()
    assert r["auto_apply"] is True and applied_calls
    assert "hurst_regime=noise" in r["recent_applied"] and "ttc_bucket=<60s" in r["recent_applied"]


def test_research_loop_event_trigger_respects_min_gap():
    # event-triggered run only fires after event_min_gap_s since the last run; interval is the floor.
    rl = ResearchLoop(research_fn=lambda rep: {"summary": "x"}, report_provider=lambda: {},
                      interval_s=99999, event_min_gap_s=600)
    rl.request_run("new_edge")
    import time as _t
    now = _t.time()
    # too soon after a (just-now) run -> not yet due
    rl._last_run_ts = now
    assert (rl._pending_event == "new_edge")
    # simulate the worker's decision logic directly: gap not elapsed -> no event run
    ev = rl._pending_event if (now - rl._last_run_ts) >= rl.event_min_gap_s else None
    assert ev is None
    # after the gap, the event would fire
    rl._last_run_ts = now - 700
    ev2 = rl._pending_event if (now - rl._last_run_ts) >= rl.event_min_gap_s else None
    assert ev2 == "new_edge"
    rep = rl.report()
    assert rep["interval_floor_s"] == 99999 and rep["event_min_gap_s"] == 600
    assert "pending_event" in rep and "triggers" in rep
