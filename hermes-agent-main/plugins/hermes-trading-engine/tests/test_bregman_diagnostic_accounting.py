"""bregman_funnel.diagnostic_events_written must reflect ACTUAL bregman_diagnostic
rows written to data/training/diagnostics.jsonl + events.jsonl — including those the
standalone scanner writes directly to the sink (which bypass closed_loop.record).
PAPER ONLY."""

from __future__ import annotations

from engine.training.closed_loop import ClosedLoopLearning
from engine.training.config import TrainingConfig
from engine.training.inspection_summary import build_bregman_funnel


def test_scanner_bregman_diagnostics_counted_in_ledger(tmp_path):
    cl = ClosedLoopLearning("run-x", tmp_path, TrainingConfig(mode="paper_train"), now=1e6)
    cl.begin_tick()
    # the standalone scanner writes diagnostics straight to the sink (no record())
    for i in range(7):
        cl.sink.append_bregman_diagnostic({
            "group_id": f"g{i}", "skip_reason": "non_numeric_price",
            "missing_fields": ["non_numeric_price"]})
    led = cl.ledger_summary()
    assert led["bregman_diagnostics"] == 7         # reflects actual sink writes
    # diagnostics.jsonl + events.jsonl both carry the rows
    fc = cl.sink.file_line_counts()
    assert fc["diagnostics"] >= 7 and fc["events"] >= 7


def test_funnel_diagnostic_events_written_uses_actual_count():
    funnel = build_bregman_funnel(
        {"groups_discovered": 0, "constraint_groups_scanned": 0, "groups_skipped": 259,
         "skip_reasons": {"non_numeric_price": 259}},
        market_groups_detected=259, diagnostic_events_written=259)
    assert funnel["diagnostic_events_written"] == 259
    # detected>0 with adapter failures + diagnostics is internally consistent
    assert funnel["groups_adapter_failed"] == 259
    assert funnel["internally_consistent"] is True
