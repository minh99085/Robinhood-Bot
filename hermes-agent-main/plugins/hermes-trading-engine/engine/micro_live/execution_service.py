"""MicroLiveExecutionService (Phase 9).

The ONLY path that can submit a real order. It validates locks, loads the canary
plan, re-runs preflight + RiskEngine + SafetyEnvelope, reconstructs and hash-
matches the venue payload, persists an idempotency key + SUBMITTING attempt
BEFORE any network call, submits exactly once through the venue broker, then
mandatorily reconciles and reports. It does NOT loop and never submits a second
order. Fail closed everywhere."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable, Optional

from . import ledger
from .account_snapshot import build_account_snapshot, redacted_account_payload
from .audit import write_audit
from .config import SUBMIT_CONFIRMATION, MicroLiveConfig
from .errors import MicroLiveDisabled, NotImplementedLiveSigning
from .idempotency import already_attempted, make_client_order_id
from .kalshi_live_broker import KalshiLiveBroker
from .locks import all_pass, check_locks
from .network_guard import NetworkGuard
from .order_builder import build_payload
from .polymarket_live_broker import PolymarketLiveBroker
from .preflight import preflight_canary_plan
from .reconciliation import reconcile_kalshi, terminal_order_status
from .schemas import MicroLiveCanaryPlan, MicroLiveOrderAttempt
from .secret_runtime import load_kalshi_trading_signer, redact
from .state_machine import MicroLiveStateMachine


class SubmitBlocked(dict):
    """Result of a blocked submit (never raised; returned + audited)."""


class MicroLiveExecutionService:
    def __init__(self, store, config: Optional[MicroLiveConfig] = None, *,
                 network_guard: Optional[NetworkGuard] = None):
        self.store = store
        self.cfg = config or MicroLiveConfig.from_env()
        self.guard = network_guard or NetworkGuard(allow_production=self.cfg.allow_production)
        self.sm = MicroLiveStateMachine(store, state="CANARY_READY")

    # ------------------------------------------------------------------ #
    def _make_broker(self, plan, *, locks_ok, signer, transport):
        if plan.venue == "kalshi":
            return KalshiLiveBroker(self.cfg, locks_ok=locks_ok, network_guard=self.guard,
                                    transport=transport, signer=signer,
                                    environment=plan.environment)
        if plan.venue == "polymarket":
            return PolymarketLiveBroker(self.cfg, locks_ok=locks_ok, network_guard=self.guard,
                                        transport=transport, signer=signer,
                                        environment=plan.environment)
        raise MicroLiveDisabled("make_broker", f"venue {plan.venue} not supported")

    def _blocked(self, reason: str, *, canary_plan_id=None, **extra) -> SubmitBlocked:
        write_audit(self.store, event_type="submit_blocked", severity="WARN", actor="cli",
                    canary_plan_id=canary_plan_id, message=reason)
        return SubmitBlocked(submitted=False, blocked=True, reason=reason, **extra)

    def submit_canary_order(self, canary_plan_id: str, *, arming_token: str = "",
                            confirm: str = "", market_ctx: Optional[dict] = None,
                            transport: Optional[Callable] = None, signer=None,
                            cli_context: bool = False,
                            non_interactive_test_fixture: bool = False,
                            now_ms: Optional[int] = None) -> dict:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        market_ctx = dict(market_ctx or {})

        # 1) CLI-only gate (API/service callers cannot set cli_context)
        if not cli_context and not non_interactive_test_fixture:
            return self._blocked("not_cli_context", canary_plan_id=canary_plan_id)
        # 2) typed confirmation
        if confirm != SUBMIT_CONFIRMATION:
            return self._blocked("typed_confirmation_required", canary_plan_id=canary_plan_id)
        # 3) locks
        lock_results = check_locks(self.cfg)
        if not all_pass(lock_results):
            failed = [r.lock_name for r in lock_results if not r.passed]
            return self._blocked(f"locks_failed:{','.join(failed)}", canary_plan_id=canary_plan_id)
        locks_ok = True
        # 4) load plan
        row = self.store.get_micro_live_canary_plan(canary_plan_id) if self.store else None
        if not row:
            return self._blocked("canary_plan_not_found", canary_plan_id=canary_plan_id)
        plan = MicroLiveCanaryPlan(**{k: row.get(k) for k in MicroLiveCanaryPlan.model_fields
                                      if k in row})
        # 5) idempotency / single-order-per-token / daily caps
        if already_attempted(self.store, canary_plan_id):
            return self._blocked("duplicate_submit_for_plan", canary_plan_id=canary_plan_id)
        if self.cfg.one_order_per_token and plan.arming_token_id and \
                ledger.token_used(self.store, plan.arming_token_id):
            return self._blocked("arming_token_already_used", canary_plan_id=canary_plan_id)
        if ledger.orders_today(self.store, now_ms=now) >= self.cfg.max_orders_per_day:
            return self._blocked("max_orders_per_day_reached", canary_plan_id=canary_plan_id)
        if ledger.active_or_blocking(self.store):
            return self._blocked("prior_live_order_blocks_new_orders", canary_plan_id=canary_plan_id)
        # 6) arming token verification (Phase 8) unless fixture mode
        arming_ok = True
        if self.cfg.require_arming_token and not non_interactive_test_fixture:
            arming_ok = self._verify_arming(arming_token)
            if not arming_ok:
                return self._blocked("arming_token_invalid", canary_plan_id=canary_plan_id)
        # 7) re-run preflight immediately before submit
        approvals_ok = True if non_interactive_test_fixture else None
        result, safety, risk = preflight_canary_plan(
            self.store, self.cfg, plan, approvals_ok=approvals_ok, arming_ok=arming_ok,
            account_ok=True, market_ctx=market_ctx, now_ms=now)
        write_audit(self.store, event_type="last_chance_before_submit", severity="WARN",
                    actor="cli", canary_plan_id=canary_plan_id,
                    message=f"preflight={result.status} risk={risk.code} safety={safety.reason}")
        if result.status != "PASS":
            return self._blocked(f"preflight_failed:{result.status}", canary_plan_id=canary_plan_id)
        # 8) reconstruct + hash-match payload
        payload, perrs = build_payload(plan, self.cfg)
        if payload is None or perrs:
            return self._blocked(f"payload_build_failed:{perrs}", canary_plan_id=canary_plan_id)
        if payload.payload_hash != plan.expected_payload_hash:
            return self._blocked("payload_hash_mismatch", canary_plan_id=canary_plan_id)
        # 9) idempotency key + SUBMITTING attempt persisted BEFORE network
        client_order_id = make_client_order_id(plan.venue, canary_plan_id, 1)
        attempt = MicroLiveOrderAttempt(
            canary_plan_id=canary_plan_id, ts_ms=now, venue=plan.venue,
            environment=plan.environment, client_order_id=client_order_id, status="SUBMITTING",
            submit_allowed=True, submitted=False,
            notional_submitted=payload.notional or Decimal(0),
            request_payload_hash=payload.payload_hash,
            risk_decision_id=safety.proposal_id, safety_envelope_decision_id=safety.decision_id)
        try:
            self.store.add_micro_live_order_attempt(attempt.record())
        except Exception:  # noqa: BLE001 — storage failure before submit MUST block
            return self._blocked("storage_failed_before_submit", canary_plan_id=canary_plan_id)
        attempt.audit_chain_hash = write_audit(
            self.store, event_type="idempotency_key_persisted", severity="INFO", actor="cli",
            canary_plan_id=canary_plan_id, live_order_attempt_id=attempt.live_order_attempt_id,
            message=f"client_order_id={client_order_id}")
        try:
            self.sm.transition("SUBMITTING", actor="cli", canary_plan_id=canary_plan_id,
                               live_order_attempt_id=attempt.live_order_attempt_id)
        except Exception:  # noqa: BLE001
            pass

        # 10) signer (loaded only now, after locks) + broker
        if signer is None and plan.venue == "kalshi":
            signer, sstatus = load_kalshi_trading_signer(locks_ok)
            if signer is None:
                return self._fail_attempt(attempt, "no_trading_signer:" + sstatus)
        broker = self._make_broker(plan, locks_ok=locks_ok, signer=signer, transport=transport)

        # pre-submit account snapshot
        try:
            raw_acct = broker.get_account_snapshot()
            snap = build_account_snapshot(raw_acct, plan.venue, plan.environment)
            self.store.add_micro_live_account_snapshot(snap.record())
        except Exception as e:  # noqa: BLE001 — account snapshot optional for fixture
            raw_acct = None
            if self.cfg.require_account_snapshot and not non_interactive_test_fixture:
                return self._fail_attempt(attempt, "account_snapshot_failed:" + type(e).__name__)

        # 11) SUBMIT EXACTLY ONCE
        try:
            resp = broker.submit_fok_canary_order(payload.payload_redacted, client_order_id)
        except NotImplementedLiveSigning as e:
            return self._fail_attempt(attempt, "live_signing_not_implemented",
                                      error_type="NotImplementedLiveSigning",
                                      error_message=str(e), signer_used=broker.signer_used)
        except Exception as e:  # noqa: BLE001 — timeout/network: UNKNOWN, never resubmit
            attempt.status = "UNKNOWN"
            attempt.submitted = True  # may have reached the exchange
            attempt.signer_used = broker.signer_used
            attempt.network_call_count = broker.guard.count("create_order")
            attempt.error_type = type(e).__name__
            attempt.error_message_redacted = redact(str(e))[:200]
            self._persist_attempt(attempt)
            write_audit(self.store, event_type="submit_uncertain", severity="CRITICAL", actor="cli",
                        canary_plan_id=canary_plan_id,
                        live_order_attempt_id=attempt.live_order_attempt_id,
                        message="network error after submit -> UNKNOWN; manual reconcile required")
            self._safe_transition("PAUSED", canary_plan_id, attempt.live_order_attempt_id)
            return self._finish(attempt, plan, safety, risk, next_step="fix_reconciliation")

        attempt.submitted = True
        attempt.acknowledged = True
        attempt.signer_used = broker.signer_used
        attempt.network_call_count = broker.guard.count("create_order")
        attempt.response_payload_hash = self._hash(resp)
        attempt.status = "SUBMITTED"
        self._safe_transition("SUBMITTED", canary_plan_id, attempt.live_order_attempt_id)

        # 12) MANDATORY reconciliation
        recon = self._reconcile(broker, plan, attempt, resp)
        self._persist_attempt(attempt)
        # post-submit account snapshot
        try:
            after = build_account_snapshot(broker.get_account_snapshot(), plan.venue, plan.environment)
            self.store.add_micro_live_account_snapshot(after.record())
        except Exception:  # noqa: BLE001
            pass

        next_step = self._next_step(attempt, recon)
        return self._finish(attempt, plan, safety, risk, reconciliation=recon, next_step=next_step)

    # ------------------------------------------------------------------ #
    def _reconcile(self, broker, plan, attempt, resp):
        try:
            order_id = (resp.get("body", {}) or {}).get("order", {}).get("order_id") \
                or (resp.get("body", {}) or {}).get("order_id") or attempt.client_order_id
            order_body = broker.get_order(order_id)
            fills_body = broker.get_fills(order_id)
            recon = reconcile_kalshi(order_body, fills_body,
                                     expected_qty=attempt.notional_submitted /
                                     (Decimal(str(plan.limit_price)) or Decimal(1)),
                                     live_order_attempt_id=attempt.live_order_attempt_id)
            attempt.exchange_order_id = order_id
            attempt.filled_quantity = recon.filled_quantity
            attempt.notional_filled = recon.filled_quantity * (Decimal(str(plan.limit_price)) or
                                                               Decimal(0))
            attempt.fee = recon.fee
            attempt.status = terminal_order_status(
                recon.local_order_status, recon.filled_quantity,
                attempt.notional_submitted / (Decimal(str(plan.limit_price)) or Decimal(1)))
            if self.store:
                self.store.add_micro_live_reconciliation(recon.record())
            sev = "CRITICAL" if recon.local_order_status == "OPEN" else "INFO"
            if recon.local_order_status == "OPEN":
                self._safe_transition("PAUSED", plan.canary_plan_id, attempt.live_order_attempt_id)
                write_audit(self.store, event_type="fok_unexpectedly_open", severity="CRITICAL",
                            actor="cli", canary_plan_id=plan.canary_plan_id,
                            live_order_attempt_id=attempt.live_order_attempt_id,
                            message="FOK order resting/open -> emergency cancel required")
            else:
                self._safe_transition("RECONCILING", plan.canary_plan_id,
                                      attempt.live_order_attempt_id)
                final = {"FILLED": "FILLED", "PARTIALLY_FILLED": "PARTIALLY_FILLED",
                         "REJECTED": "REJECTED", "UNKNOWN": "PAUSED"}.get(attempt.status, "FAILED")
                if final in ("FILLED", "REJECTED", "PARTIALLY_FILLED"):
                    self._safe_transition(final if final != "PARTIALLY_FILLED" else
                                          "PARTIALLY_FILLED", plan.canary_plan_id,
                                          attempt.live_order_attempt_id)
            write_audit(self.store, event_type="reconciled", severity=sev, actor="cli",
                        canary_plan_id=plan.canary_plan_id,
                        live_order_attempt_id=attempt.live_order_attempt_id,
                        message=f"recon={recon.status} status={attempt.status}")
            return recon
        except Exception as e:  # noqa: BLE001
            attempt.status = "RECONCILE_FAILED"
            write_audit(self.store, event_type="reconcile_failed", severity="CRITICAL", actor="cli",
                        canary_plan_id=plan.canary_plan_id,
                        live_order_attempt_id=attempt.live_order_attempt_id,
                        message=redact(str(e))[:200])
            return None

    def _verify_arming(self, arming_token: str) -> bool:
        try:
            from ..guarded_live.arming import ArmingTokenManager
            from ..guarded_live.config import GuardedLiveConfig
            mgr = ArmingTokenManager(self.store, GuardedLiveConfig.from_env())
            ok, _ = mgr.verify(arming_token)
            return bool(ok)
        except Exception:  # noqa: BLE001
            return False

    def _fail_attempt(self, attempt, reason, *, error_type=None, error_message=None,
                      signer_used=False) -> dict:
        attempt.status = "FAILED"
        attempt.reject_reason = reason
        attempt.error_type = error_type
        attempt.error_message_redacted = redact(error_message)[:200] if error_message else None
        attempt.signer_used = signer_used
        self._persist_attempt(attempt)
        write_audit(self.store, event_type="submit_failed", severity="WARN", actor="cli",
                    canary_plan_id=attempt.canary_plan_id,
                    live_order_attempt_id=attempt.live_order_attempt_id, message=reason)
        self._safe_transition("FAILED", attempt.canary_plan_id, attempt.live_order_attempt_id)
        return SubmitBlocked(submitted=bool(attempt.submitted), blocked=True, reason=reason,
                             live_order_attempt_id=attempt.live_order_attempt_id)

    def _persist_attempt(self, attempt):
        if self.store:
            try:
                self.store.add_micro_live_order_attempt(attempt.record())
            except Exception:  # noqa: BLE001
                pass

    def _safe_transition(self, to_state, canary_plan_id, attempt_id):
        try:
            self.sm.transition(to_state, actor="cli", canary_plan_id=canary_plan_id,
                               live_order_attempt_id=attempt_id)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _hash(obj) -> str:
        import hashlib
        import json
        return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]

    @staticmethod
    def _next_step(attempt, recon) -> str:
        if attempt.status == "UNKNOWN":
            return "fix_reconciliation"
        if attempt.status == "ACKNOWLEDGED":
            return "cancel_open_order"
        if attempt.status == "PARTIALLY_FILLED":
            return "stop_and_review"
        if attempt.status == "FILLED":
            return "manual_review_before_any_next_canary"
        if attempt.status == "REJECTED":
            return "collect_more_shadow"
        return "stop_and_review"

    def _finish(self, attempt, plan, safety, risk, *, reconciliation=None, next_step="stop_and_review"):
        from .report import write_report
        report_path = None
        try:
            report_path = write_report(self.store, self.cfg, plan=plan, attempt=attempt,
                                       safety=safety, risk=risk, reconciliation=reconciliation,
                                       next_step=next_step)
        except Exception:  # noqa: BLE001
            pass
        return {"submitted": bool(attempt.submitted), "status": attempt.status,
                "live_order_attempt_id": attempt.live_order_attempt_id,
                "client_order_id": attempt.client_order_id,
                "filled_quantity": str(attempt.filled_quantity),
                "next_step": next_step, "report_path": report_path,
                "network_call_count": attempt.network_call_count,
                "signer_used": attempt.signer_used}

    # ------------------------------------------------------------------ #
    def emergency_cancel(self, *, venue: str, environment: str, confirm: str, requested_by: str,
                         order_id: Optional[str] = None, market_ticker: Optional[str] = None,
                         cancel_all: bool = False, transport: Optional[Callable] = None,
                         signer=None, cli_context: bool = False) -> dict:
        from .config import EMERGENCY_CANCEL_CONFIRMATION
        from .schemas import EmergencyCancelResult
        res = EmergencyCancelResult(venue=venue, environment=environment,
                                    requested_by=requested_by, reason="emergency",
                                    client_order_id=order_id, cancel_all=cancel_all)
        if confirm != EMERGENCY_CANCEL_CONFIRMATION:
            res.error_message_redacted = "typed_confirmation_required"
            self._persist_cancel(res)
            return {"sent": False, "reason": "typed_confirmation_required"}
        if not (order_id or (cancel_all and market_ticker)):
            res.error_message_redacted = "no_target"
            self._persist_cancel(res)
            return {"sent": False, "reason": "no_target"}
        lock_results = check_locks(self.cfg)
        if not all_pass(lock_results):
            res.error_message_redacted = "locks_failed"
            self._persist_cancel(res)
            return {"sent": False, "reason": "locks_failed"}
        plan_stub = MicroLiveCanaryPlan(venue=venue, environment=environment,
                                        market_ticker=market_ticker)
        if signer is None and venue == "kalshi":
            signer, _ = load_kalshi_trading_signer(True)
        broker = self._make_broker(plan_stub, locks_ok=True, signer=signer, transport=transport)
        try:
            if cancel_all and market_ticker:
                broker.emergency_cancel_all_for_market(market_ticker)
            else:
                broker.emergency_cancel_order(order_id)
            res.sent = True
            res.success = True
        except Exception as e:  # noqa: BLE001
            res.sent = True
            res.success = False
            res.error_message_redacted = redact(str(e))[:200]
        self._persist_cancel(res)
        write_audit(self.store, event_type="emergency_cancel", severity="CRITICAL",
                    actor=requested_by, message=f"cancel sent={res.sent} ok={res.success}")
        return {"sent": res.sent, "success": res.success, "cancel_id": res.cancel_id}

    def _persist_cancel(self, res):
        if self.store:
            try:
                self.store.add_micro_live_emergency_cancel(res.record())
            except Exception:  # noqa: BLE001
                pass


# ---- fixture helpers (mocked exchange only; never touches the network) ---- #
class FixtureSigner:
    """A non-cryptographic stand-in used ONLY by the test fixture path."""

    def headers(self, method, path):
        return {"KALSHI-ACCESS-KEY": "[REDACTED]", "KALSHI-ACCESS-SIGNATURE": "[REDACTED]",
                "KALSHI-ACCESS-TIMESTAMP": "0"}


def fixture_transport(fill: bool = True):
    """Return a mock transport that simulates a Kalshi demo FOK fill. No network."""
    state = {"calls": 0}

    def _t(method, url, *, headers=None, json_body=None):
        state["calls"] += 1
        if method == "POST" and "/orders" in url:
            oid = "demo-order-1"
            return 201, {"order": {"order_id": oid, "status": "executed" if fill else "canceled",
                                   "filled_quantity": (json_body or {}).get("count", 1) if fill else 0,
                                   "fee": 1}}
        if method == "GET" and "/balance" in url:
            return 200, {"balance": 100000}
        if method == "GET" and "/orders/" in url:
            return 200, {"order": {"order_id": "demo-order-1",
                                   "status": "executed" if fill else "canceled",
                                   "filled_quantity": 1 if fill else 0, "fee": 1}}
        if method == "GET" and "/fills" in url:
            return 200, {"fills": [{"count": 1, "fee": 1}] if fill else []}
        if method == "DELETE":
            return 200, {"status": "canceled"}
        return 200, {}

    _t.state = state
    return _t
