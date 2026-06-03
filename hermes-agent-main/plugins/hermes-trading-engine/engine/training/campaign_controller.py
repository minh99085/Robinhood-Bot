"""Institutional paper-training campaign controller (PAPER ONLY).

This is the bridge between paper training and real-money readiness. During a
campaign the bot FREEZES algorithm development and runs the existing aggressive
paper engine purely to collect durable evidence; the controller aggregates that
evidence ACROSS runs, persists it, and produces a hard verdict that decides
whether a real-money Polymarket micro-canary may even be considered. It NEVER
enables live trading and never relaxes a risk gate.

Required principle: Bregman arbitrage stays the flagship strategy, but in
campaign mode evidence quality beats new code — algorithm parameters are not
promoted unless campaign evidence thresholds pass, and the verdict depends on
EVIDENCE, never on elapsed time alone.

Quant scope documented across this module:
* Data Acquisition & Ingestion — decisions / scan-driven evidence counts.
* Data Preprocessing & Feature Engineering — stale-book / null-rate inputs.
* Statistical & Probabilistic Modeling — calibration / Brier / log-loss / ECE,
  clean-label-only updates, calibration no-regression gate.
* Signal Generation & Strategy Development — Bregman candidates / certified /
  false positives / partial-fill hedge breaks (Bregman priority).
* Risk Management & Portfolio Optimization — risk-gate violations, drawdown, CVaR.
* Backtesting & Simulation — realistic-fill + replay validation evidence.
* Strategy Optimization & Robustness Testing — algorithm freeze, no promotion.
* Execution Engine CLOB v2 Simulation — realistic-fill expectancy, slippage.
* Live Trading & Monitoring — verdict, blockers, next evidence target.
* Compliance/Security/Operational Excellence — no live orders, verdict never
  enables live, evidence never deleted, fail closed.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "CampaignState", "CampaignThresholds", "CampaignProgress", "CampaignEvidence",
    "CampaignVerdict", "TrainingCampaignController", "campaign_json", "campaign_markdown",
    "VERDICT_BLOCKED", "VERDICT_CONTINUE", "VERDICT_PAPER_QUALIFIED",
    "VERDICT_MICRO_CANARY_READY", "CAMPAIGN_BLOCKERS",
]

VERDICT_BLOCKED = "blocked"
VERDICT_CONTINUE = "continue_training"
VERDICT_PAPER_QUALIFIED = "paper_qualified"
VERDICT_MICRO_CANARY_READY = "micro_canary_ready"

_LIVE_READY_STATES = {"micro_canary_ready", "canary_ready"}

# campaign blocker taxonomy (documented + asserted)
CAMPAIGN_BLOCKERS = (
    "insufficient_runtime", "insufficient_decisions", "insufficient_paper_trades",
    "insufficient_resolved_labels", "insufficient_clean_labels",
    "insufficient_bregman_candidates", "insufficient_bregman_certified",
    "bregman_false_positive", "bregman_partial_fill_hedge_break",
    "negative_after_cost_expectancy", "negative_realistic_fill_expectancy",
    "calibration_regression", "stale_chainlink", "stale_order_book",
    "stale_data_confidence_improvement", "risk_gate_violation", "live_order_attempted",
    "dirty_labels", "excessive_drawdown", "excessive_slippage",
    "optimistic_only_profitability", "algorithm_not_frozen", "campaign_not_long_enough",
    "replay_validation_missing",
)

# aggregation key groups
_COUNTER_KEYS = ("decisions", "paper_trades", "resolved_labels", "clean_labels",
                 "bregman_candidates", "bregman_certified")
_FAILURE_KEYS = ("bregman_false_positives", "partial_fill_hedge_breaks",
                 "risk_violations", "live_orders")
_BOOL_OR_KEYS = ("stale_chainlink", "stale_book", "stale_data_confidence_improvement",
                 "replay_validation_ran")
_MAX_KEYS = ("max_drawdown_pct", "slippage_bps", "stale_data_rejection_rate")
_LATEST_KEYS = ("after_cost_expectancy", "realistic_fill_expectancy",
                "optimistic_expectancy", "calibration_error", "baseline_calibration_error",
                "brier", "log_loss", "ece", "live_readiness_state", "validation_campaign")


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _i(v, d=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


# --------------------------------------------------------------------------- #
# thresholds
# --------------------------------------------------------------------------- #
@dataclass
class CampaignThresholds:
    target_min_days: int = 14
    target_min_runtime_hours: float = 72.0
    target_min_decisions: int = 1000
    target_min_paper_trades: int = 300
    target_min_resolved_labels: int = 100
    target_min_clean_labels: int = 80
    target_min_bregman_candidates: int = 50
    target_min_bregman_certified: int = 1
    max_allowed_bregman_false_positives: int = 0
    max_allowed_partial_fill_hedge_breaks: int = 0
    require_after_cost_profitability: bool = True
    require_realistic_fill_profitability: bool = True
    require_calibration_no_regression: bool = True
    require_no_risk_gate_violations: bool = True
    require_no_stale_data_violations: bool = True
    require_no_live_orders: bool = True
    require_clean_settlement_labels: bool = True
    require_bregman_certification_quality: bool = True
    require_replay_or_realistic_fill_validation: bool = True
    require_algorithm_freeze: bool = True
    # internal quality caps (only ever TIGHTEN; never relaxed by env)
    max_dirty_label_rate: float = 0.20
    max_drawdown_pct: float = 0.25
    max_slippage_bps: float = 200.0
    max_stale_data_rejection_rate: float = 0.10
    calibration_regression_tol: float = 0.05

    @classmethod
    def from_config(cls, cfg) -> "CampaignThresholds":
        g = lambda n, d: getattr(cfg, n, d)
        return cls(
            target_min_days=int(g("campaign_target_min_days", 14)),
            target_min_decisions=int(g("campaign_target_min_decisions", 1000)),
            target_min_paper_trades=int(g("campaign_target_min_paper_trades", 300)),
            target_min_resolved_labels=int(g("campaign_target_min_resolved_labels", 100)),
            target_min_bregman_candidates=int(g("campaign_target_min_bregman_candidates", 50)),
            max_allowed_bregman_false_positives=int(g("campaign_max_bregman_false_positives", 0)))

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# persistent state
# --------------------------------------------------------------------------- #
@dataclass
class CampaignState:
    campaign_id: str
    campaign_name: str
    algorithm_freeze_mode: bool = False
    started_ts: Optional[float] = None
    last_update_ts: Optional[float] = None
    stop_requested: bool = False
    runs: dict = field(default_factory=dict)        # run_id -> latest cumulative snapshot
    latest_run_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id, "campaign_name": self.campaign_name,
            "algorithm_freeze_mode": bool(self.algorithm_freeze_mode),
            "started_ts": self.started_ts, "last_update_ts": self.last_update_ts,
            "stop_requested": bool(self.stop_requested), "runs": self.runs,
            "latest_run_id": self.latest_run_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CampaignState":
        d = d or {}
        return cls(
            campaign_id=d.get("campaign_id") or ("campaign_" + uuid.uuid4().hex[:10]),
            campaign_name=d.get("campaign_name", "institutional_paper_campaign"),
            algorithm_freeze_mode=bool(d.get("algorithm_freeze_mode", False)),
            started_ts=d.get("started_ts"), last_update_ts=d.get("last_update_ts"),
            stop_requested=bool(d.get("stop_requested", False)),
            runs=dict(d.get("runs") or {}), latest_run_id=d.get("latest_run_id"))


# --------------------------------------------------------------------------- #
# aggregated evidence
# --------------------------------------------------------------------------- #
@dataclass
class CampaignEvidence:
    runs: int = 0
    started_ts: Optional[float] = None
    last_update_ts: Optional[float] = None
    elapsed_days: float = 0.0
    runtime_hours: float = 0.0
    decisions: int = 0
    paper_trades: int = 0
    resolved_labels: int = 0
    clean_labels: int = 0
    dirty_label_rate: float = 0.0
    bregman_candidates: int = 0
    bregman_certified: int = 0
    bregman_false_positives: int = 0
    partial_fill_hedge_breaks: int = 0
    risk_violations: int = 0
    live_orders: int = 0
    after_cost_expectancy: float = 0.0
    realistic_fill_expectancy: float = 0.0
    optimistic_expectancy: float = 0.0
    calibration_error: float = 0.0
    baseline_calibration_error: float = 0.0
    brier: float = 0.0
    log_loss: float = 0.0
    ece: float = 0.0
    stale_data_rejection_rate: float = 0.0
    stale_chainlink: bool = False
    stale_book: bool = False
    stale_data_confidence_improvement: bool = False
    max_drawdown_pct: float = 0.0
    slippage_bps: float = 0.0
    algorithm_freeze_mode: bool = False
    live_readiness_state: str = "paper_learning"
    validation_campaign: Optional[dict] = None
    replay_validation_ran: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# progress
# --------------------------------------------------------------------------- #
@dataclass
class CampaignProgress:
    targets: dict = field(default_factory=dict)     # name -> {current, target, pct, met}

    def to_dict(self) -> dict:
        return {"targets": self.targets,
                "overall_pct": round(
                    100.0 * sum(1 for t in self.targets.values() if t["met"])
                    / max(1, len(self.targets)), 1)}


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
@dataclass
class CampaignVerdict:
    state: str
    blockers: list = field(default_factory=list)
    hard_safety_failures: list = field(default_factory=list)
    next_target: str = ""
    reasons: list = field(default_factory=list)
    live_trading_enabled: bool = False  # HARD invariant: always False

    def to_dict(self) -> dict:
        return {"state": self.state, "blockers": list(self.blockers),
                "hard_safety_failures": list(self.hard_safety_failures),
                "next_target": self.next_target, "reasons": list(self.reasons),
                "live_trading_enabled": False}


# --------------------------------------------------------------------------- #
# controller
# --------------------------------------------------------------------------- #
class TrainingCampaignController:
    """Aggregates durable campaign evidence across runs and produces a verdict.

    Never enables live trading. Evidence accumulates across runs (per-run
    cumulative counters are summed; safety failures are summed; quality scalars
    use the latest run). State persists to JSON (and optionally SQLite)."""

    def __init__(self, *, campaign_name: str = "institutional_paper_campaign",
                 thresholds: Optional[CampaignThresholds] = None,
                 algorithm_freeze_mode: bool = False,
                 state_path: Optional[Path] = None, store=None,
                 run_id: Optional[str] = None, started_ts: Optional[float] = None,
                 state: Optional[CampaignState] = None):
        self.thresholds = thresholds or CampaignThresholds()
        self.algorithm_freeze_mode = bool(algorithm_freeze_mode)
        self.state_path = Path(state_path) if state_path else None
        self.store = store
        self.run_id = run_id
        if state is not None:
            self.state = state
            self.algorithm_freeze_mode = self.algorithm_freeze_mode or state.algorithm_freeze_mode
        else:
            self.state = CampaignState(
                campaign_id="campaign_" + uuid.uuid4().hex[:10], campaign_name=campaign_name,
                algorithm_freeze_mode=self.algorithm_freeze_mode,
                started_ts=started_ts)

    @property
    def campaign_name(self) -> str:
        return self.state.campaign_name

    # -- construction helpers ----------------------------------------------
    @classmethod
    def from_config(cls, cfg, *, state_path: Optional[Path] = None, store=None,
                    run_id: Optional[str] = None) -> "TrainingCampaignController":
        ctrl = None
        if state_path and Path(state_path).exists():
            try:
                ctrl = cls.load(state_path, thresholds=CampaignThresholds.from_config(cfg),
                                store=store,
                                algorithm_freeze_mode=bool(getattr(cfg, "algorithm_freeze_mode",
                                                                   False)))
            except Exception:  # noqa: BLE001 — never break the trainer on load failure
                ctrl = None
        if ctrl is None:
            ctrl = cls(campaign_name=getattr(cfg, "campaign_name", "institutional_paper_campaign"),
                       thresholds=CampaignThresholds.from_config(cfg),
                       algorithm_freeze_mode=bool(getattr(cfg, "algorithm_freeze_mode", False)),
                       state_path=state_path, store=store, run_id=run_id)
        ctrl.run_id = run_id or ctrl.run_id
        ctrl.store = store or ctrl.store
        return ctrl

    @classmethod
    def load(cls, state_path, *, thresholds: Optional[CampaignThresholds] = None,
             store=None, algorithm_freeze_mode: bool = False) -> "TrainingCampaignController":
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        state = CampaignState.from_dict(data)
        # Reconstruct the campaign's ACTUAL thresholds from the persisted file so a
        # status/report script (no live trainer) renders a faithful verdict — fall
        # back to the institutional defaults only when none were persisted.
        if thresholds is None and isinstance(data.get("thresholds"), dict):
            fields = {f for f in CampaignThresholds.__dataclass_fields__}
            thresholds = CampaignThresholds(
                **{k: v for k, v in data["thresholds"].items() if k in fields})
        return cls(campaign_name=state.campaign_name, thresholds=thresholds,
                   algorithm_freeze_mode=algorithm_freeze_mode or state.algorithm_freeze_mode,
                   state_path=state_path, store=store, state=state)

    # -- ingest -------------------------------------------------------------
    def update(self, snapshot: dict) -> CampaignVerdict:
        snap = dict(snapshot or {})
        rid = str(snap.get("run_id") or self.run_id or "default")
        now = time.time()
        rec = dict(snap)
        rec["_last_update_ts"] = now
        if "started_ts" not in rec or rec.get("started_ts") is None:
            rec["started_ts"] = now
        prev = self.state.runs.get(rid)
        if prev:
            # monotonic-within-run: cumulative counters/failures take the max
            for k in (_COUNTER_KEYS + _FAILURE_KEYS):
                rec[k] = max(_i(prev.get(k)), _i(rec.get(k)))
            rec["started_ts"] = prev.get("started_ts", rec["started_ts"])
        self.state.runs[rid] = rec
        self.state.latest_run_id = rid
        self.state.last_update_ts = now
        starts = [r.get("started_ts") for r in self.state.runs.values()
                  if r.get("started_ts") is not None]
        self.state.started_ts = min(starts) if starts else now
        if bool(snap.get("algorithm_freeze_mode")):
            self.algorithm_freeze_mode = True
        self.state.algorithm_freeze_mode = self.algorithm_freeze_mode
        if self.state_path or self.store:
            try:
                self.persist()
            except Exception:  # noqa: BLE001 — persistence must never break a tick
                pass
        return self.verdict()

    # -- aggregation --------------------------------------------------------
    def evidence(self) -> CampaignEvidence:
        runs = self.state.runs or {}
        ev = CampaignEvidence()
        ev.runs = len(runs)
        if not runs:
            ev.algorithm_freeze_mode = self.algorithm_freeze_mode
            return ev
        latest = runs.get(self.state.latest_run_id) or next(iter(runs.values()))
        for k in _COUNTER_KEYS:
            setattr(ev, k, sum(_i(r.get(k)) for r in runs.values()))
        for k in _FAILURE_KEYS:
            setattr(ev, k, sum(_i(r.get(k)) for r in runs.values()))
        for k in _BOOL_OR_KEYS:
            val = any(bool(r.get(k)) for r in runs.values())
            attr = "stale_book" if k == "stale_book" else k
            setattr(ev, attr, val)
        for k in _MAX_KEYS:
            setattr(ev, k, round(max(_f(r.get(k)) for r in runs.values()), 6))
        for k in _LATEST_KEYS:
            setattr(ev, k, latest.get(k) if k in ("live_readiness_state", "validation_campaign")
                    else _f(latest.get(k)))
        ev.live_readiness_state = latest.get("live_readiness_state") or "paper_learning"
        ev.validation_campaign = latest.get("validation_campaign")
        ev.dirty_label_rate = (round(max(0, ev.resolved_labels - ev.clean_labels)
                                     / ev.resolved_labels, 6) if ev.resolved_labels else 0.0)
        ev.runtime_hours = round(sum(_f(r.get("runtime_seconds")) for r in runs.values())
                                 / 3600.0, 4)
        ev.started_ts = self.state.started_ts
        ev.last_update_ts = self.state.last_update_ts
        if ev.started_ts and ev.last_update_ts:
            ev.elapsed_days = round(max(0.0, (ev.last_update_ts - ev.started_ts) / 86400.0), 4)
        ev.algorithm_freeze_mode = self.algorithm_freeze_mode
        return ev

    # -- progress -----------------------------------------------------------
    def progress(self) -> CampaignProgress:
        ev = self.evidence()
        th = self.thresholds

        def row(cur, tgt):
            cur, tgt = float(cur), float(tgt)
            return {"current": round(cur, 4), "target": round(tgt, 4),
                    "pct": round(min(100.0, 100.0 * cur / tgt), 1) if tgt > 0 else 100.0,
                    "met": cur >= tgt}

        targets = {
            "runtime_days": row(ev.elapsed_days, th.target_min_days),
            "runtime_hours": row(ev.runtime_hours, th.target_min_runtime_hours),
            "decisions": row(ev.decisions, th.target_min_decisions),
            "paper_trades": row(ev.paper_trades, th.target_min_paper_trades),
            "resolved_labels": row(ev.resolved_labels, th.target_min_resolved_labels),
            "clean_labels": row(ev.clean_labels, th.target_min_clean_labels),
            "bregman_candidates": row(ev.bregman_candidates, th.target_min_bregman_candidates),
            "bregman_certified": row(ev.bregman_certified, th.target_min_bregman_certified),
        }
        return CampaignProgress(targets=targets)

    # -- verdict ------------------------------------------------------------
    def verdict(self) -> CampaignVerdict:
        ev = self.evidence()
        th = self.thresholds
        prog = self.progress()
        freeze = bool(self.algorithm_freeze_mode)

        # minimum evidence for the expectancy hard-block: all count targets met
        has_min_evidence = (ev.decisions >= th.target_min_decisions
                            and ev.paper_trades >= th.target_min_paper_trades
                            and ev.resolved_labels >= th.target_min_resolved_labels)

        hard: list = []
        if th.require_no_live_orders and ev.live_orders > 0:
            hard.append("live_order_attempted")
        if th.require_no_risk_gate_violations and ev.risk_violations > 0:
            hard.append("risk_gate_violation")
        if ev.stale_data_confidence_improvement:
            hard.append("stale_data_confidence_improvement")
        if th.require_no_stale_data_violations and ev.stale_chainlink:
            hard.append("stale_chainlink")
        if th.require_no_stale_data_violations and ev.stale_book:
            hard.append("stale_order_book")
        if ev.bregman_false_positives > th.max_allowed_bregman_false_positives:
            hard.append("bregman_false_positive")
        if ev.partial_fill_hedge_breaks > th.max_allowed_partial_fill_hedge_breaks:
            hard.append("bregman_partial_fill_hedge_break")
        if th.require_clean_settlement_labels and ev.dirty_label_rate > th.max_dirty_label_rate:
            hard.append("dirty_labels")
        if th.require_algorithm_freeze and not freeze:
            hard.append("algorithm_not_frozen")
        if has_min_evidence:
            if th.require_after_cost_profitability and ev.after_cost_expectancy <= 0.0:
                hard.append("negative_after_cost_expectancy")
            if th.require_realistic_fill_profitability and ev.realistic_fill_expectancy <= 0.0:
                hard.append("negative_realistic_fill_expectancy")
                if ev.optimistic_expectancy > 0.0:
                    hard.append("optimistic_only_profitability")

        # soft (count + quality) shortfalls
        soft: list = []
        t = prog.targets
        if not t["runtime_days"]["met"]:
            soft.append("campaign_not_long_enough")
        if not t["runtime_hours"]["met"]:
            soft.append("insufficient_runtime")
        if not t["decisions"]["met"]:
            soft.append("insufficient_decisions")
        if not t["paper_trades"]["met"]:
            soft.append("insufficient_paper_trades")
        if not t["resolved_labels"]["met"]:
            soft.append("insufficient_resolved_labels")
        if not t["clean_labels"]["met"]:
            soft.append("insufficient_clean_labels")
        if not t["bregman_candidates"]["met"]:
            soft.append("insufficient_bregman_candidates")
        if not t["bregman_certified"]["met"]:
            soft.append("insufficient_bregman_certified")
        # quality shortfalls (prevent ready but are not hard-safety failures)
        calibration_regressed = (th.require_calibration_no_regression
                                 and ev.calibration_error
                                 > ev.baseline_calibration_error + th.calibration_regression_tol)
        if calibration_regressed:
            soft.append("calibration_regression")
        if ev.max_drawdown_pct > th.max_drawdown_pct:
            soft.append("excessive_drawdown")
        if ev.slippage_bps > th.max_slippage_bps:
            soft.append("excessive_slippage")
        if (th.require_no_stale_data_violations
                and ev.stale_data_rejection_rate > th.max_stale_data_rejection_rate):
            soft.append("stale_data_confidence_improvement")
        # advisory note: the separate 9-profile validation campaign hasn't been run
        notes: list = []
        validated = (ev.realistic_fill_expectancy > 0.0
                     and ev.resolved_labels >= th.target_min_resolved_labels)
        vc = ev.validation_campaign or {}
        if not vc.get("overall_ready") and not ev.replay_validation_ran:
            notes.append("replay_validation_missing")

        counts_met = all(t[k]["met"] for k in (
            "runtime_days", "runtime_hours", "decisions", "paper_trades",
            "resolved_labels", "clean_labels", "bregman_candidates", "bregman_certified"))
        quality_met = (
            (not th.require_after_cost_profitability or ev.after_cost_expectancy > 0.0)
            and (not th.require_realistic_fill_profitability or ev.realistic_fill_expectancy > 0.0)
            and not calibration_regressed
            and ev.max_drawdown_pct <= th.max_drawdown_pct
            and ev.slippage_bps <= th.max_slippage_bps
            and (not th.require_no_stale_data_violations
                 or ev.stale_data_rejection_rate <= th.max_stale_data_rejection_rate)
            and (not th.require_replay_or_realistic_fill_validation
                 or validated or ev.replay_validation_ran))

        # ---- state machine (evidence-driven; time alone never qualifies) ----
        if hard:
            state = VERDICT_BLOCKED
        elif not (counts_met and quality_met):
            state = VERDICT_CONTINUE
        else:
            micro_ok = (
                freeze and ev.after_cost_expectancy > 0.0 and ev.realistic_fill_expectancy > 0.0
                and ev.live_readiness_state in _LIVE_READY_STATES
                and ev.bregman_certified >= th.target_min_bregman_certified
                and ev.bregman_false_positives <= th.max_allowed_bregman_false_positives
                and ev.partial_fill_hedge_breaks <= th.max_allowed_partial_fill_hedge_breaks)
            state = VERDICT_MICRO_CANARY_READY if micro_ok else VERDICT_PAPER_QUALIFIED

        blockers = list(dict.fromkeys(hard + soft + notes))
        next_target = self._next_target(prog)
        reasons = []
        if state == VERDICT_BLOCKED:
            reasons.append("hard safety failure(s): " + ", ".join(hard))
        elif state == VERDICT_CONTINUE:
            reasons.append("evidence thresholds incomplete")
        elif state == VERDICT_PAPER_QUALIFIED:
            reasons.append("thresholds pass; micro-canary conditions not all met")
        else:
            reasons.append("all thresholds pass; readiness state live-ready")
        return CampaignVerdict(state=state, blockers=blockers, hard_safety_failures=hard,
                               next_target=next_target, reasons=reasons,
                               live_trading_enabled=False)

    @staticmethod
    def _next_target(prog: CampaignProgress) -> str:
        order = ("decisions", "paper_trades", "resolved_labels", "clean_labels",
                 "bregman_candidates", "bregman_certified", "runtime_days", "runtime_hours")
        for name in order:
            row = prog.targets.get(name)
            if row and not row["met"]:
                return f"{name}: {row['current']}/{row['target']}"
        return "all evidence targets met"

    def thresholds_met(self) -> bool:
        return self.verdict().state in (VERDICT_PAPER_QUALIFIED, VERDICT_MICRO_CANARY_READY)

    # -- reporting ----------------------------------------------------------
    def report(self) -> dict:
        ev = self.evidence()
        prog = self.progress()
        verdict = self.verdict()
        return {
            "enabled": True,
            "campaign_id": self.state.campaign_id,
            "campaign_name": self.state.campaign_name,
            "algorithm_freeze_mode": bool(self.algorithm_freeze_mode),
            "stop_requested": bool(self.state.stop_requested),
            "started_ts": self.state.started_ts,
            "last_update_ts": self.state.last_update_ts,
            "elapsed_days": ev.elapsed_days,
            "runtime_hours": ev.runtime_hours,
            "runs": ev.runs,
            "state": verdict.state,
            "verdict": verdict.to_dict(),
            "blockers": verdict.blockers,
            "next_target": verdict.next_target,
            "evidence": ev.to_dict(),
            "progress": prog.to_dict(),
            "thresholds": self.thresholds.to_dict(),
            "no_live_orders": ev.live_orders == 0,
            "note": "PAPER ONLY — this campaign never enables live trading.",
        }

    # -- persistence --------------------------------------------------------
    def mark_stop_requested(self) -> None:
        self.state.stop_requested = True
        if self.state_path or self.store:
            try:
                self.persist()
            except Exception:  # noqa: BLE001
                pass

    def persist(self) -> None:
        if self.state_path:
            payload = dict(self.state.to_dict())
            payload["thresholds"] = self.thresholds.to_dict()
            payload["report"] = self.report()
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(payload, indent=2, default=str),
                                       encoding="utf-8")
        if self.store is not None:
            try:
                self._persist_store()
            except Exception:  # noqa: BLE001 — SQLite persistence is best-effort
                pass

    def _persist_store(self) -> None:
        rep = self.report()
        ev = self.evidence()
        self.store.record_campaign(
            campaign_id=self.state.campaign_id, campaign_name=self.state.campaign_name,
            run_id=self.run_id or self.state.latest_run_id or "",
            started_ts_ms=int((self.state.started_ts or 0) * 1000),
            last_update_ts_ms=int((self.state.last_update_ts or 0) * 1000),
            status=rep["state"], algorithm_freeze_mode=bool(self.algorithm_freeze_mode),
            thresholds_json=json.dumps(self.thresholds.to_dict(), default=str),
            evidence_json=json.dumps(ev.to_dict(), default=str),
            verdict_json=json.dumps(rep["verdict"], default=str),
            blockers_json=json.dumps(rep["blockers"], default=str),
            progress_json=json.dumps(rep["progress"], default=str))
        self.store.record_campaign_snapshot(
            campaign_id=self.state.campaign_id,
            run_id=self.run_id or self.state.latest_run_id or "",
            status=rep["state"], evidence_json=json.dumps(ev.to_dict(), default=str),
            verdict_json=json.dumps(rep["verdict"], default=str),
            blockers_json=json.dumps(rep["blockers"], default=str))


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def campaign_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str, sort_keys=True)


def campaign_markdown(report: dict) -> str:
    r = report or {}
    ev = r.get("evidence", {}) or {}
    prog = (r.get("progress", {}) or {}).get("targets", {}) or {}
    L: list = []
    a = L.append
    a("# Institutional paper-training campaign")
    a("")
    a(f"- campaign: **{r.get('campaign_name')}**  ·  algorithm_freeze_mode: "
      f"{r.get('algorithm_freeze_mode')}  ·  no_live_orders: {r.get('no_live_orders')}")
    a(f"- elapsed: {ev.get('elapsed_days')}d / {ev.get('runtime_hours')}h  ·  runs: "
      f"{ev.get('runs')}")
    a("")
    a("## Verdict")
    a(f"- decision: **{r.get('state')}**  ·  live_trading_enabled: "
      f"{(r.get('verdict', {}) or {}).get('live_trading_enabled')}")
    a(f"- next evidence target: {r.get('next_target')}")
    a(f"- blockers: {', '.join(r.get('blockers', [])) or 'none'}")
    a("")
    a("## Evidence progress")
    a("| target | current | target | pct | met |")
    a("|---|---|---|---|---|")
    for name, row in prog.items():
        a(f"| {name} | {row.get('current')} | {row.get('target')} | {row.get('pct')}% | "
          f"{row.get('met')} |")
    a("")
    a("## Key metrics")
    a(f"- after-cost expectancy: {ev.get('after_cost_expectancy')}  ·  realistic-fill "
      f"expectancy: {ev.get('realistic_fill_expectancy')}")
    a(f"- Bregman: candidates={ev.get('bregman_candidates')} certified="
      f"{ev.get('bregman_certified')} false_positives={ev.get('bregman_false_positives')} "
      f"partial_fill_hedge_breaks={ev.get('partial_fill_hedge_breaks')}")
    a(f"- calibration_error={ev.get('calibration_error')} (baseline "
      f"{ev.get('baseline_calibration_error')})  ·  brier={ev.get('brier')} "
      f"ece={ev.get('ece')}")
    a(f"- risk_violations={ev.get('risk_violations')}  ·  live_orders="
      f"{ev.get('live_orders')}  ·  readiness_state={ev.get('live_readiness_state')}")
    a("")
    a("_PAPER ONLY. The campaign verdict never enables live trading._")
    return "\n".join(L) + "\n"
