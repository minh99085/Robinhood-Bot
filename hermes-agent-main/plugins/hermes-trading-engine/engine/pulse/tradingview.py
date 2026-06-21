"""TradingView indicator-alert intake for the BTC 5-min pulse (OBSERVE-ONLY).

TradingView alerts feed Hermes **candidate signals only**. A TradingView alert can NEVER:
directly place a trade, resize a trade, bypass the strategy/execution gate, or override the
Polymarket orderbook checks. It is normalized into a ``TradingViewSignalEvent`` and attached to
candidates as an observe-only external feature; whether a paper trade happens is decided solely
by the existing Hermes strategy + the strict execution-quality gate.

This module is pure (no sockets) so it is fully unit-testable; the HTTP listener lives in
``engine/pulse/webhook.py`` and simply calls :meth:`TradingViewIntake.ingest`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hte.pulse.tradingview")

# explicit, stable rejection reasons (acceptance criterion #3 + #8)
INVALID_JSON = "invalid_json"
MISSING_SECRET = "missing_secret"
BAD_SECRET = "bad_secret"
WRONG_BOT = "wrong_bot_name"
UNSUPPORTED_SYMBOL = "unsupported_symbol"
STALE_TIMESTAMP = "stale_timestamp"
MALFORMED_DIRECTION = "malformed_direction"
DUPLICATE_EVENT_ID = "duplicate_event_id"
NOT_OBJECT = "payload_not_object"
REJECT_REASONS = (INVALID_JSON, MISSING_SECRET, BAD_SECRET, WRONG_BOT, UNSUPPORTED_SYMBOL,
                  STALE_TIMESTAMP, MALFORMED_DIRECTION, DUPLICATE_EVENT_ID, NOT_OBJECT)

_DIRECTION_MAP = {
    "up": "UP", "long": "UP", "buy": "UP", "bull": "UP", "bullish": "UP", "1": "UP",
    "down": "DOWN", "short": "DOWN", "sell": "DOWN", "bear": "DOWN", "bearish": "DOWN", "-1": "DOWN",
    "flat": "FLAT", "neutral": "FLAT", "none": "FLAT", "close": "FLAT", "exit": "FLAT", "0": "FLAT",
}


def normalize_direction(raw) -> Optional[str]:
    if raw is None:
        return None
    return _DIRECTION_MAP.get(str(raw).strip().lower())


def normalize_symbol(raw) -> str:
    """Uppercase + strip a leading ``EXCHANGE:`` prefix so TradingView ``{{ticker}}`` values like
    ``COINBASE:BTCUSD`` / ``BINANCE:BTCUSDT`` match the allow-list (``BTCUSD`` / ``BTCUSDT``)."""
    s = str(raw or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    return s


def _parse_ts(val) -> Optional[float]:
    """Parse an epoch (s or ms) or ISO-8601 timestamp into epoch seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v / 1000.0 if v > 1e11 else v          # ms -> s heuristic
    s = str(val).strip()
    if not s:
        return None
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


@dataclass
class TradingViewSignalEvent:
    """Normalized, observe-only external signal from a TradingView indicator alert."""
    event_id: str
    bot_name: str
    symbol: str
    timeframe: Optional[str]
    bar_time: Optional[float]
    received_at: float
    direction: str                       # "UP" | "DOWN" | "FLAT"
    strength: Optional[float]
    indicator_name: Optional[str]
    raw_payload_hash: str
    source: str = "tradingview"
    observe_only: bool = True

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "source": self.source, "bot_name": self.bot_name,
                "symbol": self.symbol, "timeframe": self.timeframe, "bar_time": self.bar_time,
                "received_at": round(self.received_at, 3), "direction": self.direction,
                "strength": self.strength, "indicator_name": self.indicator_name,
                "raw_payload_hash": self.raw_payload_hash, "observe_only": True}

    def as_feature(self, *, now: Optional[float] = None) -> dict:
        """The observe-only feature view attached to a candidate (never trades/sizes/vetoes)."""
        now = float(now if now is not None else time.time())
        return {"source": "tradingview", "observe_only": True, "event_id": self.event_id,
                "direction": self.direction, "strength": self.strength,
                "indicator_name": self.indicator_name, "symbol": self.symbol,
                "timeframe": self.timeframe, "bar_time": self.bar_time,
                "age_s": (round(now - self.received_at, 3))}


class TradingViewEdge:
    """OBSERVE-ONLY measurement: did the TradingView signal present at entry predict the 5-min
    Chainlink outcome, and did the bot win more when its side aligned with the signal?

    Grouped by direction / timeframe / symbol / alignment. REPORT-ONLY — it never affects which
    paper trades are taken (it is computed at SETTLEMENT, after the outcome is known)."""

    MIN_EVIDENCE = 30          # min UP/DOWN signals before claiming a directional edge

    def __init__(self):
        self.n_total = 0
        self.outcomes_up = 0
        self.n_with_signal = 0
        self.n_no_signal = 0
        self.signal_evaluated = 0      # UP/DOWN signals only (FLAT/none excluded)
        self.signal_correct = 0
        self.dims: dict = {"direction": {}, "timeframe": {}, "symbol": {}, "alignment": {}}

    def _b(self, dim: str, key: str) -> dict:
        return self.dims[dim].setdefault(str(key), {"n": 0, "sig_eval": 0, "sig_correct": 0,
                                                     "bot_wins": 0, "pnl": 0.0})

    def record(self, *, tv, traded_side, outcome_up: bool, won: bool, pnl: float) -> None:
        self.n_total += 1
        if outcome_up:
            self.outcomes_up += 1
        won = bool(won)
        pnl = float(pnl or 0.0)
        tv = tv or {}
        direction = tv.get("direction")
        tf = tv.get("timeframe")
        sym = tv.get("symbol")
        has_dir = direction in ("UP", "DOWN")
        if direction in ("UP", "DOWN", "FLAT"):
            self.n_with_signal += 1
        else:
            self.n_no_signal += 1
        correct = None
        if has_dir:
            correct = (direction == "UP" and outcome_up) or (direction == "DOWN" and not outcome_up)
            self.signal_evaluated += 1
            self.signal_correct += int(bool(correct))
        if has_dir and traded_side in ("up", "down"):
            aligned = ((direction == "UP" and traded_side == "up")
                       or (direction == "DOWN" and traded_side == "down"))
            align_key = "aligned" if aligned else "opposed"
        elif direction == "FLAT":
            align_key = "flat_signal"
        else:
            align_key = "no_signal"

        def bump(dim, key):
            b = self._b(dim, key)
            b["n"] += 1
            b["bot_wins"] += int(won)
            b["pnl"] = round(b["pnl"] + pnl, 6)
            if correct is not None:
                b["sig_eval"] += 1
                b["sig_correct"] += int(bool(correct))
        bump("direction", direction or "none")
        bump("timeframe", tf or "none")
        bump("symbol", sym or "none")
        bump("alignment", align_key)

    @staticmethod
    def _bucket(b: dict) -> dict:
        return {"n": b["n"],
                "signal_hit_rate": (round(b["sig_correct"] / b["sig_eval"], 4) if b["sig_eval"]
                                    else None),
                "bot_win_rate": (round(b["bot_wins"] / b["n"], 4) if b["n"] else None),
                "pnl_usd": round(b["pnl"], 4),
                "avg_pnl_usd": (round(b["pnl"] / b["n"], 4) if b["n"] else None)}

    def report(self) -> dict:
        base_up = round(self.outcomes_up / self.n_total, 4) if self.n_total else None
        hit = (round(self.signal_correct / self.signal_evaluated, 4)
               if self.signal_evaluated else None)
        dims = {f"by_{d}": {k: self._bucket(v) for k, v in self.dims[d].items()} for d in self.dims}
        al = self.dims["alignment"]
        aligned_wr = (self._bucket(al["aligned"])["bot_win_rate"] if "aligned" in al else None)
        opposed_wr = (self._bucket(al["opposed"])["bot_win_rate"] if "opposed" in al else None)
        verdict = "insufficient_evidence"
        if self.signal_evaluated >= self.MIN_EVIDENCE and hit is not None:
            if hit >= 0.55:
                verdict = "signal_predictive_edge"
            elif hit <= 0.45:
                verdict = "signal_inverse_edge"      # consistently wrong -> a fade signal
            else:
                verdict = "no_directional_edge"
        return {
            "report_only": True, "observe_only": True,
            "min_evidence": self.MIN_EVIDENCE,
            "n_settled_with_signal": self.n_with_signal,
            "n_settled_no_signal": self.n_no_signal,
            "signal_evaluated_up_down": self.signal_evaluated,
            "signal_hit_rate": hit, "baseline_up_rate": base_up,
            "aligned_bot_win_rate": aligned_wr, "opposed_bot_win_rate": opposed_wr,
            "verdict": verdict,
            **dims,
            "note": ("observe-only: did the TradingView signal at entry predict the 5-min "
                     "Chainlink outcome (signal_hit_rate vs baseline_up_rate), and did aligning "
                     "help the bot win (aligned vs opposed bot_win_rate)? Never affects trading."),
        }

    def to_state(self) -> dict:
        return {"n_total": self.n_total, "outcomes_up": self.outcomes_up,
                "n_with_signal": self.n_with_signal, "n_no_signal": self.n_no_signal,
                "signal_evaluated": self.signal_evaluated, "signal_correct": self.signal_correct,
                "dims": {d: {k: dict(v) for k, v in self.dims[d].items()} for d in self.dims}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.n_total = int(data.get("n_total", 0) or 0)
        self.outcomes_up = int(data.get("outcomes_up", 0) or 0)
        self.n_with_signal = int(data.get("n_with_signal", 0) or 0)
        self.n_no_signal = int(data.get("n_no_signal", 0) or 0)
        self.signal_evaluated = int(data.get("signal_evaluated", 0) or 0)
        self.signal_correct = int(data.get("signal_correct", 0) or 0)
        for d in self.dims:
            self.dims[d] = {}
            for k, v in (data.get("dims") or {}).get(d, {}).items():
                self.dims[d][k] = {"n": int(v.get("n", 0) or 0),
                                   "sig_eval": int(v.get("sig_eval", 0) or 0),
                                   "sig_correct": int(v.get("sig_correct", 0) or 0),
                                   "bot_wins": int(v.get("bot_wins", 0) or 0),
                                   "pnl": float(v.get("pnl", 0.0) or 0.0)}


class RSITrendModel:
    """OBSERVE-ONLY: track the per-symbol history of RSI alerts, classify the current up/down
    trend, and learn ``P(next 5-min Chainlink outcome | current RSI trend state)`` so it can
    PREDICT the next 5-min window's direction — then SCORE its own predictions against reality.

    Leakage-free: the prediction for a window is made from counts that EXCLUDE that window's own
    outcome (counts are updated only at settlement, after scoring). REPORT-ONLY — it never affects
    which paper trades are taken."""

    HIST = 64                  # alerts kept per symbol
    MIN_STATE_N = 8            # min settled samples for a trend-state before it will predict

    def __init__(self):
        self.hist: dict = {}            # symbol -> deque[(ts, direction)]
        self.state_counts: dict = {}    # symbol -> {state_key: {"up": int, "n": int}}
        self.pred_n = 0
        self.pred_correct = 0
        self.pred_by_symbol: dict = {}  # symbol -> {"n","correct"}

    def observe(self, *, symbol: str, direction: str, ts: float) -> None:
        if not symbol:
            return
        dq = self.hist.setdefault(symbol, deque(maxlen=self.HIST))
        dq.append((float(ts or 0.0), direction))

    @staticmethod
    def _streak(dq) -> "tuple[int, Optional[str]]":
        """Signed run length of the latest consecutive same non-FLAT direction (UP=+, DOWN=-)."""
        if not dq:
            return 0, None
        last = dq[-1][1]
        if last not in ("UP", "DOWN"):
            return 0, last
        run = 0
        for _, d in reversed(dq):
            if d == last:
                run += 1
            else:
                break
        return (run if last == "UP" else -run), last

    def _state_key(self, dq) -> str:
        streak, last = self._streak(dq)
        if last not in ("UP", "DOWN"):
            return "flat_or_none"
        return ("up" if streak > 0 else "down") + "_streak" + str(min(abs(streak), 3))

    def trend(self, symbol: str) -> dict:
        dq = self.hist.get(symbol)
        if not dq:
            return {"symbol": symbol, "n": 0, "last_direction": None, "streak": 0,
                    "state": "flat_or_none", "recent_up_fraction": None}
        streak, last = self._streak(dq)
        recent = [d for _, d in list(dq)[-8:] if d in ("UP", "DOWN")]
        ups = sum(1 for d in recent if d == "UP")
        return {"symbol": symbol, "n": len(dq), "last_direction": last, "streak": streak,
                "state": self._state_key(dq),
                "recent_up_fraction": (round(ups / len(recent), 3) if recent else None)}

    def predict(self, symbol: str) -> dict:
        """Observe-only next-5-min prediction from P(up | current RSI trend state)."""
        dq = self.hist.get(symbol)
        if not dq:
            return {"symbol": symbol, "prediction": None, "reason": "no_history"}
        state = self._state_key(dq)
        c = (self.state_counts.get(symbol) or {}).get(state)
        if not c or c["n"] < self.MIN_STATE_N:
            return {"symbol": symbol, "state": state, "prediction": None, "prob_up": None,
                    "reason": "insufficient_state_samples", "state_n": (c["n"] if c else 0)}
        p_up = c["up"] / c["n"]
        return {"symbol": symbol, "state": state,
                "prediction": ("UP" if p_up > 0.5 else "DOWN"), "prob_up": round(p_up, 4),
                "confidence": round(abs(p_up - 0.5) * 2, 3), "state_n": c["n"],
                "basis": "conditional_outcome_given_rsi_trend"}

    def score_and_update(self, *, symbol: str, state: Optional[str], predicted: Optional[str],
                         outcome_up: bool) -> None:
        """Score the entry-time prediction (leakage-free), then fold the realized outcome into the
        conditional distribution for that trend state."""
        if predicted in ("UP", "DOWN"):
            correct = (predicted == "UP" and outcome_up) or (predicted == "DOWN" and not outcome_up)
            self.pred_n += 1
            self.pred_correct += int(bool(correct))
            ps = self.pred_by_symbol.setdefault(symbol or "none", {"n": 0, "correct": 0})
            ps["n"] += 1
            ps["correct"] += int(bool(correct))
        if state:
            sc = self.state_counts.setdefault(symbol or "none", {}).setdefault(
                state, {"up": 0, "n": 0})
            sc["n"] += 1
            sc["up"] += int(bool(outcome_up))

    def report(self) -> dict:
        acc = round(self.pred_correct / self.pred_n, 4) if self.pred_n else None
        return {
            "observe_only": True, "report_only": True,
            "min_state_samples": self.MIN_STATE_N,
            "predictions_scored": self.pred_n,
            "prediction_accuracy": acc,
            "prediction_accuracy_by_symbol": {
                s: {"n": v["n"], "accuracy": (round(v["correct"] / v["n"], 4) if v["n"] else None)}
                for s, v in self.pred_by_symbol.items()},
            "current_trend": {s: self.trend(s) for s in self.hist},
            "next_window_prediction": {s: self.predict(s) for s in self.hist},
            "learned_states": {s: {k: {"n": v["n"],
                                       "p_up": (round(v["up"] / v["n"], 4) if v["n"] else None)}
                                   for k, v in sc.items()}
                               for s, sc in self.state_counts.items()},
            "note": ("observe-only: learns P(next 5-min outcome | RSI alert trend state) from the "
                     "alert history and scores its own next-window predictions. Never trades."),
        }

    def to_state(self) -> dict:
        return {"hist": {s: [[t, d] for t, d in dq] for s, dq in self.hist.items()},
                "state_counts": {s: {k: dict(v) for k, v in sc.items()}
                                 for s, sc in self.state_counts.items()},
                "pred_n": self.pred_n, "pred_correct": self.pred_correct,
                "pred_by_symbol": {s: dict(v) for s, v in self.pred_by_symbol.items()}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.hist = {}
        for s, seq in (data.get("hist") or {}).items():
            dq = deque(maxlen=self.HIST)
            for item in seq:
                try:
                    dq.append((float(item[0]), item[1]))
                except Exception:  # noqa: BLE001
                    continue
            self.hist[s] = dq
        self.state_counts = {}
        for s, sc in (data.get("state_counts") or {}).items():
            self.state_counts[s] = {k: {"up": int(v.get("up", 0) or 0), "n": int(v.get("n", 0) or 0)}
                                    for k, v in sc.items()}
        self.pred_n = int(data.get("pred_n", 0) or 0)
        self.pred_correct = int(data.get("pred_correct", 0) or 0)
        self.pred_by_symbol = {s: {"n": int(v.get("n", 0) or 0), "correct": int(v.get("correct", 0) or 0)}
                               for s, v in (data.get("pred_by_symbol") or {}).items()}


def _event_from_dict(d) -> Optional["TradingViewSignalEvent"]:
    if not isinstance(d, dict) or not d.get("event_id"):
        return None
    try:
        return TradingViewSignalEvent(
            event_id=str(d["event_id"]), bot_name=str(d.get("bot_name") or ""),
            symbol=str(d.get("symbol") or ""), timeframe=d.get("timeframe"),
            bar_time=d.get("bar_time"), received_at=float(d.get("received_at") or 0.0),
            direction=str(d.get("direction") or "FLAT"), strength=d.get("strength"),
            indicator_name=d.get("indicator_name"),
            raw_payload_hash=str(d.get("raw_payload_hash") or ""))
    except Exception:  # noqa: BLE001
        return None


class TradingViewIntake:
    """Validates + normalizes + de-duplicates TradingView alerts and exposes report counters.

    Thread-safe: the webhook thread calls :meth:`ingest`; the engine thread calls
    :meth:`drain_pending`, :meth:`latest_feature`, and :meth:`report`."""

    def __init__(self, *, secret: str, allowed_symbols, bot_name: str = "hermes",
                 max_age_s: float = 90.0, future_skew_s: float = 30.0,
                 data_dir: Optional[str] = None, dedupe_capacity: int = 5000,
                 header_name: str = "X-Tradingview-Secret"):
        self.secret = str(secret or "")
        self.allowed_symbols = {str(s).strip().upper() for s in (allowed_symbols or []) if str(s).strip()}
        self.bot_name = str(bot_name or "").strip().lower()
        self.max_age_s = float(max_age_s)
        self.future_skew_s = float(future_skew_s)
        self.header_name = header_name
        self._lock = threading.Lock()
        self._seen: "deque[str]" = deque(maxlen=int(dedupe_capacity))
        self._seen_set: set = set()
        self._pending: list = []
        self.received = 0
        self.valid = 0
        self.rejected = 0
        self.consumed = 0
        self.reject_reasons: dict = {}
        self.latest: Optional[TradingViewSignalEvent] = None
        # per-source tracking (e.g. Coinbase BTCUSD + Binance BTCUSDT used together)
        self.latest_by_symbol: dict = {}
        self.valid_by_symbol: dict = {}
        self._path = (Path(data_dir) / "btc_pulse_tradingview.json") if data_dir else None
        self._load_state()

    # -- validation (pure given inputs) ------------------------------------- #
    def _check_secret(self, payload: dict, provided_header: Optional[str]) -> Optional[str]:
        provided = provided_header if provided_header else payload.get("secret")
        if provided is None or str(provided) == "":
            return MISSING_SECRET
        if not hmac.compare_digest(str(provided), self.secret):
            return BAD_SECRET
        return None

    def normalize(self, raw_bytes: bytes, *, provided_header: Optional[str], now: float):
        """Return (event, reject_reason). Exactly one is non-None."""
        raw_hash = hashlib.sha256(raw_bytes if isinstance(raw_bytes, bytes)
                                  else str(raw_bytes).encode("utf-8")).hexdigest()
        try:
            payload = json.loads(raw_bytes)
        except Exception:  # noqa: BLE001
            return None, INVALID_JSON
        if not isinstance(payload, dict):
            return None, NOT_OBJECT
        # 1) authenticate FIRST (don't leak symbol/bot validity to unauthenticated callers)
        sec = self._check_secret(payload, provided_header)
        if sec is not None:
            return None, sec
        # 2) bot name filter
        bot = str(payload.get("bot_name") or payload.get("bot") or "").strip()
        if self.bot_name and bot.lower() != self.bot_name:
            return None, WRONG_BOT
        # 3) symbol allow-list (exchange-prefix tolerant)
        symbol = normalize_symbol(payload.get("symbol") or payload.get("ticker"))
        if not symbol or (self.allowed_symbols and symbol not in self.allowed_symbols):
            return None, UNSUPPORTED_SYMBOL
        # 4) direction
        direction = normalize_direction(payload.get("direction") or payload.get("action")
                                        or payload.get("signal"))
        if direction is None:
            return None, MALFORMED_DIRECTION
        # 5) freshness (only when a bar/alert timestamp is supplied)
        bar_time = _parse_ts(payload.get("bar_time") or payload.get("time")
                             or payload.get("timestamp"))
        if bar_time is not None:
            if (now - bar_time) > self.max_age_s or (bar_time - now) > self.future_skew_s:
                return None, STALE_TIMESTAMP
        # strength (optional)
        strength = None
        try:
            if payload.get("strength") is not None:
                strength = float(payload.get("strength"))
        except (TypeError, ValueError):
            strength = None
        event_id = str(payload.get("event_id") or payload.get("id") or "").strip() or raw_hash[:24]
        ev = TradingViewSignalEvent(
            event_id=event_id, bot_name=(bot or self.bot_name), symbol=symbol,
            timeframe=(str(payload.get("timeframe") or payload.get("interval") or "").strip() or None),
            bar_time=bar_time, received_at=now, direction=direction, strength=strength,
            indicator_name=(str(payload.get("indicator_name") or payload.get("indicator")
                                or "").strip() or None),
            raw_payload_hash=raw_hash)
        return ev, None

    # -- ingest (called by the webhook thread) ------------------------------ #
    def ingest(self, raw_bytes: bytes, *, provided_header: Optional[str] = None,
               now: Optional[float] = None):
        """Validate + record one alert. Returns (status_code:int, body:dict)."""
        now = float(now if now is not None else time.time())
        with self._lock:
            self.received += 1
            ev, reason = self.normalize(raw_bytes, provided_header=provided_header, now=now)
            if reason is not None:
                self.rejected += 1
                self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
                # 401 for auth failures, 400 for everything else (never reveals the secret)
                code = 401 if reason in (MISSING_SECRET, BAD_SECRET) else 400
                self._persist_locked()
                logger.info("tradingview alert REJECTED: reason=%s (received=%d valid=%d)",
                            reason, self.received, self.valid)
                return code, {"ok": False, "reason": reason, "observe_only": True}
            if ev.event_id in self._seen_set:
                self.rejected += 1
                self.reject_reasons[DUPLICATE_EVENT_ID] = \
                    self.reject_reasons.get(DUPLICATE_EVENT_ID, 0) + 1
                self._persist_locked()
                return 200, {"ok": True, "duplicate": True, "reason": DUPLICATE_EVENT_ID,
                             "event_id": ev.event_id, "observe_only": True}
            # accept (observe-only): record dedupe id, counters, latest, pending queue
            self._seen.append(ev.event_id)
            self._seen_set.add(ev.event_id)
            if len(self._seen_set) > self._seen.maxlen:
                # keep the set bounded to the deque window
                self._seen_set = set(self._seen)
            self.valid += 1
            self.latest = ev
            self.latest_by_symbol[ev.symbol] = ev
            self.valid_by_symbol[ev.symbol] = self.valid_by_symbol.get(ev.symbol, 0) + 1
            self._pending.append(ev)
            self._persist_locked()
            logger.info("tradingview alert ACCEPTED (observe-only): %s %s tf=%s strength=%s id=%s "
                        "(valid=%d)", ev.symbol, ev.direction, ev.timeframe, ev.strength,
                        ev.event_id, self.valid)
            return 200, {"ok": True, "accepted": True, "event_id": ev.event_id,
                         "direction": ev.direction, "observe_only": True,
                         "note": "candidate-signal only; cannot place/resize/bypass a trade"}

    # -- engine-side consumption -------------------------------------------- #
    def drain_pending(self) -> list:
        with self._lock:
            out, self._pending = self._pending, []
            self.consumed += len(out)
            return out

    def latest_feature(self, *, now: Optional[float] = None, symbol: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            ev = self.latest
        if ev is None:
            return None
        if symbol is not None and self.allowed_symbols and ev.symbol != str(symbol).strip().upper():
            # latest signal is for a different (still-allowed) symbol — still observe-only
            pass
        return ev.as_feature(now=now)

    def report(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "tradingview_observe_only": True,
                "tradingview_alerts_received": self.received,
                "tradingview_alerts_valid": self.valid,
                "tradingview_alerts_rejected": self.rejected,
                "tradingview_alerts_consumed_as_features": self.consumed,
                "tradingview_reject_reasons": dict(self.reject_reasons),
                "tradingview_latest_signal": (self.latest.to_dict() if self.latest else None),
                "tradingview_latest_by_symbol": {s: e.to_dict()
                                                 for s, e in self.latest_by_symbol.items()},
                "tradingview_valid_by_symbol": dict(self.valid_by_symbol),
                "allowed_symbols": sorted(self.allowed_symbols),
                "bot_name": self.bot_name,
                "dedupe_tracked": len(self._seen_set),
                "note": ("TradingView alerts are candidate signals only — they cannot place, "
                         "resize, or bypass trades; the strategy + execution gate remain "
                         "the sole trade authority."),
            }

    # -- persistence (dedupe survives restarts) ----------------------------- #
    def _persist_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                "received": self.received, "valid": self.valid, "rejected": self.rejected,
                "consumed": self.consumed, "reject_reasons": dict(self.reject_reasons),
                "seen_ids": list(self._seen),
                "latest": (self.latest.to_dict() if self.latest else None),
                "latest_by_symbol": {s: e.to_dict() for s, e in self.latest_by_symbol.items()},
                "valid_by_symbol": dict(self.valid_by_symbol),
            }, default=str, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001 — persistence never breaks intake
            pass

    def _load_state(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        self.received = int(data.get("received", 0) or 0)
        self.valid = int(data.get("valid", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.consumed = int(data.get("consumed", 0) or 0)
        self.reject_reasons = {k: int(v or 0) for k, v in (data.get("reject_reasons") or {}).items()}
        for sid in (data.get("seen_ids") or []):
            self._seen.append(sid)
        self._seen_set = set(self._seen)
        # restore the last signal(s) so the report keeps showing them across restarts
        self.latest = _event_from_dict(data.get("latest"))
        self.latest_by_symbol = {}
        for sym, ed in (data.get("latest_by_symbol") or {}).items():
            ev = _event_from_dict(ed)
            if ev is not None:
                self.latest_by_symbol[sym] = ev
        self.valid_by_symbol = {k: int(v or 0)
                                for k, v in (data.get("valid_by_symbol") or {}).items()}
