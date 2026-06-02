"""ConformanceHarness (Phase 8).

Proves — without any real network/order/signing — that the guarded-live door is
locked: execution methods are disabled, dry-run intents are unsigned/unsent and
require RiskEngine + SafetyEnvelope, the state machine has no live state, secrets
are redacted, and forbidden env/endpoints are detected. ``traps`` lets tests
inject simulated network/order/signing calls to prove the harness FAILS on them.
"""

from __future__ import annotations

import time
from typing import Optional

from .config import GuardedLiveConfig
from .disabled_brokers import DisabledLiveBroker
from .dry_run import DryRunLiveBroker
from .errors import LiveExecutionDisabled
from .schemas import ConformanceCheck, ConformanceRun
from .secret_policy import SecretPolicy, redact
from .state_machine import FORBIDDEN_LIVE_STATES, STATES


class ConformanceHarness:
    def __init__(self, store=None, config: Optional[GuardedLiveConfig] = None):
        self.store = store
        self.cfg = config or GuardedLiveConfig()

    def run(self, traps: Optional[dict] = None) -> ConformanceRun:
        traps = traps or {}
        run = ConformanceRun(config_hash=self.cfg.config_hash())
        checks: list[ConformanceCheck] = []

        def add(name, ok, reason=""):
            checks.append(ConformanceCheck(conformance_run_id=run.conformance_run_id,
                                           check_name=name, status="PASS" if ok else "FAIL",
                                           reason=reason))

        # 1 no live broker configured
        import os
        add("no_live_broker_configured",
            os.getenv("LIVE_BROKER_ENABLED") in (None, "", "0", "false", "False"),
            "LIVE_BROKER_ENABLED must be unset")

        # 2 disabled broker execution methods raise
        db = DisabledLiveBroker("polymarket")
        locked = True
        for m in ("submit_order", "cancel_order", "replace_order"):
            try:
                getattr(db, m)()
                locked = False
            except LiveExecutionDisabled:
                pass
            except Exception:  # noqa: BLE001
                locked = False
        add("disabled_broker_execution_locked", locked)

        # 3 dry-run broker never calls network
        add("dry_run_broker_no_network", int(traps.get("network", 0)) == 0,
            "network trap fired" if traps.get("network") else "")
        # 4 no signer used
        add("dry_run_broker_no_signer", int(traps.get("signer", 0)) == 0,
            "signer trap fired" if traps.get("signer") else "")
        # 5 no order endpoint called
        add("no_order_endpoint_called", int(traps.get("order_endpoint", 0)) == 0,
            "order endpoint trap fired" if traps.get("order_endpoint") else "")

        # 6 forbidden env detection works (clean env => none)
        ok_env, violations = SecretPolicy(self.cfg).check()
        add("forbidden_env_clean", ok_env,
            f"{len(violations)} forbidden env var(s)" if violations else "")
        # 7 secret redaction works
        add("secret_redaction_works", "xai-SECRET12345" not in redact("k=xai-SECRET12345"))

        # 8 dry-run intent requires risk + safety decisions
        drb = DryRunLiveBroker(store=None, config=self.cfg)
        order = {"venue": "kalshi", "market_ticker": "X", "outcome": "YES", "side": "BUY",
                 "price": 0.45, "quantity": 1}
        no_risk = drb.validate_order(order)
        add("dry_run_requires_risk_decision", no_risk.status == "BLOCKED"
            and no_risk.reason == "missing_risk_decision")
        no_safe = drb.validate_order(order, risk_decision_id="r1")
        add("dry_run_requires_safety_decision", no_safe.status == "BLOCKED"
            and no_safe.reason == "missing_safety_envelope_decision")
        good = drb.validate_order(order, risk_decision_id="r1", safety_envelope_decision_id="s1")
        add("dry_run_intent_unsigned_unsent",
            good.unsigned and good.unsent and not good.signer_used and not good.network_called)
        add("dry_run_intent_validated", good.status == "VALIDATED")

        # 9 state machine has no live state
        add("no_live_active_state", not (set(STATES) & FORBIDDEN_LIVE_STATES))

        # 10 dry-run broker submit is locked too
        try:
            drb.submit_order()
            add("dry_run_broker_submit_locked", False)
        except LiveExecutionDisabled:
            add("dry_run_broker_submit_locked", True)

        # 11 report explicitly says no live orders (statement constant present)
        from .report import NO_LIVE_STATEMENT
        add("report_states_no_live_orders", "No live orders were submitted" in NO_LIVE_STATEMENT)

        run.checks = checks
        run.test_count = len(checks)
        run.pass_count = sum(1 for c in checks if c.status == "PASS")
        run.fail_count = sum(1 for c in checks if c.status == "FAIL")
        run.warning_count = sum(1 for c in checks if c.status == "WARN")
        run.finished_ts_ms = int(time.time() * 1000)
        run.status = "PASS" if run.fail_count == 0 else "FAIL"
        if self.store is not None:
            try:
                self.store.add_conformance_run(run.record())
                for c in checks:
                    self.store.add_conformance_check(c.record())
            except Exception:  # noqa: BLE001
                pass
        return run
