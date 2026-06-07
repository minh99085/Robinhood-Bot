"""Canonical append-only training event sink (PAPER ONLY).

This is the ONE durable writer for the closed-loop training event stream. Every
runtime training counter is derived from the same in-memory event batch that
writes these files, so a positive counter can never coexist with a missing or
empty event file (the failure this module exists to prevent):

* ``data/training/events.jsonl``            — every event (union stream)
* ``data/training/decision_records.jsonl``  — one row per evaluated candidate
* ``data/training/no_trade_labels.jsonl``   — economic no-trade labels
* ``data/training/shadow_labels.jsonl``     — executable-realism near-misses
* ``data/training/diagnostics.jsonl``       — data/adapter failures (no label target)
* ``data/training/pending_labels.jsonl``    — labels awaiting resolution
* ``data/training/completed_labels.jsonl``  — resolved labels (Brier feedback)
* ``data/training/learning_state.json``     — persistent learning state

Safety: creates the directory if missing, never raises into the trainer (a bad
candidate writes an error diagnostic and the loop continues), and touches valid
empty files on ``ensure_files()`` so the inspection zip always bundles them.

PAPER ONLY — no wallet, no order path, no live trading.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hte.training.event_sink")

# durable file per logical stream (relative to the sink dir = data/training)
EVENT_FILES = {
    "events": "events.jsonl",
    "decision_records": "decision_records.jsonl",
    "no_trade_labels": "no_trade_labels.jsonl",
    "shadow_labels": "shadow_labels.jsonl",
    "diagnostics": "diagnostics.jsonl",
    "pending_labels": "pending_labels.jsonl",
    "completed_labels": "completed_labels.jsonl",
}

# required base fields on every event (strict schema; null + missing_fields when
# a value is unavailable, never a silently dropped event).
BASE_EVENT_FIELDS = (
    "event_id", "event_type", "timestamp", "run_id", "tick", "source_module",
    "candidate_id", "market_id", "condition_id", "event_id_polymarket", "cluster_id",
    "strategy_tier", "strategy_source", "decision", "decision_reason",
    "rejection_reason", "label_status", "counts_for_readiness",
)


class TrainingEventSink:
    """Append-only JSONL sink for the canonical training event stream.

    All write methods return the event dict written (or None only if the sink is
    hard-disabled). Counters live on the owner (ClosedLoopLearning); this module
    is the single durable writer so files and counters cannot diverge."""

    def __init__(self, data_dir, run_id: str = "", *, source_module: str = "polymarket_trainer"):
        from .artifact_dirs import training_dir
        self.dir = training_dir(data_dir)
        self.run_id = run_id
        self.source_module = source_module
        self.counts = {k: 0 for k in EVENT_FILES}
        self.bregman_diagnostics_written = 0   # actual bregman_diagnostic rows emitted
        self.write_errors = 0
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001 — never fatal
            pass

    # -- low-level durable append -------------------------------------------
    def _append(self, stream: str, row: dict) -> None:
        name = EVENT_FILES.get(stream)
        if not name:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with (self.dir / name).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            self.counts[stream] = self.counts.get(stream, 0) + 1
        except Exception as exc:  # noqa: BLE001 — persistence must never break a tick
            self.write_errors += 1
            logger.debug("event sink append failed (%s): %s", stream, exc)

    def _stamp(self, event_type: str, row: dict) -> dict:
        out = dict(row or {})
        out.setdefault("event_id", uuid.uuid4().hex)
        out["event_type"] = event_type
        out.setdefault("timestamp", round(time.time(), 3))
        out.setdefault("run_id", self.run_id)
        out.setdefault("source_module", self.source_module)
        # strict base schema: fill any unavailable base field with null + record it
        missing = [f for f in BASE_EVENT_FIELDS if f not in out or out.get(f) is None]
        for f in missing:
            out.setdefault(f, None)
        if missing:
            out["missing_fields"] = sorted(set(out.get("missing_fields", []) + missing))
        return out

    def _emit(self, stream: str, event_type: str, row: dict) -> dict:
        ev = self._stamp(event_type, row)
        # write to the typed stream AND the union events.jsonl (single source)
        self._append(stream, ev)
        if stream != "events":
            self._append("events", ev)
        return ev

    # -- public append-only API (what the trainer / closed_loop call) -------
    def append_candidate_evaluated(self, row: dict) -> dict:
        return self._emit("decision_records", "candidate_evaluated", row)

    def append_decision(self, row: dict) -> dict:
        return self._emit("decision_records", "decision", row)

    def append_rejection(self, row: dict) -> dict:
        return self._emit("no_trade_labels", "rejection", row)

    def append_no_trade_label(self, row: dict) -> dict:
        return self._emit("no_trade_labels", "no_trade_label", row)

    def append_shadow_label(self, row: dict) -> dict:
        return self._emit("shadow_labels", "shadow_label", row)

    def append_diagnostic(self, row: dict) -> dict:
        return self._emit("diagnostics", "diagnostic", row)

    def append_bregman_diagnostic(self, row: dict) -> dict:
        self.bregman_diagnostics_written += 1
        return self._emit("diagnostics", "bregman_diagnostic", row)

    def append_active_learning_selection(self, row: dict) -> dict:
        return self._emit("events", "active_learning_selection", row)

    def append_pending_label(self, row: dict) -> dict:
        return self._emit("pending_labels", "pending_label", row)

    def append_completed_label(self, row: dict) -> dict:
        return self._emit("completed_labels", "completed_label", row)

    def append_paper_trade_opened(self, row: dict) -> dict:
        return self._emit("decision_records", "paper_trade_opened", row)

    def append_paper_trade_closed(self, row: dict) -> dict:
        return self._emit("decision_records", "paper_trade_closed", row)

    def append_error(self, source: str, error: str, context: Optional[dict] = None) -> dict:
        """A bad candidate writes an error diagnostic and the loop continues."""
        return self._emit("diagnostics", "error_diagnostic",
                          {"error_source": source, "error": str(error),
                           "context": context or {}})

    # -- durability helpers --------------------------------------------------
    def ensure_files(self) -> None:
        """Touch all stream files so the inspection zip bundles valid (possibly
        empty) JSONL files from tick 1."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for name in EVENT_FILES.values():
                (self.dir / name).touch(exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("event sink ensure_files failed: %s", exc)

    def file_line_counts(self) -> dict:
        """Actual on-disk row counts per stream (the durable source of truth)."""
        out: dict = {}
        for stream, name in EVENT_FILES.items():
            p = self.dir / name
            n = 0
            try:
                if p.exists():
                    with p.open("r", encoding="utf-8") as fh:
                        n = sum(1 for line in fh if line.strip())
            except Exception:  # noqa: BLE001
                n = -1
            out[stream] = n
        return out

    def missing_files(self) -> list:
        return [name for name in EVENT_FILES.values()
                if not (self.dir / name).exists()]
