"""ShadowOrchestrator — runs the full live decision stack WITHOUT trading.

Per market: select candidate → (cached/online) research → deterministic decision
→ RiskEngine → (if approved + allowed) ShadowOMS/PaperBroker → schedule outcome
observations. Every step is persisted and auditable. Fails closed on kill switch,
CRITICAL alerts, reconciliation failure, or degraded venue status.

It NEVER calls a real order/cancel endpoint, live broker, wallet, or private
user channel.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from ..risk import RiskContext, RiskEngine
from .alerts import AlertManager
from .candidate_selector import ShadowCandidateSelector
from .config import ShadowConfig
from .decision_engine import ShadowDecisionEngine
from .outcome_tracker import ShadowOutcomeTracker
from .schemas import SHADOW_MODE, ShadowDecision, ShadowSession
from .shadow_oms import ShadowOMS


class ShadowOrchestrator:
    def __init__(self, store=None, config: Optional[ShadowConfig] = None,
                 registry=None, research=None, risk: Optional[RiskEngine] = None,
                 broker=None, clock=None):
        self.store = store
        self.cfg = config or ShadowConfig.from_env()
        self.registry = registry
        self.research = research
        self.risk = risk or RiskEngine()
        self.broker = broker
        self.now_ms = clock or (lambda: int(time.time() * 1000))
        self.selector = ShadowCandidateSelector(self.cfg)
        self.decision_engine = ShadowDecisionEngine(self.cfg)
        self.tracker = ShadowOutcomeTracker(self.cfg, store)
        self.alerts: Optional[AlertManager] = None
        self.oms: Optional[ShadowOMS] = None
        self.session: Optional[ShadowSession] = None
        self.reconciliation_clean = True
        self.degraded = False
        self.counters = {
            "risk_bypass_count": 0, "unhandled_exception_count": 0,
            "live_order_endpoint_calls": 0, "secret_leak_count": 0,
            "orders_without_risk": 0, "orders_outside_oms": 0,
        }

    # ------------------------------------------------------------------ #
    def start(self, notes: Optional[str] = None) -> tuple[bool, object]:
        ok, reason = self.cfg.verify_safe_to_start()
        if not ok:
            return False, reason
        sid_name = self.cfg.session_name or ""
        session = ShadowSession(
            status="RUNNING", started_ts_ms=self.now_ms(), config_hash=self.cfg.config_hash(),
            venues=list(self.cfg.venues), mode=SHADOW_MODE, notes=notes or sid_name or None)
        self.session = session
        self.alerts = AlertManager(self.store, session.shadow_session_id)
        self.oms = ShadowOMS(self.store, self.broker, session.shadow_session_id)
        if self.store is not None:
            self.store.upsert_shadow_session({
                "shadow_session_id": session.shadow_session_id, "status": session.status,
                "started_ts_ms": session.started_ts_ms, "stopped_ts_ms": None,
                "config_hash": session.config_hash, "config_json": self.cfg.public_dict(),
                "venues_json": session.venues, "mode": SHADOW_MODE, "notes": session.notes})
            self._heartbeat()
        return True, session

    def new_orders_allowed(self) -> bool:
        if self.cfg.kill_switch_active():
            return False
        if self.alerts is not None and self.alerts.paused:
            return False
        if self.cfg.require_reconciliation_clean and not self.reconciliation_clean:
            return False
        if self.degraded:
            return False
        return True

    # ------------------------------------------------------------------ #
    def process_market(self, inp: dict, cycle_id: str = "c") -> ShadowDecision:
        """Run one market through the full stack. Errors are contained: an ERROR
        decision is persisted and an alert emitted, never an exception escape."""
        sid = self.session.shadow_session_id if self.session else ""
        try:
            return self._process_market(inp, cycle_id, sid)
        except Exception as e:  # noqa: BLE001
            self.counters["unhandled_exception_count"] += 1
            dec = ShadowDecision(shadow_session_id=sid, cycle_id=cycle_id,
                                 venue=(inp.get("candidate", {}) or {}).get("venue", "polymarket"),
                                 decision="ERROR", reason=str(e)[:200], ts_ms=self.now_ms())
            self._persist_decision(dec)
            if self.alerts is not None:
                self.alerts.emit("ERROR", "storage_failure", f"process_market error: {e}")
            return dec

    def _process_market(self, inp: dict, cycle_id: str, sid: str) -> ShadowDecision:
        if self.cfg.kill_switch_active():
            dec = ShadowDecision(shadow_session_id=sid, cycle_id=cycle_id,
                                 venue=(inp.get("candidate", {}) or {}).get("venue", "polymarket"),
                                 decision="ABSTAINED", reason="kill_switch", ts_ms=self.now_ms())
            self._persist_decision(dec)
            return dec

        cand = self.selector.evaluate(now_ms=self.now_ms(), **(inp.get("candidate") or {}))
        cand.shadow_session_id = sid
        if self.store is not None:
            self.store.add_shadow_candidate(cand.record())
        if not cand.selected:
            dec = ShadowDecision(
                shadow_session_id=sid, cycle_id=cycle_id, venue=cand.venue,
                market_id=cand.market_id, market_ticker=cand.market_ticker,
                asset_id=cand.asset_id, outcome=cand.outcome, decision="ABSTAINED",
                reason=cand.rejection_reason or "not_selected", ts_ms=self.now_ms())
            self._persist_decision(dec)
            return dec

        dec, proposal, rd = self.decision_engine.decide(
            cand, best_bid=inp.get("best_bid"), best_ask=inp.get("best_ask"),
            spread=inp.get("spread"), midpoint=inp.get("midpoint"),
            research=inp.get("research"), risk_engine=self.risk,
            risk_context=inp.get("risk_context"), cycle_id=cycle_id, now_ms=self.now_ms())
        self._persist_decision(dec)

        if dec.decision == "APPROVED_SHADOW" and proposal is not None:
            if not self.new_orders_allowed():
                dec.reason = "approved_but_orders_paused"
                self._persist_decision(dec)
                return dec
            order = self.oms.submit(
                proposal, dec, book=inp.get("book"), reference_price=inp.get("reference_price"),
                venue_kind=inp.get("venue_kind", "pm"), now_ms=self.now_ms())
            self._schedule_observations(dec, order, inp)
        return dec

    def _schedule_observations(self, dec, order, inp) -> None:
        # In a live session these fire at future horizons; here we record the
        # immediate (horizon 0) observation deterministically.
        fill_price = None
        fills = getattr(order, "_fills", [])
        if fills:
            fill_price = fills[0].price
        self.tracker.observe(dec, horizon_ms=0, best_bid=inp.get("best_bid"),
                             best_ask=inp.get("best_ask"), fill_price=fill_price,
                             shadow_order_id=order.shadow_order_id, now_ms=self.now_ms())

    def _persist_decision(self, dec: ShadowDecision) -> None:
        if self.store is not None:
            try:
                self.store.add_shadow_decision(dec.record())
            except Exception:  # noqa: BLE001 — fail closed: surface but do not crash
                self.counters["unhandled_exception_count"] += 1

    def _heartbeat(self) -> None:
        if self.store is None or self.session is None:
            return
        try:
            self.store.upsert_shadow_heartbeat({
                "shadow_session_id": self.session.shadow_session_id, "ts_ms": self.now_ms(),
                "status": self.session.status, "cycle_count": 0,
                "last_cycle_ts_ms": self.now_ms(), "last_error": None,
                "venue_status_json": {}})
        except Exception:  # noqa: BLE001
            pass

    def heartbeat(self, cycle_count: int = 0, last_error: Optional[str] = None) -> None:
        if self.store is None or self.session is None:
            return
        self.store.upsert_shadow_heartbeat({
            "shadow_session_id": self.session.shadow_session_id, "ts_ms": self.now_ms(),
            "status": self.session.status, "cycle_count": cycle_count,
            "last_cycle_ts_ms": self.now_ms(), "last_error": last_error,
            "venue_status_json": {}})

    def pause(self) -> None:
        if self.session:
            self.session.status = "PAUSED"
            self._update_status("PAUSED")

    def resume(self) -> None:
        if self.session:
            self.session.status = "RUNNING"
            if self.alerts:
                self.alerts.resume()
            self._update_status("RUNNING")

    def _update_status(self, status: str) -> None:
        if self.store is not None and self.session is not None:
            self.store.update_shadow_session(self.session.shadow_session_id, {"status": status})

    def stop(self, failed: bool = False) -> Optional[ShadowSession]:
        if self.session is None:
            return None
        if self.oms is not None:
            self.oms.cancel_open(self.now_ms())
        self.session.status = "FAILED" if failed else "STOPPED"
        self.session.stopped_ts_ms = self.now_ms()
        if self.store is not None:
            self.store.update_shadow_session(self.session.shadow_session_id, {
                "status": self.session.status, "stopped_ts_ms": self.session.stopped_ts_ms})
        return self.session
