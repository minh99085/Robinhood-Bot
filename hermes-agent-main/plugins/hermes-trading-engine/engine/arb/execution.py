"""ArbExecutionEngine — closes the loop: detect -> pre-flight -> Grok -> execute.

Polls the detector, runs the 7-check pre-flight, asks Grok for approval (Grok
refines but never blocks a structurally-sound arb except an explicit veto),
places both legs CONCURRENTLY, and runs the 4-step leg-2 failure recovery. All
PAPER. Respects trading mode + circuit breaker.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .symbol_map import EXCHANGES

# ---------------------------------------------------------------------------
# Cross-exchange arbitrage is PERMANENTLY DISABLED.
#
# The engine is now a Polymarket-only PAPER training machine (no crypto
# arbitrage). This module is retained only so historical imports/snapshots keep
# working; the execution loop never starts and no (even simulated) arb order is
# ever placed. The disable is a hard module constant — it cannot be re-enabled
# via ARB_EXECUTION_ENABLED, the API toggle, or the dashboard.
# ---------------------------------------------------------------------------
ARBITRAGE_PERMANENTLY_DISABLED = True
ARBITRAGE_DISABLED_REASON = "arbitrage removed — Polymarket-only PAPER training"


def _settle(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:120]}


class ArbExecutionEngine:
    def __init__(self, *, detector, gateway, ledger, feeds, universe, brain,
                 get_mode, circuit, get_market_context=None, risk_gate=None):
        self.detector = detector
        self.gateway = gateway
        self.ledger = ledger
        self.feeds = feeds
        self.universe = universe
        self.brain = brain
        self.get_mode = get_mode
        self.circuit = circuit
        self.get_market_context = get_market_context or (lambda: {})
        # Deterministic risk gate: callable(opp, size) -> RiskDecision. Every
        # arb (paper) execution must clear this in addition to the 7-check
        # pre-flight. Grok approval can only VETO; it can never bypass risk.
        self.risk_gate = risk_gate

        # PERMANENTLY OFF. ARB_EXECUTION_ENABLED is ignored on purpose: arbitrage
        # trading has been removed in favour of Polymarket-only PAPER training.
        self.enabled = False
        self.disabled_reason = ARBITRAGE_DISABLED_REASON
        self.min_trade = float(os.getenv("ARB_MIN_TRADE_USD", "25"))
        self.max_trade_paper = float(os.getenv("ARB_MAX_TRADE_USD", "500"))
        self.max_trade_live = float(os.getenv("ARB_MAX_TRADE_USD_LIVE", "200"))
        self.min_exec_net = float(os.getenv("ARB_MIN_EXECUTION_NET_PCT", "0.25"))
        self.leg2_retry_timeout = float(os.getenv("ARB_LEG2_RETRY_TIMEOUT_MS", "1000")) / 1000.0
        self.pause_after_incident = float(os.getenv("ARB_PAUSE_AFTER_INCIDENT_MS", "60000")) / 1000.0
        self.scan_interval = float(os.getenv("ARB_SCAN_INTERVAL_S", "12"))
        self.max_staleness_ms = 2000.0

        self._stop = threading.Event()
        self._thread = None
        self.last_opps: list[dict] = []
        self.last_incident: dict | None = None
        self.last_skip: str | None = None
        self.paused_until = 0.0

    # ------------------------------------------------------------------
    def start(self):
        # Permanently disabled: never spawn the scan/execute loop.
        if ARBITRAGE_PERMANENTLY_DISABLED:
            self.enabled = False
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="arb-exec", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _max_trade(self) -> float:
        return self.max_trade_live if self.get_mode() == "live" else self.max_trade_paper

    def _loop(self):
        self._stop.wait(5.0)
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as exc:  # noqa: BLE001
                self.last_skip = f"cycle error: {str(exc)[:120]}"
            self._stop.wait(self.scan_interval)

    def _cycle(self):
        if not self.enabled or time.time() < self.paused_until:
            return
        opps = self.detector.scan()
        self.last_opps = opps[:8]
        if not opps:
            return
        opp = opps[0]
        pf = self.run_preflight(opp)
        if not pf["pass"]:
            self.last_skip = f"{opp['symbol']}: {pf['failReason']}"
            return
        size = pf["size"]
        approval = self._grok_approve(opp)
        if approval is not None:
            # Grok may only VETO. Its suggestedSizeUSD is advisory and is NOT
            # applied — Grok never sets order size (safety requirement).
            if approval.get("approved") is False:
                self.last_skip = f"{opp['symbol']}: Grok veto - {approval.get('reason','')}"
                return
        # Deterministic RiskEngine has the final say before any (paper) order.
        if self.risk_gate is not None:
            decision = self.risk_gate(opp, size)
            if decision is not None and not getattr(decision, "approved", False):
                reason = "; ".join(getattr(decision, "reasons", []) or [])[:120]
                self.last_skip = f"{opp['symbol']}: risk {getattr(decision, 'code', '')} - {reason}"
                return
        self._execute(opp, size, approval)

    # ------------------------------------------------------------------
    # PRE-FLIGHT: all 7 must pass
    # ------------------------------------------------------------------
    def run_preflight(self, opp: dict) -> dict:
        sym = opp["symbol"]
        if self.get_mode() == "live" and self.circuit.halted:
            return {"pass": False, "failReason": "CIRCUIT_BREAKER_ACTIVE"}
        if opp.get("staleness_ms", 1e9) >= self.max_staleness_ms:
            return {"pass": False, "failReason": "STALE_DATA"}
        if opp.get("executionNetPct", -1) <= self.min_exec_net:
            return {"pass": False, "failReason": "NET_BELOW_EXECUTION_BAR"}
        if not opp.get("simulated"):
            for ex in (opp["buyExchange"], opp["sellExchange"]):
                t = self.feeds.get_latest_tick(ex, sym)
                if not t or (time.time() - t["ts"]) * 1000 > self.max_staleness_ms:
                    return {"pass": False, "failReason": f"NO_FRESH_TICK_{ex}"}
        if not self.universe.is_active(sym):
            return {"pass": False, "failReason": "NOT_IN_ACTIVE_UNIVERSE"}
        buy_usd = self.gateway.get_balance(opp["buyExchange"]).get("USD", 0.0)
        size = min(buy_usd, self._max_trade())
        if size < self.min_trade:
            return {"pass": False, "failReason": "INSUFFICIENT_BALANCE"}
        if self.ledger.is_open_trade(sym):
            return {"pass": False, "failReason": "DUPLICATE_OPEN"}
        return {"pass": True, "size": round(size, 2)}

    # ------------------------------------------------------------------
    def _grok_approve(self, opp: dict) -> dict | None:
        if not getattr(self.brain, "enabled", False):
            return None
        payload = {
            "task": "arb_approval",
            "opportunity": {k: opp.get(k) for k in
                            ("symbol", "buyExchange", "buyAsk", "sellExchange", "sellBid",
                             "grossPct", "netPct", "executionNetPct", "estimatedProfit_1k",
                             "staleness_ms", "tier")},
            "marketContext": self.get_market_context() or {},
            "respond": {"approved": "true|false", "confidence": "0-1",
                        "reason": "<=80 chars", "suggestedSizeUSD": "number"},
        }
        try:
            raw = self.brain._chat_json(payload, timeout=max(self.brain.timeout_s, 4.0))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(raw, dict):
            return None
        try:
            conf = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return {"approved": raw.get("approved") in (True, "true", "True", 1) if raw.get("approved") is not None else True,
                "confidence": conf, "reason": str(raw.get("reason", ""))[:80],
                "suggestedSizeUSD": raw.get("suggestedSizeUSD")}

    # ------------------------------------------------------------------
    # DUAL-LEG EXECUTION (both legs concurrent)
    # ------------------------------------------------------------------
    def _execute(self, opp: dict, size: float, approval: dict | None):
        sym, buy_ex, sell_ex = opp["symbol"], opp["buyExchange"], opp["sellExchange"]
        self.ledger.mark_open(sym)
        t0 = time.time()
        est_qty = (size / max(opp["buyAsk"], 1e-9)) * 0.98  # 98% to absorb leg-1 fee
        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(lambda: _settle(self.gateway.place_order, buy_ex, sym, "BUY", usd=size))
                f2 = ex.submit(lambda: _settle(self.gateway.place_order, sell_ex, sym, "SELL", qty=est_qty))
                leg1, leg2 = f1.result(), f2.result()
        except Exception as exc:  # noqa: BLE001
            self.last_incident = {"symbol": sym, "error": str(exc)[:120], "ts": time.time()}
            self.ledger.mark_closed(sym)
            return
        latency_ms = round((time.time() - t0) * 1000, 1)
        if latency_ms > 500:
            self.last_skip = f"{sym}: slow execution {latency_ms}ms"

        if leg1.get("ok") and leg2.get("ok"):
            self._record(opp, leg1, leg2, size, latency_ms, approval)
        elif not leg1.get("ok") and not leg2.get("ok"):
            if self.get_mode() == "live":
                self.circuit.record_api(False)
            self.last_skip = f"{sym}: both legs failed"
        elif leg1.get("ok") and not leg2.get("ok"):
            self._handle_leg2_failure(opp, leg1, size, est_qty, latency_ms, approval)
        else:
            self._handle_leg1_failure(opp, leg2)
        self.ledger.mark_closed(sym)

    def _profit(self, opp, leg1, leg2, size):
        coins = leg1.get("fillQty", 0.0) or 0.0
        sold = min(leg2.get("fillQty", 0.0) or 0.0, coins) if coins else (leg2.get("fillQty", 0.0) or 0.0)
        proceeds_net = sold * (leg2.get("fillPrice", 0.0) or 0.0) - (leg2.get("fee", 0.0) or 0.0)
        leftover = max(0.0, coins - sold) * (leg2.get("fillPrice", 0.0) or 0.0)
        profit = proceeds_net + leftover - size
        lp = leg1.get("fillPrice", 1) or 1
        gross_actual = ((leg2.get("fillPrice", 0) or 0) - lp) / max(lp, 1e-9) * 100.0
        net_actual = profit / size * 100.0 if size else 0.0
        return round(profit, 4), round(gross_actual, 4), round(net_actual, 4)

    def _record(self, opp, leg1, leg2, size, latency_ms, approval, incident_type=None):
        profit, gross_actual, net_actual = self._profit(opp, leg1, leg2, size)
        outcome = "incident" if incident_type and leg2.get("fillQty", 0) == 0 else (
            "profit" if profit > 0.01 else "loss" if profit < -0.01 else "breakeven")
        rec = {
            "id": f"arb-{int(time.time()*1000)}", "mode": self.get_mode(), "symbol": opp["symbol"],
            "timestamp": time.time(),
            "leg1": {"exchange": opp["buyExchange"], "side": "BUY", "orderQty": round(size, 2),
                     "fillPrice": leg1.get("fillPrice"), "fillQty": leg1.get("fillQty"),
                     "fee": leg1.get("fee"), "fillTime_ms": leg1.get("fillTime_ms")},
            "leg2": {"exchange": opp["sellExchange"], "side": "SELL", "orderQty": leg2.get("fillQty"),
                     "fillPrice": leg2.get("fillPrice"), "fillQty": leg2.get("fillQty"),
                     "fee": leg2.get("fee"), "fillTime_ms": leg2.get("fillTime_ms")},
            "grossPct_quoted": opp.get("grossPct"), "grossPct_actual": gross_actual,
            "netPct_quoted": opp.get("netPct"), "netPct_actual": net_actual,
            "profitUSD_actual": profit, "executionLatency_ms": latency_ms,
            "grokApproved": bool(approval) if approval is None else approval.get("approved"),
            "grokConfidence": approval.get("confidence") if approval else None,
            "outcome": outcome,
        }
        if incident_type:
            rec["incidentType"] = incident_type
        self.ledger.record(rec)

    # ------------------------------------------------------------------
    # LEG-2 FAILURE RECOVERY (4 steps)
    # ------------------------------------------------------------------
    def _handle_leg2_failure(self, opp, leg1, size, est_qty, latency_ms, approval):
        sym, sell_ex, buy_ex = opp["symbol"], opp["sellExchange"], opp["buyExchange"]
        qty = leg1.get("fillQty", est_qty) or est_qty
        # Step 1 — retry leg 2 once
        leg2 = _settle(self.gateway.place_order, sell_ex, sym, "SELL", qty=qty)
        if leg2.get("ok"):
            self._record(opp, leg1, leg2, size, latency_ms, approval, incident_type="leg2_retry_ok")
            self._after_incident(sym, "leg2_retry_ok")
            return
        # Step 2 — emergency sell on a different exchange (priority order)
        done = None
        recovery = None
        for venue in ["coinbase", "kraken", "bitstamp"] + EXCHANGES:
            if venue == sell_ex:
                continue
            r = _settle(self.gateway.place_order, venue, sym, "SELL", qty=qty)
            if r.get("ok"):
                done, recovery = r, f"emergency_sell_{venue}"
                break
        # Step 3 — same-exchange unwind on the buy venue
        if done is None:
            r = _settle(self.gateway.place_order, buy_ex, sym, "SELL", qty=qty)
            if r.get("ok"):
                done, recovery = r, "same_exchange_unwind"
        if done is not None:
            self._record(opp, leg1, done, size, latency_ms, approval, incident_type=recovery)
        else:
            # Step 4 — manual alert; no further automated action
            est_loss = round(qty * opp["buyAsk"], 2)
            self.last_incident = {"level": "CRITICAL",
                                  "message": f"UNHEDGED POSITION: {round(qty,6)} {sym} on {buy_ex}",
                                  "estimated_loss_usd": est_loss, "ts": time.time()}
            recovery = "UNHEDGED_MANUAL"
            self._record(opp, leg1, {"fillPrice": 0, "fillQty": 0, "fee": 0},
                         size, latency_ms, approval, incident_type=recovery)
        self._after_incident(sym, recovery)

    def _handle_leg1_failure(self, opp, leg2):
        sym, sell_ex = opp["symbol"], opp["sellExchange"]
        notional = (leg2.get("fillQty", 0) or 0) * (leg2.get("fillPrice", 0) or 0)
        _settle(self.gateway.place_order, sell_ex, sym, "BUY", usd=notional)
        self._after_incident(sym, "leg1_fail_bought_back")

    def _after_incident(self, sym, recovery):
        rec = {"ts": round(time.time(), 1), "symbol": sym, "recovery": recovery,
               "incident": self.last_incident}
        try:
            with open(str(self.ledger.path.parent / "arb_incidents.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
        if recovery != "leg2_retry_ok":
            self.paused_until = time.time() + self.pause_after_incident

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        return {
            "enabled": False,
            "permanently_disabled": ARBITRAGE_PERMANENTLY_DISABLED,
            "disabled_reason": ARBITRAGE_DISABLED_REASON,
            "status": "disabled",
            "paused_seconds_left": max(0, int(self.paused_until - now)),
            "simulate": getattr(self.detector, "simulate", False),
            "last_skip": self.last_skip,
            "last_incident": self.last_incident,
            "opportunities": self.last_opps,
            "recent_trades": self.ledger.recent(20),
            "metrics": self.ledger.metrics(),
        }
