"""GrokBrain — xAI Grok intelligence layer (signal synthesis, regime, review, anomaly).

PYTHON port of the GrokBrain spec, wired into the live engine. Grok receives
structured market data and returns ONLY JSON matching ActionSchema. Used for:
  1. Signal synthesis  -> synthesize_action(context) -> ActionSchema
  2. Regime classification -> classify_regime(candles, macro)
  3. Post-trade review -> review_trade(record) -> lesson appended to memory
  4. Anomaly detection -> detect_anomaly(exchange_snapshot)

Robustness (fallback chain):
  * Grok call fails / times out (default 4s)  -> caller falls back to Markov-only
  * Grok returns invalid JSON                 -> best-effort parse, else fallback
  * confidence < 0.4                          -> action forced to WAIT
Model is auto-resolved against the account's /v1/models (skips models that the
chat endpoint rejects). Auth: GROK_API_KEY or XAI_API_KEY (never hardcoded).

Back-compat: keeps prob_up()/latest()/status() so the existing pulse engine and
dashboard keep working unchanged.  PAPER trading only — no real orders, ever.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from typing import Optional

import httpx

from .grok_memory import GrokMemory
from .schemas import parse_grok_action

_SYSTEM = (
    "You are the reasoning core of HermesTradingEngine, an autonomous crypto scalper. "
    "You receive structured market data and must output ONLY valid JSON matching the "
    "ActionSchema. Never explain. Never add prose. JSON only."
)

_ACTION_SCHEMA = (
    "ActionSchema = {"
    '"action":"BUY|SELL|HOLD|WAIT",'
    '"confidence":0.0-1.0,'
    '"reasoning":"<=120 chars",'
    '"suggestedSizePct":0-100,'
    '"stopLossPct":number,'
    '"takeProfitPct":number,'
    '"urgency":"low|medium|high",'
    '"vetoReason":"only if WAIT/HOLD override"}'
)

_STANCE_GUIDANCE = {
    "cautious": "Be conservative: only BUY/SELL on a clear, aligned edge; otherwise WAIT.",
    "balanced": "Be balanced: act when signals lean one way; WAIT when they conflict.",
    "aggressive": "Be decisive and active: take a side on any real lean; only WAIT when truly flat.",
}

_FALLBACK_MODELS = ["grok-4.3", "grok-4-fast", "grok-3-mini", "grok-3", "grok-2-1212", "grok-2", "grok-beta"]
_SKIP_MARKERS = ("not found", "not allowed", "multi agent", "multi-agent", "unsupported",
                 "does not support", "invalid model", "not available", "no access")


def _score_model(mid: str) -> int:
    l = mid.lower()
    if "grok" not in l:
        return -100
    s = 0
    if "fast" in l:
        s += 6
    if "grok-4" in l or "grok4" in l:
        s += 5
    elif "grok-3" in l or "grok3" in l:
        s += 4
    elif "grok-2" in l or "grok2" in l:
        s += 2
    if "mini" in l:
        s += 1
    if "reasoning" in l:
        s -= 2
    if "heavy" in l or "agent" in l:
        s -= 30
    if any(x in l for x in ("vision", "image", "draw", "embed", "tts", "code")):
        s -= 30
    return s


def _clamp(x, lo, hi, default):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


class GrokBrain:
    def __init__(self, settings):
        self.s = settings
        self.api_key = (os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY") or "").strip()
        self.model = (os.getenv("GROK_MODEL") or os.getenv("HTE_GROK_MODEL") or "grok-4.3").strip()
        self.base_url = (os.getenv("GROK_BASE_URL") or os.getenv("HTE_GROK_BASE_URL")
                         or "https://api.x.ai/v1").rstrip("/")
        self.refresh_seconds = float(os.getenv("HTE_GROK_REFRESH_SECONDS", "30"))
        self.timeout_s = float(os.getenv("HTE_GROK_TIMEOUT_MS", "4000")) / 1000.0
        self.max_tokens = int(os.getenv("HTE_GROK_MAX_TOKENS", "2048"))
        self.temperature = float(os.getenv("HTE_GROK_TEMPERATURE", "0.1"))
        self.regime_interval = float(os.getenv("HTE_GROK_REGIME_INTERVAL", "300"))
        # RESEARCH_MODE gates whether the legacy Grok brain may make FRESH xAI
        # network calls. offline_cache / disabled => no fresh calls (so the BTC
        # Pulse panel stays consistent with the Research panel). Online modes, or
        # an explicit GROK_BRAIN_ONLINE=1, allow it. Grok is research-only: it can
        # never place, cancel, approve, arm, scale, or size an order.
        self.research_mode = (os.getenv("RESEARCH_MODE") or "offline_cache").strip().lower()
        _online_modes = ("online_paper", "online_shadow", "guarded_live_readonly", "online")
        self.grok_network_allowed = (
            self.research_mode in _online_modes
            or os.getenv("GROK_BRAIN_ONLINE", "0") not in ("0", "false", "False", ""))
        # network is enabled only when a key exists AND research mode permits it
        self.enabled = bool(self.api_key) and self.grok_network_allowed
        # Runtime on/off set from the dashboard (None = use the computed default
        # above). Grok stays RESEARCH-ONLY either way; this only controls whether
        # the research layer runs / makes calls.
        self._user_override = None
        if not self.api_key:
            self.grok_source = "disabled"
        elif self.grok_network_allowed:
            self.grok_source = "online_research"
        elif self.research_mode in ("offline_cache", ""):
            self.grok_source = "offline_cache"
        else:
            self.grok_source = "legacy_cached"
        self.stance = getattr(settings, "stance", "balanced")

        self.memory = GrokMemory(getattr(settings, "data_dir", "."))
        self._latest: dict = {}
        self._regime: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._context_provider = None
        self._review_q: deque = deque(maxlen=200)
        self._last_error = ""
        self._available_models: list[str] = []
        self._models_fetched = False
        self._bad_models: set[str] = set()
        self._last_settled = 0
        self._last_regime_ts = 0.0

    # ------------------------------------------------------------------
    def attach_context_provider(self, fn) -> None:
        self._context_provider = fn

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="grok-brain", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_active(self, on: bool) -> dict:
        """Dashboard on/off switch for the Grok RESEARCH layer (research-only;
        Grok still can never place, cancel, or size an order). Turning it on
        starts the worker thread if a key is present; off pauses all calls."""
        on = bool(on)
        self._user_override = on
        if on and self.api_key:
            self.enabled = True
            self.grok_source = "online_research"
            self._stop.clear()
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="grok-brain", daemon=True)
                self._thread.start()
        else:
            self.enabled = False
            self.grok_source = "disabled" if not self.api_key else "paused_by_user"
        return self.status()

    def queue_trade_review(self, record: dict) -> None:
        """Called by trading engines when a paper trade closes (non-blocking)."""
        self._review_q.append(record)

    # ------------------------------------------------------------------
    # prompts
    # ------------------------------------------------------------------
    def _system_prompt(self) -> str:
        lessons = self.memory.recent_text(10)
        parts = [_SYSTEM, _ACTION_SCHEMA, _STANCE_GUIDANCE.get(self.stance, _STANCE_GUIDANCE["balanced"])]
        if lessons:
            parts.append("Learned patterns from your past paper trades (apply them):\n" + lessons)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # model resolution + raw JSON chat
    # ------------------------------------------------------------------
    def _fetch_models(self) -> None:
        if self._models_fetched:
            return
        try:
            with httpx.Client(timeout=15.0) as c:
                r = c.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", data if isinstance(data, list) else [])
                self._available_models = [m.get("id") if isinstance(m, dict) else str(m)
                                          for m in items if (m.get("id") if isinstance(m, dict) else m)]
                self._models_fetched = True
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"model list failed: {str(exc)[:120]}"

    def _candidates(self) -> list[str]:
        pool = self._available_models or _FALLBACK_MODELS
        usable = sorted([m for m in pool if _score_model(m) > -50 and m not in self._bad_models],
                        key=_score_model, reverse=True)
        ordered = []
        if self.model and self.model not in self._bad_models and (self.model in pool or not self._available_models):
            ordered.append(self.model)
        for m in usable:
            if m not in ordered:
                ordered.append(m)
        return ordered[:6] or [self.model]

    def _chat_json(self, user_payload: dict, timeout: Optional[float] = None) -> Optional[dict]:
        if not self.enabled:
            return None
        timeout = timeout or self.timeout_s
        messages = [{"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": json.dumps(user_payload, default=str)}]
        last = ""
        try:
            with httpx.Client(timeout=timeout) as c:
                for model in self._candidates():
                    payload = {"model": model, "messages": messages,
                               "temperature": self.temperature, "max_tokens": self.max_tokens}
                    try:
                        r = c.post(f"{self.base_url}/chat/completions", json=payload,
                                   headers={"Authorization": f"Bearer {self.api_key}",
                                            "Content-Type": "application/json"})
                    except Exception as exc:  # noqa: BLE001 (timeout etc -> fallback)
                        last = str(exc)[:140]
                        continue
                    if r.status_code == 200:
                        try:
                            content = r.json()["choices"][0]["message"]["content"]
                        except Exception:  # noqa: BLE001
                            last = "no content"
                            continue
                        parsed = self._parse_json(content)
                        if parsed is not None:
                            self.model = model
                            self._last_error = ""
                            return parsed
                        last = "invalid JSON"
                        continue
                    body = r.text[:160]
                    last = f"{r.status_code}: {body}"
                    if any(mk in body.lower() for mk in _SKIP_MARKERS):
                        self._bad_models.add(model)
                        continue
                    break
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:140]
        self._last_error = f"xAI {last}"
        return None

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            pass
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except (ValueError, TypeError):
                return None
        return None

    # ------------------------------------------------------------------
    # 1) signal synthesis -> ActionSchema
    # ------------------------------------------------------------------
    def synthesize_action(self, ctx: dict) -> Optional[dict]:
        regime = ctx.get("regime", {})
        mc = ctx.get("montecarlo", {})
        patt = ctx.get("patterns", {})
        payload = {
            "task": "synthesize_action",
            "schema": "ActionSchema",
            "markov_regime": regime.get("current_state"),
            "markov_p_up": regime.get("p_up"),
            "regime_strength": regime.get("regime_strength"),
            "montecarlo_p_up": mc.get("p_up"),
            "montecarlo_expected": mc.get("expected"),
            "patterns": {"bias": patt.get("bias"),
                         "bos": patt.get("bos", {}).get("dir"),
                         "choch": patt.get("choch", {}).get("dir"),
                         "sweep": patt.get("liquidity_sweep", {}).get("dir")},
            "arb_opportunities": ctx.get("arb_opportunities", []),
            "order_book_imbalance": ctx.get("order_book_imbalance"),
            "pulse": ctx.get("pulse", {}),
            "track_record": ctx.get("track_record", {}),
            "regime_context": self._regime,
        }
        raw = self._chat_json(payload)
        return self._coerce_action(raw)

    def _coerce_action(self, raw: Optional[dict]) -> Optional[dict]:
        # A failed/empty Grok call returns None so callers fall back to the
        # quant-only path. Any NON-empty but invalid payload is parsed through
        # the Pydantic GrokAction model, which collapses to a WAIT action — we
        # never turn malformed model output into a best-effort BUY/SELL. The
        # confidence floor (<0.4) also demotes weak BUY/SELL signals to WAIT.
        if raw is None:
            return None
        act = parse_grok_action(raw, min_confidence=0.4)
        return {
            "action": act.action,
            "confidence": round(act.confidence, 3),
            "reasoning": act.reasoning,
            # NOTE: suggestedSizePct is advisory only — Grok never sizes orders.
            "suggestedSizePct": act.suggestedSizePct,
            "stopLossPct": act.stopLossPct,
            "takeProfitPct": act.takeProfitPct,
            "urgency": act.urgency,
            "vetoReason": act.vetoReason,
        }

    # ------------------------------------------------------------------
    # 2) regime classification
    # ------------------------------------------------------------------
    def classify_regime(self, candles: list, macro: Optional[dict] = None) -> Optional[dict]:
        if not candles:
            return None
        closes = [c.get("c") for c in candles[-30:] if isinstance(c, dict)]
        payload = {"task": "classify_regime", "last_closes": closes, "macro": macro or {},
                   "respond": {"regime": "trend_up|trend_down|range|volatile",
                               "maxPositionPct": "0-100", "reasoning": "<=120 chars"}}
        raw = self._chat_json(payload, timeout=max(self.timeout_s, 8.0))
        if not raw:
            return None
        out = {"regime": str(raw.get("regime", "range")),
               "maxPositionPct": _clamp(raw.get("maxPositionPct", 100), 0, 100, 100.0),
               "reasoning": str(raw.get("reasoning", ""))[:120], "ts": time.time()}
        with self._lock:
            self._regime = out
        return out

    # ------------------------------------------------------------------
    # 3) post-trade review -> lesson
    # ------------------------------------------------------------------
    def review_trade(self, record: dict) -> Optional[str]:
        payload = {"task": "review_trade", "trade": record,
                   "respond": {"lesson": "<=120 chars actionable lesson for future trades"}}
        raw = self._chat_json(payload, timeout=max(self.timeout_s, 8.0))
        if not raw:
            return None
        lesson = str(raw.get("lesson", "")).strip()[:120]
        if lesson:
            self.memory.append(lesson, meta={"trade": {k: record.get(k) for k in
                               ("market", "side", "result", "pnl")}})
        return lesson or None

    # ------------------------------------------------------------------
    # 4) anomaly detection across exchanges
    # ------------------------------------------------------------------
    def detect_anomaly(self, exchange_snapshot: dict) -> Optional[dict]:
        payload = {"task": "detect_anomaly", "exchanges": exchange_snapshot,
                   "respond": {"anomaly": "true|false", "reason": "<=120 chars",
                               "severity": "low|medium|high"}}
        raw = self._chat_json(payload, timeout=max(self.timeout_s, 6.0))
        if not raw:
            return None
        return {"anomaly": bool(raw.get("anomaly") in (True, "true", "True", 1)),
                "reason": str(raw.get("reason", ""))[:120],
                "severity": str(raw.get("severity", "low")).lower()}

    # ------------------------------------------------------------------
    # background loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        self._stop.wait(3.0)
        while not self._stop.is_set():
            if not self.enabled:          # runtime on/off: do nothing while OFF
                self._stop.wait(2.0)
                continue
            try:
                self._fetch_models()
                ctx = self._context_provider() if self._context_provider else None
                if ctx:
                    act = self.synthesize_action(ctx)
                    if act:
                        act["ts"] = time.time()
                        with self._lock:
                            self._latest = act
                    # auto post-trade review when a new pulse round has settled
                    tr = ctx.get("track_record", {})
                    cnt = tr.get("pulse_rounds_settled", 0) or 0
                    outs = tr.get("recent_pulse_outcomes", [])
                    if cnt > self._last_settled and outs:
                        self._last_settled = cnt
                        self.review_trade({"market": "pulse", **outs[0],
                                           "regime": ctx.get("regime", {}).get("current_state"),
                                           "pattern_bias": ctx.get("patterns", {}).get("bias")})
                    # periodic regime classification when candles are available
                    if ctx.get("candles") and time.time() - self._last_regime_ts > self.regime_interval:
                        self._last_regime_ts = time.time()
                        self.classify_regime(ctx.get("candles"), ctx.get("macro"))
                # drain at most one queued external trade review per iteration
                if self._review_q:
                    self.review_trade(self._review_q.popleft())
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)[:200]
            self._stop.wait(self.refresh_seconds)

    # ------------------------------------------------------------------
    # back-compat surface used by the existing pulse engine + dashboard
    # ------------------------------------------------------------------
    _DIR = {"BUY": "UP", "SELL": "DOWN", "HOLD": "HOLD", "WAIT": "HOLD"}

    def latest(self) -> dict:
        with self._lock:
            adv = dict(self._latest)
        if adv:
            adv["age_seconds"] = round(time.time() - adv.get("ts", 0), 1)
            adv["fresh"] = adv["age_seconds"] <= self.refresh_seconds * 2
            adv["direction"] = self._DIR.get(adv.get("action", "WAIT"), "HOLD")
            adv["rationale"] = adv.get("reasoning", "")
        return adv

    def status(self) -> dict:
        adv = self.latest()
        return {
            "enabled": self.enabled,
            "grok_network_enabled": bool(self.enabled),
            "grok_source": self.grok_source,
            "user_override": self._user_override,
            "research_mode": self.research_mode,
            "default_model": "grok-4.3",
            "model": self.model if self.enabled else None,
            "stance": self.stance,
            "action": adv.get("action"),
            "direction": adv.get("direction"),
            "confidence": adv.get("confidence"),
            "urgency": adv.get("urgency"),
            "rationale": adv.get("rationale"),
            "fresh": adv.get("fresh", False),
            "age_seconds": adv.get("age_seconds"),
            "regime": self._regime.get("regime") if self._regime else None,
            "memory_lessons": self.memory.count(),
            "last_error": self._last_error or None,
        }

    def prob_up(self) -> Optional[float]:
        adv = self.latest()
        if not adv or not adv.get("fresh"):
            return None
        action, c = adv.get("action"), adv.get("confidence", 0.0)
        if action == "BUY":
            return 0.5 + 0.5 * c
        if action == "SELL":
            return 0.5 - 0.5 * c
        return 0.5  # HOLD / WAIT -> neutral (no directional lean for the pulse blend)
