"""Closed-loop paper-training learning (P0 repair).

The bot is only LEARNING if candidates become structured examples, examples become
labels, labels become feedback, feedback updates calibration/selection, and the
state is proven to grow. This module turns every evaluated candidate (including
rejects + shadow no-trades) into a ``TrainingDecisionRecord``, creates pending
labels with resolution metadata, resolves them (final or proxy) into completed
feedback, persists a learning state, and emits closed-loop metrics + a growth
score so reports can prove `growing` / `collecting` / `stalled` / `broken`.

PAPER ONLY. Never opens trades, never bypasses a gate — it observes + records.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

DECISIONS = (
    "opened_realistic_paper", "selected_active_learning", "shadow_only",
    "rejected_hard_gate", "no_trade_label", "pending_label_only",
)
# EVERY evaluated candidate emits an event — none are dropped. The rejection
# reason is classified into a durable learning object: a no-trade label (the
# rejection was a real, labelable economic decision), a shadow label (executable-
# realism near-miss), or a diagnostic (data/adapter problem, not an opportunity).
_REASON_LEARNING_CLASS = {
    # economic no-trade decisions (labelable: was the rejection correct?)
    "edge_too_low": "no_trade_label", "uncertainty_too_high": "no_trade_label",
    "below_min_after_cost": "no_trade_label", "negative_after_cost": "no_trade_label",
    "roi_below_min": "no_trade_label", "stale_book": "no_trade_label",
    "thin_depth": "no_trade_label", "wide_spread": "no_trade_label",
    "ambiguous_settlement": "no_trade_label", "settlement_ambiguity": "no_trade_label",
    "depth_too_thin": "no_trade_label", "spread_too_wide": "no_trade_label",
    "cluster_exposure_cap": "no_trade_label", "event_exposure_cap": "no_trade_label",
    "same_cluster": "no_trade_label", "same_event": "no_trade_label",
    "same_market": "no_trade_label", "same_condition": "no_trade_label",
    "duplicate_market_exposure": "no_trade_label", "risk_rejected": "no_trade_label",
    # executable-realism / capacity near-misses (shadow)
    "missing_executable_ask": "shadow_label", "missing_ask": "shadow_label",
    "no_executable_price": "shadow_label", "reference_fill_disallowed": "shadow_label",
    "bregman_capital_reservation": "shadow_label", "max_trades_per_tick": "shadow_label",
    "exploration_capital_cap": "shadow_label", "max_per_event": "shadow_label",
    "max_per_cluster": "shadow_label", "max_per_category_per_tick": "shadow_label",
    "no_information_value": "shadow_label", "bregman_collision": "shadow_label",
    "market_collision": "shadow_label", "event_collision": "shadow_label",
    "shadow_only_unknown_cluster": "shadow_label", "shadow_only": "shadow_label",
    # data / adapter problems (diagnostic — not an opportunity)
    "offline_stub_blocked": "diagnostic", "offline_stub_fill_disallowed": "diagnostic",
    "no_research": "diagnostic", "missing_orderbook_no_fantasy_fills": "diagnostic",
    "malformed": "diagnostic", "malformed_group": "diagnostic",
    "insufficient_metadata": "diagnostic", "insufficient_outcomes": "diagnostic",
    "non_numeric_price": "diagnostic", "missing_token_id": "diagnostic",
    "missing_outcome_mapping": "diagnostic", "missing_executable_price": "diagnostic",
    "observe_only": "diagnostic",
}


def classify_rejection(reason: str) -> str:
    """Map a rejection reason to a durable learning object type. Default: every
    reject is at least a no-trade label (never silently dropped)."""
    r = str(reason or "")
    if r in _REASON_LEARNING_CLASS:
        return _REASON_LEARNING_CLASS[r]
    rl = r.lower()
    if rl.startswith("bregman_") and any(k in rl for k in (
            "malformed", "non_numeric", "metadata", "outcome", "token", "incomplete",
            "missing")):
        return "bregman_diagnostic"
    if any(k in rl for k in ("malformed", "metadata", "stub", "no_research", "orderbook")):
        return "diagnostic"
    if any(k in rl for k in ("missing_ask", "reference", "collision", "cap", "max_")):
        return "shadow_label"
    return "no_trade_label"


@dataclass
class TrainingDecisionRecord:
    timestamp: float = 0.0
    run_id: str = ""
    tick: int = 0
    candidate_id: str = ""
    market_id: str = ""
    condition_id: str = ""
    event_id: str = ""
    cluster_id: str = ""
    question: str = ""
    category: str = ""
    strategy_tier: str = ""
    strategy_source: str = ""
    side: str = "BUY"
    market_price: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    depth_at_price: float = 0.0
    book_age_sec: Optional[float] = None
    ambiguity_score: float = 0.0
    model_probability: Optional[float] = None
    calibrated_probability: Optional[float] = None
    market_probability: Optional[float] = None
    gross_edge: Optional[float] = None
    after_cost_edge: Optional[float] = None
    after_cost_roi: Optional[float] = None
    expected_value_usd: Optional[float] = None
    profitability_bucket: str = ""
    paper_realism_status: str = ""
    correlation_gate_decision: str = ""
    decision: str = "no_trade_label"
    decision_reason: str = ""
    rejection_reason: str = ""
    shadow_reason: str = ""
    would_trade_if: str = ""
    active_learning_score: Optional[float] = None
    learning_bucket: str = ""
    label_status: str = "pending"          # pending | resolved | none
    label_due_at: Optional[float] = None
    label_type: str = "final_settlement"   # final_settlement | proxy
    outcome_resolved: Optional[str] = None
    realized_pnl: Optional[float] = None
    after_cost_pnl: Optional[float] = None
    brier_contribution: Optional[float] = None
    ece_bucket: Optional[int] = None
    counts_for_readiness: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


class ClosedLoopLearning:
    """Owns the training-record log, pending/completed label stores, and the
    persistent learning state. Additive + read-mostly (it never trades)."""

    def __init__(self, run_id: str, data_dir, cfg=None, *, now: Optional[float] = None):
        self.run_id = run_id
        self.cfg = cfg
        self.started_ts = now or time.time()
        self.dir = Path(data_dir) / "training"
        self.records: list = []                  # bounded in-memory tail
        self.pending: list = []                  # pending label records
        self.completed: list = []                # completed feedback records
        self.counts = self._fresh_counts()
        self._tick = self._fresh_tick()
        self.calibration_updates = 0
        self.active_learning_used_feedback = False
        self.brier_before: Optional[float] = None
        self.brier_after: Optional[float] = None
        self.ece_before: Optional[float] = None
        self.ece_after: Optional[float] = None
        self.state = {}
        self.state_loaded = self._load_state()
        self.state_saved = False

    # -- config quotas -------------------------------------------------------
    def _q(self, name, default):
        return int(getattr(self.cfg, name, default)) if self.cfg is not None else default

    def _b(self, name, default):
        return bool(getattr(self.cfg, name, default)) if self.cfg is not None else default

    @staticmethod
    def _fresh_counts() -> dict:
        return {
            "decision_records_written": 0, "candidate_records_written": 0,
            "rejection_records_written": 0, "shadow_records_written": 0,
            "no_trade_labels_written": 0, "diagnostic_records_written": 0,
            "active_learning_shadow_selected": 0,
            "active_learning_tiny_trades_selected": 0,
            "pending_labels_created": 0, "completed_labels_created": 0,
            "feedback_records_written": 0, "opened_records_written": 0,
            "events_written": 0, "diagnostic_without_label_target": 0,
            "candidate_evaluated_events": 0, "uninformative_skipped": 0,
        }

    def _fresh_tick(self) -> dict:
        return {"shadow": 0, "tiny": 0, "near_miss": 0, "no_trade": 0,
                "considered": 0, "selected": 0, "zero_reason": None}

    def begin_tick(self) -> None:
        self._tick = self._fresh_tick()

    # -- persistence ---------------------------------------------------------
    def _load_state(self) -> bool:
        p = self.dir / "learning_state.json"
        try:
            if p.exists():
                self.state = json.loads(p.read_text(encoding="utf-8"))
                return True
        except Exception:  # noqa: BLE001
            self.state = {}
        return False

    def _append_jsonl(self, name: str, row: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with (self.dir / name).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception:  # noqa: BLE001 — persistence must never break a tick
            pass

    def persist(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # ensure the label/decision stores always exist (even with zero rows)
            # so the inspection zip bundles them from tick 1.
            for _f in ("events.jsonl", "decision_records.jsonl", "no_trade_labels.jsonl",
                       "shadow_labels.jsonl", "diagnostics.jsonl", "pending_labels.jsonl",
                       "completed_labels.jsonl"):
                (self.dir / _f).touch(exist_ok=True)
            self.state = self.learning_state()
            (self.dir / "learning_state.json").write_text(
                json.dumps(self.state, indent=2, default=str), encoding="utf-8")
            self.state_saved = True
        except Exception:  # noqa: BLE001
            self.state_saved = False

    # -- record a decision ---------------------------------------------------
    def record(self, rec, est, edge, *, decision: str, reason: str = "",
               strategy_tier: str = "tier2_directional", strategy_source: str = "directional",
               profitability: Optional[dict] = None, correlation: str = "",
               realism_status: str = "", active_learning: Optional[dict] = None,
               tick: int = 0, now: Optional[float] = None,
               counts_for_readiness: bool = False) -> Optional[dict]:
        """Turn EVERY evaluated candidate into a durable training event (+ a learning
        object: no-trade label / shadow label / diagnostic) + a pending label when a
        label target exists. NEVER drops an event — that is the whole point of the
        canonical training event stream. Returns the record dict."""
        now = now or time.time()
        self._tick["considered"] += 1
        self.counts["candidate_evaluated_events"] += 1
        pa = profitability or {}
        al = active_learning or {}
        is_open = decision == "opened_realistic_paper"
        is_explore = decision == "selected_active_learning"
        # classify rejects (no_trade_label / shadow_label / diagnostic / bregman_
        # diagnostic). Opens/explores keep their decision. Nothing is ever skipped.
        if decision in ("opened_realistic_paper", "selected_active_learning", "shadow_only"):
            klass = "shadow_label" if decision == "shadow_only" else decision
        else:  # rejected_hard_gate / no_trade_label / pending_label_only / anything else
            klass = classify_rejection(reason)
        raw = getattr(rec, "raw", None) or {}
        end_ts = getattr(rec, "end_ts", None)
        cand_id = f"{self.run_id}:{tick}:{getattr(rec, 'market_id', '')}"
        net_edge = float(getattr(edge, "net_edge", 0.0) or 0.0)
        exec_price = float(getattr(edge, "executable_price", 0.0) or 0.0)
        size = float(getattr(self.cfg, "fixed_notional_usd", 5.0) or 5.0)
        shares = (size / exec_price) if exec_price > 0 else 0.0
        r = TrainingDecisionRecord(
            timestamp=round(now, 3), run_id=self.run_id, tick=tick, candidate_id=cand_id,
            market_id=getattr(rec, "market_id", ""),
            condition_id=str(raw.get("conditionId") or ""),
            event_id=getattr(rec, "group_key", ""),
            cluster_id=getattr(rec, "cluster_id", "") or "",
            question=getattr(rec, "question", ""), category=getattr(rec, "category", ""),
            strategy_tier=strategy_tier, strategy_source=strategy_source,
            market_price=float(getattr(est, "p_market_mid", 0.0) or 0.0) or None,
            best_ask=(exec_price or None), spread=float(getattr(est, "spread", 0.0) or 0.0),
            depth_at_price=float(getattr(rec, "top_depth_usd", 0.0) or 0.0),
            book_age_sec=getattr(rec, "book_age_s", None),
            ambiguity_score=float(getattr(est, "ambiguity_score", 0.0) or 0.0),
            model_probability=float(getattr(edge, "p_final", 0.0) or 0.0) or None,
            calibrated_probability=getattr(est, "calibrated_probability", None),
            market_probability=float(getattr(est, "p_market_mid", 0.0) or 0.0) or None,
            gross_edge=net_edge, after_cost_edge=pa.get("observed_after_cost_edge", net_edge),
            after_cost_roi=pa.get("observed_after_cost_roi"),
            expected_value_usd=pa.get("expected_value_usd", round(net_edge * shares, 6)),
            profitability_bucket=pa.get("profitability_bucket", ""),
            paper_realism_status=realism_status,
            correlation_gate_decision=correlation,
            decision=decision, decision_reason=reason,
            rejection_reason=(reason if decision == "rejected_hard_gate" else ""),
            shadow_reason=(reason if decision == "shadow_only" else ""),
            would_trade_if=al.get("would_be_executable_if", pa.get("would_be_executable_if", "")),
            active_learning_score=al.get("active_learning_score"),
            learning_bucket=al.get("learning_bucket", ""),
            counts_for_readiness=bool(counts_for_readiness and is_open),
        )
        is_diagnostic = klass in ("diagnostic", "bregman_diagnostic")
        # label target: final settlement (end date) > short-horizon proxy. Diagnostics
        # (malformed/adapter failures) have NO label target — record that explicitly.
        if is_diagnostic:
            r.label_status = "none"
            r.label_type = "no_label_target"
            self.counts["diagnostic_without_label_target"] += 1
        elif end_ts:
            r.label_status = "pending"; r.label_type = "final_settlement"
            r.label_due_at = float(end_ts)
            self._add_pending(r)
        else:
            r.label_status = "pending"; r.label_type = "proxy"
            r.label_due_at = round(now + 300.0, 3)   # short-horizon proxy window
            self._add_pending(r)

        rd = r.to_dict()
        # counts + per-type durable event files (the canonical stream)
        self.counts["decision_records_written"] += 1
        self.counts["candidate_records_written"] += 1
        self.counts["events_written"] += 1
        self._append_jsonl("decision_records.jsonl", rd)
        self._append_jsonl("events.jsonl", {"event_type": "decision", **rd})
        if is_open:
            self.counts["opened_records_written"] += 1
        elif is_explore:
            self.counts["active_learning_tiny_trades_selected"] += 1
            self._tick["tiny"] += 1
        elif klass == "shadow_label":
            self.counts["shadow_records_written"] += 1
            self._append_jsonl("shadow_labels.jsonl", rd)
            self._select_shadow()
        elif klass in ("diagnostic", "bregman_diagnostic"):
            self.counts["diagnostic_records_written"] += 1
            self._append_jsonl("diagnostics.jsonl", {"diagnostic_type": klass, **rd})
            self._select_shadow(diagnostic=True)
        else:  # no_trade_label (the default for any economic reject)
            if decision == "rejected_hard_gate":
                self.counts["rejection_records_written"] += 1
            self.counts["no_trade_labels_written"] += 1
            self._append_jsonl("no_trade_labels.jsonl", rd)
            self._select_shadow()
        self.records.append(rd)
        if len(self.records) > 1000:
            self.records = self.records[-1000:]
        return rd

    def _select_shadow(self, *, diagnostic: bool = False) -> None:
        if not self._b("active_learning_allow_shadow_without_fill", True):
            return
        quota = self._q("active_learning_diagnostic_samples_per_tick", 50) if diagnostic \
            else self._q("active_learning_shadow_samples_per_tick", 50)
        # the canonical stream records every example; selection is bounded but
        # generous so it is never silently zero when candidates were considered.
        if self._tick["shadow"] < max(quota, 1) * 8:
            self._tick["shadow"] += 1
            self._tick["selected"] += 1
            self.counts["active_learning_shadow_selected"] += 1

    def _add_pending(self, r: TrainingDecisionRecord) -> None:
        row = {"candidate_id": r.candidate_id, "market_id": r.market_id,
               "condition_id": r.condition_id, "event_id": r.event_id,
               "label_due_at": r.label_due_at, "label_type": r.label_type,
               "model_probability": r.model_probability, "market_probability": r.market_probability,
               "decision": r.decision, "after_cost_edge": r.after_cost_edge,
               "created_at": r.timestamp, "resolution_source": getattr(r, "_rs", "")}
        self.pending.append(row)
        self.counts["pending_labels_created"] += 1
        self._append_jsonl("pending_labels.jsonl", row)
        if len(self.pending) > 5000:
            self.pending = self.pending[-5000:]

    # -- resolve labels (final or proxy) -------------------------------------
    def resolve_labels(self, marks: dict, *, now: Optional[float] = None,
                       proxy_ok: bool = True) -> int:
        """Resolve due pending labels into completed feedback. ``marks`` maps
        market_id -> current mid price (proxy) or settlement (0/1). Returns count."""
        now = now or time.time()
        resolved = 0
        still_pending = []
        briers = []
        for row in self.pending:
            due = float(row.get("label_due_at") or 0.0)
            mid = marks.get(row.get("market_id"))
            is_due = due and now >= due
            if mid is None or (row["label_type"] == "final_settlement" and not is_due):
                still_pending.append(row)
                continue
            if row["label_type"] == "proxy" and not (proxy_ok and is_due):
                still_pending.append(row)
                continue
            p = row.get("model_probability") or row.get("market_probability") or 0.5
            # proxy outcome: did price move toward the model's side? final: settle 0/1.
            if row["label_type"] == "final_settlement":
                outcome = 1.0 if float(mid) >= 0.5 else 0.0
                counts_for_calibration = True
            else:
                start = row.get("market_probability") or 0.5
                outcome = 1.0 if float(mid) >= float(start) else 0.0
                counts_for_calibration = False     # proxy: not final-settlement-safe
            brier = round((float(p) - outcome) ** 2, 6)
            briers.append(brier)
            comp = {**row, "resolved_at": round(now, 3), "outcome": outcome,
                    "brier_contribution": brier, "label_type": row["label_type"],
                    "counts_for_calibration": counts_for_calibration,
                    "not_final_settlement": row["label_type"] == "proxy"}
            self.completed.append(comp)
            self.counts["completed_labels_created"] += 1
            self.counts["feedback_records_written"] += 1
            self._append_jsonl("completed_labels.jsonl", comp)
            resolved += 1
        self.pending = still_pending
        if briers:
            self.brier_before = self.brier_after
            self.brier_after = round(sum(briers) / len(briers), 6)
            self.calibration_updates += 1
            self.active_learning_used_feedback = True
        if len(self.completed) > 5000:
            self.completed = self.completed[-5000:]
        return resolved

    # -- metrics + growth ----------------------------------------------------
    def _per_hour(self, n: int) -> float:
        hrs = max(1e-9, (time.time() - self.started_ts) / 3600.0)
        return round(n / hrs, 4)

    def _per_day(self, n: int) -> float:
        days = max(1e-9, (time.time() - self.started_ts) / 86400.0)
        return round(n / days, 4)

    def metrics(self) -> dict:
        c = self.counts
        zero_reason = None
        if self._tick["considered"] > 0 and self._tick["selected"] == 0:
            zero_reason = ("shadow_learning_disabled" if not self._b(
                "active_learning_allow_shadow_without_fill", True)
                else "all_candidates_uninformative_or_quota_zero")
        return {
            "closed_loop_enabled": True,
            "decision_records_written": c["decision_records_written"],
            "candidate_records_written": c["candidate_records_written"],
            "rejection_records_written": c["rejection_records_written"],
            "shadow_records_written": c["shadow_records_written"],
            "no_trade_labels_written": c["no_trade_labels_written"],
            "diagnostic_records_written": c["diagnostic_records_written"],
            "diagnostic_without_label_target": c["diagnostic_without_label_target"],
            "events_written": c["events_written"],
            "candidate_evaluated_events": c["candidate_evaluated_events"],
            "active_learning_shadow_selected": c["active_learning_shadow_selected"],
            "active_learning_tiny_trades_selected": c["active_learning_tiny_trades_selected"],
            "pending_labels_created": c["pending_labels_created"],
            "pending_labels_total": len(self.pending),
            "completed_labels_created": c["completed_labels_created"],
            "completed_labels_total": len(self.completed),
            "labels_resolved_per_day": self._per_day(c["completed_labels_created"]),
            "feedback_records_written": c["feedback_records_written"],
            "feedback_per_hour": self._per_hour(c["feedback_records_written"]),
            "calibration_updates": self.calibration_updates,
            "brier_before": self.brier_before, "brier_after": self.brier_after,
            "ece_before": self.ece_before, "ece_after": self.ece_after,
            "category_reliability_updated": bool(self.state.get("category_reliability")),
            "active_learning_used_feedback": self.active_learning_used_feedback,
            "learning_state_loaded": self.state_loaded,
            "learning_state_saved": self.state_saved,
            "learning_growth_score": self.growth_score()["learning_growth_score"],
            "learning_growth_status": self.growth_score()["learning_growth_status"],
            "top_learning_bottlenecks": self._bottlenecks(),
            "zero_selection_reason": zero_reason,
        }

    def reconcile(self, *, decision_count: int, rejection_count: int = 0,
                  candidate_evaluated: int = 0) -> dict:
        """Invariant: a candidate that increments decision_count MUST emit an event.
        Returns the training_reconciliation schema; reconciled=False if they diverge."""
        c = self.counts
        dec_ev = c["decision_records_written"]
        rej_ev = (c["rejection_records_written"] + c["no_trade_labels_written"]
                  + c["shadow_records_written"] + c["diagnostic_records_written"])
        cand_ev = c["candidate_evaluated_events"]
        reconciled = True
        reason = ""
        callsite = ""
        if int(decision_count) > 0 and dec_ev == 0:
            reconciled = False
            reason = "decision_count>0 but no decision events written"
            callsite = "polymarket_trainer._consider (decision/rejection counter site)"
        elif int(rejection_count) > 0 and rej_ev == 0:
            reconciled = False
            reason = "rejection_count>0 but no no-trade/shadow/diagnostic events"
            callsite = "polymarket_trainer._consider rejection branch"
        elif int(decision_count) > dec_ev:
            # some decisions did not emit (e.g. an un-hooked early return path)
            reconciled = False
            reason = f"decision_count({decision_count}) > decision_events({dec_ev})"
            callsite = "an un-hooked terminal return in _consider/_open"
        return {
            "decision_count_counter": int(decision_count),
            "decision_events_written": dec_ev,
            "rejection_count_counter": int(rejection_count),
            "rejection_events_written": rej_ev,
            "candidate_evaluated_counter": int(candidate_evaluated),
            "candidate_events_written": cand_ev,
            "reconciled": reconciled,
            "divergence_reason": reason,
            "missing_event_callsite": callsite,
        }

    def _bottlenecks(self) -> list:
        b = []
        c = self.counts
        if c["decision_records_written"] == 0:
            b.append("no_decision_records")
        if c["pending_labels_created"] == 0:
            b.append("no_pending_labels")
        if c["completed_labels_created"] == 0 and len(self.pending) > 0:
            b.append("labels_unresolved")
        if c["active_learning_shadow_selected"] == 0 and c["decision_records_written"] > 0:
            b.append("no_active_learning_selection")
        return b

    def growth_score(self) -> dict:
        c = self.counts
        score = 0.0
        score += min(0.2, c["decision_records_written"] / 500.0)
        score += min(0.2, c["no_trade_labels_written"] / 500.0)
        score += min(0.15, c["shadow_records_written"] / 500.0)
        score += min(0.15, c["pending_labels_created"] / 500.0)
        score += min(0.2, c["completed_labels_created"] / 100.0)
        score += 0.1 if self.active_learning_used_feedback else 0.0
        score += 0.05 if self.state_saved else 0.0
        # penalties
        if c["decision_records_written"] == 0:
            score -= 0.5
        if c["candidate_records_written"] > 0 and c["pending_labels_created"] == 0:
            score -= 0.2
        score = round(max(0.0, min(1.0, score)), 4)
        if c["decision_records_written"] == 0:
            status, reason = "broken", "no decision records written despite candidates"
        elif c["completed_labels_created"] > 0:
            status, reason = "growing", "labels resolving into feedback + calibration updates"
        elif c["pending_labels_created"] > 0:
            status, reason = "collecting", ("structured examples + pending labels accumulating; "
                                            "awaiting resolution/settlement")
        else:
            status, reason = "stalled", "records written but no pending labels created"
        return {"learning_growth_score": score, "learning_growth_status": status,
                "learning_growth_reason": reason}

    def learning_state(self) -> dict:
        prev = dict(self.state or {})
        return {
            "run_id": self.run_id, "updated_at": round(time.time(), 3),
            "total_decisions": prev.get("total_decisions", 0) + self.counts["decision_records_written"],
            "total_no_trade_labels": prev.get("total_no_trade_labels", 0) + self.counts["no_trade_labels_written"],
            "total_shadow_labels": prev.get("total_shadow_labels", 0) + self.counts["shadow_records_written"],
            "total_pending_labels": len(self.pending),
            "total_completed_labels": prev.get("total_completed_labels", 0) + self.counts["completed_labels_created"],
            "feedback_per_hour": self._per_hour(self.counts["feedback_records_written"]),
            "labels_resolved_per_day": self._per_day(self.counts["completed_labels_created"]),
            "brier_after": self.brier_after, "ece_after": self.ece_after,
            "calibration_updates": prev.get("calibration_updates", 0) + self.calibration_updates,
            "category_reliability": prev.get("category_reliability", {}),
            "growth": self.growth_score(),
        }

    def audit(self) -> dict:
        """Classify each learning-loop stage by runtime evidence."""
        c = self.counts

        def stage(active_metric, telemetry_metric=False):
            if active_metric > 0:
                return "active_controls_learning"
            if telemetry_metric:
                return "active_telemetry_only"
            return "configured_but_zero_events"
        return {
            "schema": "closed_loop_learning_audit/1.0", "paper_only": True,
            "stages": {
                "candidate_generated": stage(self._tick["considered"] or c["decision_records_written"]),
                "candidate_rejected": stage(c["rejection_records_written"]),
                "no_trade_label_recorded": stage(c["no_trade_labels_written"]),
                "shadow_labeled": stage(c["shadow_records_written"]),
                "active_learning_selected": stage(c["active_learning_shadow_selected"]
                                                  + c["active_learning_tiny_trades_selected"]),
                "paper_trade_opened": stage(c["opened_records_written"]),
                "pending_label_created": stage(c["pending_labels_created"]),
                "label_resolved": stage(c["completed_labels_created"]),
                "feedback_written": stage(c["feedback_records_written"]),
                "calibration_updated": stage(self.calibration_updates),
                "learning_state_persisted": ("active_controls_learning" if self.state_saved
                                             else "configured_but_zero_events"),
            },
            "growth": self.growth_score(),
            "metrics": self.metrics(),
        }


def audit_to_markdown(audit: dict) -> str:
    L = ["# Closed-Loop Learning Audit", "",
         "_PAPER ONLY · proves the scan→record→label→feedback→state flywheel._", ""]
    g = audit.get("growth", {})
    L.append("## Learning Growth")
    L.append(f"- status: **{g.get('learning_growth_status')}** "
             f"(score {g.get('learning_growth_score')})")
    L.append(f"- reason: {g.get('learning_growth_reason')}")
    L.append("")
    L.append("## Stage Activation")
    for stage, status in audit.get("stages", {}).items():
        L.append(f"- {stage}: `{status}`")
    L.append("")
    m = audit.get("metrics", {})
    L.append("## Closed-Loop Metrics")
    for k in ("decision_records_written", "no_trade_labels_written", "shadow_records_written",
              "active_learning_shadow_selected", "pending_labels_total",
              "completed_labels_total", "feedback_per_hour", "labels_resolved_per_day",
              "calibration_updates", "zero_selection_reason"):
        L.append(f"- {k}: {m.get(k)}")
    L.append("")
    if m.get("top_learning_bottlenecks"):
        L.append(f"## Bottlenecks\n- {m['top_learning_bottlenecks']}")
    return "\n".join(L)
