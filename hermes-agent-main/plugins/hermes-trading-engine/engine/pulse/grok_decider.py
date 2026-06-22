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

from engine.pulse.grok_intel import _grok_chat, _grok_responses, _parse_json, GrokBudget

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
    # p_up: Grok's probability BTC closes UP — REQUIRED on every window (even no_trade) so the
    # directional VIEW is graded each window and Grok accumulates calibrated edge data fast.
    p_up = _clamp01(d.get("p_up"))
    if p_up is None:
        c = conf or 0.5
        p_up = c if action == "up" else ((1.0 - c) if action == "down" else 0.5)
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
    return {"action": action, "confidence": round(conf or 0.0, 4), "p_up": round(p_up, 4),
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
            "LEARN from your own track record in 'decider_track_record' (direction accuracy overall, "
            "by context, and 'recent_decisions' hits/misses): lean into contexts where your calls "
            "have been right and avoid/abstain in contexts where they've been wrong as evidence grows. "
            "Be calibrated and selective; prefer no_trade for the ACTION when uncertain. But ALWAYS "
            "give your best-estimate p_up (probability BTC closes UP this window) even when action is "
            "no_trade — it is graded every window to build your track record. Respond with STRICT "
            "JSON ONLY: {\"action\":\"up|down|no_trade\",\"p_up\":<0-1>,\"confidence\":<0-1>,"
            "\"size_fraction\":<0-1>,\"max_price\":<0-1 optional>,\"key_risks\":[\"...\"],"
            "\"rationale\":\"<short>\",\"ttl_s\":<seconds this decision stays valid>}.\nBUNDLE: "
            + json.dumps(bundle, default=str)[:11000])
        content = chat(prompt, model=model, timeout_s=timeout_s, box=box, extra_body=extra)
        return normalize_decision(_parse_json(content), default_ttl_s=default_ttl_s)
    return _decide


def make_news_fn(*, model: str = "grok-4.3", timeout_s: float = 35.0, responses=_grok_responses):
    """Build ``news_fn() -> digest|None`` that pulls a short BTC news/sentiment digest via the xAI
    Agent Tools API (built-in web_search + x_search on /v1/responses). Separated from the per-window
    decision so news is gathered periodically (cheap, bounded) and injected into every bundle."""
    box: dict = {}
    tools = [{"type": "web_search"}, {"type": "x_search"}]

    def _news() -> Optional[dict]:
        prompt = (
            "Search the latest web + X for BREAKING Bitcoin news and sentiment in the last ~30 "
            "minutes that could move BTC over the NEXT 5 MINUTES (macro prints, ETF flows, "
            "exchange/regulatory headlines, large liquidations, prominent X posts). Summarize for a "
            "short-horizon trader. Reply with STRICT JSON only: {\"sentiment\":\"bullish|bearish|"
            "neutral\",\"confidence\":<0-1>,\"headlines\":[\"...\"],\"event_risk\":\"low|medium|"
            "high\"}.")
        d = _parse_json(responses(prompt, model=model, timeout_s=timeout_s, box=box, tools=tools))
        if not d:
            return None
        return {"sentiment": str(d.get("sentiment", "neutral"))[:20],
                "confidence": _clamp01(d.get("confidence"), 0.0),
                "headlines": [str(x)[:200] for x in (d.get("headlines") or [])][:6],
                "event_risk": str(d.get("event_risk", "low"))[:12]}
    return _news


class GrokNewsDigest:
    """Periodic BTC news/sentiment digest (xAI live search), cached + injected into every decision
    bundle. Budget-gated + fail-open. Observe-only context; the decision still belongs to Grok."""

    def __init__(self, *, news_fn=None, budget: Optional[GrokBudget] = None,
                 interval_s: float = 300.0, max_age_s: float = 600.0):
        self._fn = news_fn if news_fn is not None else make_news_fn()
        self._budget = budget
        self.interval_s = max(60.0, float(interval_s))
        self.max_age_s = float(max_age_s)
        self._lock = threading.Lock()
        self._digest: Optional[dict] = None
        self._ts = 0.0
        self.calls = 0
        self.errors = 0
        self.skipped_budget = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def refresh(self) -> Optional[dict]:
        if self._budget is not None and not self._budget.try_spend("news"):
            self.skipped_budget += 1
            return None
        d = None
        try:
            d = self._fn()
        except Exception:  # noqa: BLE001
            d = None
        if d is None:
            self.errors += 1
        else:
            self.calls += 1
            with self._lock:
                self._digest, self._ts = d, time.time()
        return d

    def latest(self) -> Optional[dict]:
        with self._lock:
            if not self._digest or (time.time() - self._ts) > self.max_age_s:
                return None
            return {**self._digest, "age_s": round(time.time() - self._ts, 1)}

    def _worker(self) -> None:
        self._stop.wait(min(self.interval_s, 15.0))
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval_s)

    def start(self) -> "GrokNewsDigest":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grok-news-digest",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            return {"enabled": True, "interval_s": self.interval_s, "calls": self.calls,
                    "errors": self.errors, "skipped_budget": self.skipped_budget,
                    "latest": (dict(self._digest) if self._digest else None),
                    "age_s": (round(time.time() - self._ts, 1) if self._digest else None)}

    def to_state(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "digest": self._digest, "ts": self._ts}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.calls = int(data.get("calls", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self._digest = data.get("digest")
            self._ts = float(data.get("ts", 0.0) or 0.0)


class GrokDecider:
    """Background decision worker + grader. The engine ``request``s a decision per window, reads the
    cached ``get`` result fail-open, and ``grade``s it against the realized outcome. PAPER ONLY."""

    def __init__(self, *, decider_fn=None, budget: Optional[GrokBudget] = None,
                 mode: str = "shadow", min_confidence: float = 0.55, ttl_s: float = 240.0,
                 max_consecutive_losses: int = 4, daily_loss_cap_usd: float = 30.0,
                 max_latency_s: float = 20.0, cooldown_s: float = 1800.0,
                 max_pending: int = 200, max_results: int = 5000):
        self._fn = decider_fn if decider_fn is not None else make_decider_fn()
        self._budget = budget
        self.mode = mode if mode in ("off", "shadow", "follow") else "off"
        self.min_confidence = float(min_confidence)
        self.ttl_s = float(ttl_s)
        # ---- circuit breaker (FOLLOW only): trip -> stop following (fall back to baseline) ----
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.daily_loss_cap_usd = float(daily_loss_cap_usd)
        self.max_latency_s = float(max_latency_s)
        self.cooldown_s = float(cooldown_s)
        self._consec_losses = 0
        self._daily_loss = 0.0
        self._daily_key = None
        self._tripped_until = 0.0
        self._trip_reason: Optional[str] = None
        self.trips = 0
        self._recent_lat: deque = deque(maxlen=10)
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
        # ---- directional VIEW grading (p_up vs realized close on EVERY window, traded or not) ----
        # this is the rich, always-on edge data: Grok's p_up is scored each window so it accumulates
        # a calibrated track record even while it abstains from trading.
        self.view_graded = 0
        self.view_correct = 0
        self.view_brier_sum = 0.0
        # ---- learning-as-it-trades: per-context VIEW accuracy + recent graded outcomes ----
        self.by_context: dict = {}            # dim -> bucket -> {"n","correct"} (by p_up view)
        self._recent: deque = deque(maxlen=12)

    # -- request / read ----------------------------------------------------- #
    def request(self, decision_id: str, bundle: dict, context: Optional[dict] = None) -> None:
        if not decision_id or self.mode == "off":
            return
        with self._lock:
            if decision_id in self._seen:
                return
            self._seen.add(decision_id)
            self._queue.append((decision_id, bundle, context or {}))
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

    # -- circuit breaker (FOLLOW safety) ------------------------------------ #
    def _trip(self, now: float, reason: str) -> None:
        self._tripped_until = now + self.cooldown_s
        self._trip_reason = reason
        self.trips += 1
        self._consec_losses = 0

    def should_follow(self, now: Optional[float] = None) -> "tuple[bool, str]":
        """Whether FOLLOW may act now, else (False, reason) — the bot falls back to baseline. Trips
        on consecutive follow-losses, a daily follow-loss cap, or sustained high decision latency."""
        now = float(now if now is not None else time.time())
        with self._lock:
            if now < self._tripped_until:
                return False, ("breaker_" + (self._trip_reason or "tripped"))
            if self._daily_loss >= self.daily_loss_cap_usd > 0:
                self._trip(now, "daily_loss_cap")
                return False, "breaker_daily_loss_cap"
            if (len(self._recent_lat) >= self._recent_lat.maxlen
                    and (sum(self._recent_lat) / len(self._recent_lat)) > self.max_latency_s > 0):
                self._trip(now, "latency")
                return False, "breaker_latency"
            return True, "ok"

    def record_follow_result(self, *, won: bool, pnl: float, now: Optional[float] = None) -> None:
        """Feed a settled FOLLOW trade back to the breaker (consecutive losses + daily loss)."""
        now = float(now if now is not None else time.time())
        with self._lock:
            day = int(now // 86400)
            if day != self._daily_key:
                self._daily_key, self._daily_loss = day, 0.0
            pnl = float(pnl or 0.0)
            if pnl < 0:
                self._daily_loss += -pnl
            self._consec_losses = 0 if won else (self._consec_losses + 1)
            if self.max_consecutive_losses > 0 and self._consec_losses >= self.max_consecutive_losses:
                self._trip(now, "consecutive_losses")

    def _breaker_status_locked(self, now: float) -> dict:
        tripped = now < self._tripped_until
        return {"tripped": tripped, "reason": (self._trip_reason if tripped else None),
                "consecutive_losses": self._consec_losses,
                "daily_follow_loss_usd": round(self._daily_loss, 4),
                "daily_loss_cap_usd": self.daily_loss_cap_usd, "trips": self.trips,
                "cooldown_remaining_s": (round(self._tripped_until - now, 1) if tripped else 0),
                "max_consecutive_losses": self.max_consecutive_losses,
                "max_latency_s": self.max_latency_s}

    def breaker_status(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else time.time())
        with self._lock:
            return self._breaker_status_locked(now)

    # -- worker ------------------------------------------------------------- #
    def _process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            decision_id, bundle, context = self._queue.popleft()
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
                decision["context"] = context or {}
                self.decided += 1
                self.latency_sum += latency
                self._recent_lat.append(latency)
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
            # (1) ALWAYS grade the directional VIEW (p_up) vs the realized outcome — this is the
            # rich edge data that accrues every window even when the action is no_trade.
            p_up = float(dec.get("p_up") if dec.get("p_up") is not None else 0.5)
            view_correct = (p_up > 0.5) == bool(outcome_up)
            self.view_graded += 1
            self.view_correct += int(view_correct)
            self.view_brier_sum += (p_up - (1.0 if outcome_up else 0.0)) ** 2
            for dim, bucket in (dec.get("context") or {}).items():
                if bucket is None:
                    continue
                cb = self.by_context.setdefault(dim, {}).setdefault(str(bucket), {"n": 0, "correct": 0})
                cb["n"] += 1
                cb["correct"] += int(view_correct)
            self._recent.append({"action": action, "p_up": round(p_up, 3),
                                 "confidence": round(float(dec.get("confidence") or 0.0), 3),
                                 "outcome_up": bool(outcome_up), "view_correct": bool(view_correct),
                                 "context": dec.get("context") or {}})
            # (2) ACTION-level grading (only for up/down trades the bot would actually take)
            if action == "no_trade":
                self.abstains += 1
                return
            ap = p_up if action == "up" else (1.0 - p_up)
            correct = (action == "up") == bool(outcome_up)
            self.graded += 1
            self.correct += int(correct)
            self.brier_sum += (ap - (1.0 if outcome_up else 0.0)) ** 2
            b["wins"] += int(correct)

    def report(self) -> dict:
        with self._lock:
            acc = round(self.correct / self.graded, 4) if self.graded else None
            brier = round(self.brier_sum / self.graded, 4) if self.graded else None
            v_acc = round(self.view_correct / self.view_graded, 4) if self.view_graded else None
            v_brier = round(self.view_brier_sum / self.view_graded, 4) if self.view_graded else None
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
                "views_graded": self.view_graded, "view_accuracy": v_acc, "view_brier": v_brier,
                "abstains": self.abstains, "by_action": by_action,
                "accuracy_by_context": {
                    dim: {b: {"n": s["n"],
                              "accuracy": (round(s["correct"] / s["n"], 4) if s["n"] else None)}
                          for b, s in buckets.items()}
                    for dim, buckets in self.by_context.items()},
                "recent_decisions": list(self._recent),
                "circuit_breaker": self._breaker_status_locked(time.time()),
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
                    "view_graded": self.view_graded, "view_correct": self.view_correct,
                    "view_brier_sum": round(self.view_brier_sum, 6),
                    "by_action": {a: dict(s) for a, s in self.by_action.items()},
                    "trips": self.trips, "consec_losses": self._consec_losses,
                    "daily_loss": round(self._daily_loss, 4), "daily_key": self._daily_key,
                    "tripped_until": self._tripped_until, "trip_reason": self._trip_reason,
                    "by_context": {d: {b: dict(s) for b, s in bk.items()}
                                   for d, bk in self.by_context.items()},
                    "recent": list(self._recent)}

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
            self.view_graded = int(data.get("view_graded", 0) or 0)
            self.view_correct = int(data.get("view_correct", 0) or 0)
            self.view_brier_sum = float(data.get("view_brier_sum", 0.0) or 0.0)
            self.by_action = {a: {"n": int(s.get("n", 0) or 0), "wins": int(s.get("wins", 0) or 0),
                                  "pnl": float(s.get("pnl", 0.0) or 0.0)}
                              for a, s in (data.get("by_action") or {}).items()}
            self.trips = int(data.get("trips", 0) or 0)
            self._consec_losses = int(data.get("consec_losses", 0) or 0)
            self._daily_loss = float(data.get("daily_loss", 0.0) or 0.0)
            self._daily_key = data.get("daily_key")
            self._tripped_until = float(data.get("tripped_until", 0.0) or 0.0)
            self._trip_reason = data.get("trip_reason")
            self.by_context = {d: {b: {"n": int(s.get("n", 0) or 0),
                                       "correct": int(s.get("correct", 0) or 0)}
                                   for b, s in bk.items()}
                               for d, bk in (data.get("by_context") or {}).items()}
            self._recent = deque((data.get("recent") or []), maxlen=12)
