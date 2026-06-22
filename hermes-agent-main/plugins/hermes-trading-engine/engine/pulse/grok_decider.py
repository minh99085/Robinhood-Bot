"""Grok Decision Engine for the BTC 5-min pulse — the "Grok decides, bot executes" architecture.

This inverts the usual hierarchy: instead of the quant engine deciding and Grok observing, Grok is
asked to synthesize EVERYTHING the bot knows (TradingView signals incl. order-flow/event fields,
market microstructure, price/vol regime, the bot's own LEARNED evidence, position/account state,
and — optionally — live web/X news) and return a structured TRADE DECISION.

Two safety properties are non-negotiable and built in here:

* **PAPER ONLY.** Nothing in this module places a real order; it only emits an advisory decision
  the engine may act on in paper mode.
* **Fail-CLOSED.** A decider that times out, blows its budget, returns malformed output, or is below
  the confidence floor yields ``no_trade`` (abstain) — it never produces a blind trade.

Modes (engine-controlled): ``off`` (disabled), ``shadow`` (decide + grade every window but DO NOT
trade — the safe default that proves whether Grok beats the baseline), ``follow`` (engine follows
Grok's direction/size, subject only to the deterministic floor: execution-quality realism, risk
caps, freshness). Runs on a background worker so it never blocks the tick loop.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Optional

from engine.pulse.grok_intel import _grok_chat, _parse_json, GrokBudget

ACTIONS = ("up", "down", "no_trade")
_ACTION_ALIASES = {
    "up": "up", "buy": "up", "long": "up", "bull": "up", "bullish": "up", "yes": "up",
    "down": "down", "sell": "down", "short": "down", "bear": "down", "bearish": "down", "no": "down",
    "no_trade": "no_trade", "no-trade": "no_trade", "hold": "no_trade", "flat": "no_trade",
    "none": "no_trade", "skip": "no_trade", "abstain": "no_trade", "wait": "no_trade",
}


def _clamp01(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def normalize_decision(d, *, default_ttl_s: float = 240.0) -> Optional[dict]:
    """Parse + validate Grok's raw output into a canonical decision, or None (fail-closed)."""
    if not isinstance(d, dict):
        return None
    raw_action = str(d.get("action") or d.get("decision") or d.get("side") or "").strip().lower()
    action = _ACTION_ALIASES.get(raw_action)
    if action is None:
        return None                                   # unknown action -> caller treats as no decision
    conf = _clamp01(d.get("confidence"), 0.0)
    size = _clamp01(d.get("size_fraction"), 1.0 if action != "no_trade" else 0.0)
    if action == "no_trade":
        size = 0.0
    mp = None
    try:
        if d.get("max_price") is not None:
            mp = max(0.0, min(1.0, float(d.get("max_price"))))
    except (TypeError, ValueError):
        mp = None
    try:
        ttl = float(d.get("ttl_s")) if d.get("ttl_s") is not None else float(default_ttl_s)
    except (TypeError, ValueError):
        ttl = float(default_ttl_s)
    return {"action": action, "confidence": round(conf or 0.0, 4),
            "size_fraction": round(size or 0.0, 4), "max_price": mp,
            "ttl_s": max(0.0, ttl),
            "key_risks": [str(x)[:160] for x in (d.get("key_risks") or [])][:6],
            "rationale": str(d.get("rationale") or "")[:600],
            "schema_version": str(d.get("schema_version") or "1")}


def make_decider_fn(*, model: str = "grok-4.3", timeout_s: float = 12.0,
                    use_search: bool = False, default_ttl_s: float = 240.0,
                    chat=_grok_chat):
    """Build ``decider_fn(bundle) -> decision|None``. ``use_search`` enables xAI live web/X search
    so Grok can pull fresh BTC news/sentiment in parallel. Fail-open (returns None on any error)."""
    box: dict = {}
    extra = None
    if use_search:
        extra = {"search_parameters": {"mode": "auto",
                                       "sources": [{"type": "web"}, {"type": "x"}],
                                       "max_search_results": 8}}

    def _decide(bundle: dict) -> Optional[dict]:
        prompt = (
            "You are the lead decision-maker for an OBSERVE-AND-PAPER-TRADE bot on the Polymarket "
            "BTC 5-minute 'Up or Down' market (settles UP if BTC's Chainlink price at window close "
            "is >= the open). You are given EVERYTHING the bot knows: the TradingView signal "
            "(incl. order-flow/event fields), the live Polymarket order book (spread/depth/mid), "
            "price/volatility + regime, the bot's OWN learned per-bucket performance (win-rate vs "
            "breakeven, EV-after-cost), recent decision grades, and position/account state"
            + (" plus live web/X news you may search" if use_search else "") + ". Decide the single "
            "best action for THIS window. Account for the binary payoff: at ask price p a win nets "
            "(1-p)/p per $ while a loss costs the full stake, so breakeven win-rate is ~p; only "
            "choose up/down if your probability clears that bar after costs, else choose no_trade. "
            "Be calibrated and selective; prefer no_trade when uncertain. Respond with STRICT JSON "
            "ONLY: {\"action\":\"up|down|no_trade\",\"confidence\":<0-1>,\"size_fraction\":<0-1>,"
            "\"max_price\":<0-1 optional>,\"key_risks\":[\"...\"],\"rationale\":\"<short>\","
            "\"ttl_s\":<seconds this decision stays valid>}.\nBUNDLE: "
            + json.dumps(bundle, default=str)[:11000])
        content = chat(prompt, model=model, timeout_s=timeout_s, box=box, extra_body=extra)
        return normalize_decision(_parse_json(content), default_ttl_s=default_ttl_s)
    return _decide


class GrokDecider:
    """Background decision worker + grader. The engine ``request``s a decision per window, reads the
    cached ``get`` result fail-open, and ``grade``s it against the realized outcome. PAPER ONLY."""

    def __init__(self, *, decider_fn=None, budget: Optional[GrokBudget] = None,
                 mode: str = "shadow", min_confidence: float = 0.55, ttl_s: float = 240.0,
                 max_pending: int = 200, max_results: int = 5000):
        self._fn = decider_fn if decider_fn is not None else make_decider_fn()
        self._budget = budget
        self.mode = mode if mode in ("off", "shadow", "follow") else "off"
        self.min_confidence = float(min_confidence)
        self.ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._queue: deque = deque(maxlen=int(max_pending))
        self._results: dict = {}              # decision_id -> decision (+ "ts","latency_s")
        self._order: deque = deque(maxlen=int(max_results))
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # counters
        self.requested = 0
        self.decided = 0
        self.errors = 0
        self.skipped_budget = 0
        self.latency_sum = 0.0
        # grading (direction vs realized 5-min outcome; abstains tracked separately)
        self.graded = 0
        self.correct = 0
        self.brier_sum = 0.0
        self.abstains = 0
        self.by_action: dict = {}             # action -> {"n","wins","pnl"}

    # -- request / read ----------------------------------------------------- #
    def request(self, decision_id: str, bundle: dict) -> None:
        if not decision_id or self.mode == "off":
            return
        with self._lock:
            if decision_id in self._seen:
                return
            self._seen.add(decision_id)
            self._queue.append((decision_id, bundle))
            self.requested += 1

    def get(self, decision_id: str) -> Optional[dict]:
        with self._lock:
            r = self._results.get(decision_id)
            return dict(r) if r else None

    def is_actionable(self, decision: Optional[dict], *, now: Optional[float] = None) -> bool:
        """True only for a fresh, confident up/down decision (used by FOLLOW mode)."""
        if not decision or decision.get("action") not in ("up", "down"):
            return False
        if float(decision.get("confidence") or 0.0) < self.min_confidence:
            return False
        now = float(now if now is not None else time.time())
        age = now - float(decision.get("ts") or 0.0)
        return age <= float(decision.get("ttl_s") or self.ttl_s)

    # -- worker ------------------------------------------------------------- #
    def _process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            decision_id, bundle = self._queue.popleft()
        if self._budget is not None and not self._budget.try_spend("decider"):
            with self._lock:
                self.skipped_budget += 1
            return True
        t0 = time.time()
        decision = None
        try:
            decision = self._fn(bundle)
        except Exception:  # noqa: BLE001 — fail-closed
            decision = None
        latency = time.time() - t0
        with self._lock:
            if decision is None:
                self.errors += 1
            else:
                decision["ts"] = time.time()
                decision["latency_s"] = round(latency, 3)
                self.decided += 1
                self.latency_sum += latency
                self._results[decision_id] = decision
                self._order.append(decision_id)
                if len(self._results) > self._order.maxlen:
                    self._results.pop(self._order.popleft(), None)
        return True

    def _worker(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self._process_one()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(0.2 if worked else 1.0)

    def start(self) -> "GrokDecider":
        if self.mode != "off" and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grok-decider", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- grading ------------------------------------------------------------ #
    def grade(self, decision_id: str, *, outcome_up: bool, pnl: Optional[float] = None) -> None:
        """Grade a prior decision against the realized window outcome (leakage-free; called at/after
        close). Direction accuracy + Brier for up/down; abstains counted. ``pnl`` optional (FOLLOW)."""
        with self._lock:
            dec = self._results.get(decision_id)
            if not dec:
                return
            action = dec.get("action")
            b = self.by_action.setdefault(action, {"n": 0, "wins": 0, "pnl": 0.0})
            b["n"] += 1
            if pnl is not None:
                b["pnl"] = round(b["pnl"] + float(pnl), 6)
            if action == "no_trade":
                self.abstains += 1
                return
            p_up = float(dec.get("confidence") or 0.5)
            p_up = p_up if action == "up" else (1.0 - p_up)     # confidence -> P(up)
            correct = (action == "up") == bool(outcome_up)
            self.graded += 1
            self.correct += int(correct)
            self.brier_sum += (p_up - (1.0 if outcome_up else 0.0)) ** 2
            b["wins"] += int(correct)

    def report(self) -> dict:
        with self._lock:
            acc = round(self.correct / self.graded, 4) if self.graded else None
            brier = round(self.brier_sum / self.graded, 4) if self.graded else None
            avg_lat = round(self.latency_sum / self.decided, 3) if self.decided else None
            by_action = {a: {"n": s["n"],
                             "direction_accuracy": (round(s["wins"] / s["n"], 4)
                                                    if s["n"] and a != "no_trade" else None),
                             "pnl_usd": round(s["pnl"], 4)}
                         for a, s in self.by_action.items()}
            return {
                "enabled": self.mode != "off", "mode": self.mode, "paper_only": True,
                "affects_trading": self.mode == "follow",
                "fail_closed": True, "min_confidence": self.min_confidence, "ttl_s": self.ttl_s,
                "requested": self.requested, "decided": self.decided, "errors": self.errors,
                "skipped_budget": self.skipped_budget, "pending": len(self._queue),
                "avg_latency_s": avg_lat,
                "graded_directional": self.graded, "direction_accuracy": acc, "brier": brier,
                "abstains": self.abstains, "by_action": by_action,
                "note": ("Grok decides; bot executes (paper). shadow=decide+grade only (no trade), "
                         "follow=engine follows direction/size subject to the deterministic floor "
                         "(execution realism, risk caps, freshness). Fail-closed -> no_trade."),
            }

    def to_state(self) -> dict:
        with self._lock:
            return {"requested": self.requested, "decided": self.decided, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "latency_sum": round(self.latency_sum, 3),
                    "graded": self.graded, "correct": self.correct,
                    "brier_sum": round(self.brier_sum, 6), "abstains": self.abstains,
                    "by_action": {a: dict(s) for a, s in self.by_action.items()}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.requested = int(data.get("requested", 0) or 0)
            self.decided = int(data.get("decided", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self.latency_sum = float(data.get("latency_sum", 0.0) or 0.0)
            self.graded = int(data.get("graded", 0) or 0)
            self.correct = int(data.get("correct", 0) or 0)
            self.brier_sum = float(data.get("brier_sum", 0.0) or 0.0)
            self.abstains = int(data.get("abstains", 0) or 0)
            self.by_action = {a: {"n": int(s.get("n", 0) or 0), "wins": int(s.get("wins", 0) or 0),
                                  "pnl": float(s.get("pnl", 0.0) or 0.0)}
                              for a, s in (data.get("by_action") or {}).items()}
