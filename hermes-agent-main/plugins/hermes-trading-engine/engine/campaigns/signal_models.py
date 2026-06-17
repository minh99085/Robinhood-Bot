"""Signal models + recursive feedback calibrator for paper campaigns.

`ResearchSignalModel` sources its fair value from the Phase-5 Grok **research**
engine (research-only: Grok estimates a probability + evidence; it NEVER places,
cancels, or sizes an order). Resolution order per market:

  1. in-memory cache (per tick window)
  2. latest persisted probability estimate in storage
  3. a live Grok research call (only when RESEARCH_MODE is online AND a key is set)
  4. a deterministic OFFLINE research stub (clearly labelled; used with no key)

`FeedbackCalibrator` closes the recursive loop: every closed paper trade feeds
(predicted_prob, realised win/loss, edge) back into a persistent calibration
state that scales the *next* cycle's edge. Good calibration widens the edge
multiplier (more trades pass the gate); poor calibration shrinks it.

Nothing here can submit an order; the campaign's risk gate + paper fill
simulator remain the only path, and Grok stays research-only.

Quant scope — *Data Acquisition & Ingestion* (cached/live research estimate
resolution), *Signal Generation & Strategy Development* (fair-value signal), and
*Strategy Optimization & Robustness Testing* (the recursive feedback calibrator).
Grok stays research-only: it estimates a probability + evidence and can never
size, approve, place, arm, or bypass risk.

In the priority hierarchy (Bregman arbitrage P1 > calibrated statistical
mispricing P2 > directional predictive edge P3), the research signal is a P3
directional input consumed by :mod:`engine.training.signal_resolver`; it cannot
escalate its own priority or override the deterministic edge gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from engine.markets import universe_manager as um

_ONLINE_MODES = ("online_paper", "online_shadow", "guarded_live_readonly", "online")


def _market_mid(rec: um.MarketRecord) -> float:
    bid = um._as_float(rec.raw.get("bestBid"), 0.0)
    ask = um._as_float(rec.raw.get("bestAsk"), 0.0)
    if bid and ask:
        return (bid + ask) / 2.0
    return rec.yes_price if rec.yes_price is not None else 0.5


def _opt_float(v):
    """Best-effort float or None (never raises) — for optional structured fields."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class SignalResult:
    fair_value: float
    confidence: float
    source: str            # grok_online | grok_cache | offline_research_stub | simulated
    estimate_id: Optional[str] = None
    # ---- structured research signal (advisory-only; optional) ----
    # Used by the probability stack to scale HOW MUCH / HOW LONG this signal moves
    # p_raw: per-call conviction (0..1), the news as-of timestamp + half-life for
    # time-decay, and the evidence trail for audit. None -> today's behavior.
    conviction: Optional[float] = None
    research_uncertainty: Optional[float] = None
    asof_ts: Optional[float] = None
    news_half_life_s: Optional[float] = None
    key_evidence: Optional[list] = None


class SimulatedSignalModel:
    """SIMULATED fair value (NOT real alpha). Deterministic per-market pseudo-edge."""

    name = "simulated"

    def __init__(self, seed: int = 42):
        self.seed = seed

    def fair_value(self, rec: um.MarketRecord) -> float:
        mid = _market_mid(rec)
        h = hashlib.sha256(f"{self.seed}:{rec.market_id}".encode()).digest()
        n = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
        return max(0.02, min(0.98, mid + (n - 0.5) * 0.12))

    def evaluate(self, rec: um.MarketRecord) -> SignalResult:
        return SignalResult(self.fair_value(rec), 0.5, "simulated")

    def status(self) -> dict:
        return {"name": self.name, "grok_enabled": False, "grok_source": "disabled",
                "research_mode": "n/a"}


class ResearchSignalModel:
    """Fair value sourced from the Grok research engine (research-only)."""

    name = "research"

    def __init__(self, store=None, seed: int = 42, cache_ttl_s: float = 120.0):
        self.store = store
        self.seed = seed
        self.cache_ttl_s = cache_ttl_s
        self.research_mode = (os.getenv("RESEARCH_MODE") or "offline_cache").strip().lower()
        self.api_key = (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()
        self.model = (os.getenv("GROK_MODEL") or os.getenv("HTE_GROK_MODEL") or "grok-4.3").strip()
        self.grok_online = bool(self.api_key) and self.research_mode in _ONLINE_MODES
        self._cache: dict[str, tuple[float, SignalResult]] = {}
        self._client = None
        self.calls_online = 0
        self.calls_cache = 0
        self.calls_stub = 0
        if self.grok_online:
            try:
                from engine.research.grok_client import GrokResearchClient
                self._client = GrokResearchClient.from_env(store=store)
            except Exception:  # noqa: BLE001 - never let research wiring crash the campaign
                self._client = None
                self.grok_online = False

    # ---- offline deterministic research stub (clearly NOT real Grok) -----
    def _offline_stub(self, rec: um.MarketRecord) -> SignalResult:
        mid = _market_mid(rec)
        h = hashlib.sha256(f"research:{self.seed}:{rec.market_id}".encode()).digest()
        n = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
        conf = 0.4 + (int.from_bytes(h[4:6], "big") / 0xFFFF) * 0.4
        # Wider synthetic deviation so the campaign/feedback machinery is exercised
        # when Grok is unavailable. The LIVE Grok path uses real probabilities.
        fair = max(0.02, min(0.98, mid + (n - 0.5) * 0.16))
        return SignalResult(round(fair, 4), round(conf, 3), "offline_research_stub")

    def _from_storage(self, rec: um.MarketRecord) -> Optional[SignalResult]:
        if self.store is None:
            return None
        try:
            ests = self.store.get_probability_estimates(
                venue="polymarket", market_id=rec.market_id, limit=1)
        except Exception:  # noqa: BLE001
            return None
        if not ests:
            return None
        e = ests[0]
        p = e.get("p_ensemble") or e.get("p_calibrated")
        try:
            fair = float(p)
        except (TypeError, ValueError):
            return None
        conf = float(e.get("confidence") or 0.5)
        return SignalResult(round(fair, 4), conf, "grok_cache", e.get("estimate_id"))

    def _from_grok(self, rec: um.MarketRecord) -> Optional[SignalResult]:
        if not self.grok_online or self._client is None:
            return None
        ctx = {
            "venue": "polymarket", "market_id": rec.market_id, "question": rec.question,
            "outcome": "YES", "p_market_mid": _market_mid(rec),
            "rules": rec.raw.get("rules") or rec.raw.get("resolutionSource") or "",
            "description": rec.raw.get("description") or "",
            "end_date": rec.raw.get("endDate") or "",
        }
        try:
            res = self._client.research(ctx)
        except Exception:  # noqa: BLE001
            return None
        p = getattr(res, "p_ensemble", None) or getattr(res, "p_calibrated", None)
        if p is None:
            return None  # ResearchFailure (e.g. not online / budget) -> caller falls back
        # structured (advisory-only) fields when the Grok result carries them; else None
        ev = getattr(res, "key_evidence", None) or getattr(res, "evidence", None)
        return SignalResult(
            round(float(p), 4), float(getattr(res, "confidence", 0.5) or 0.5),
            "grok_online", getattr(res, "estimate_id", None),
            conviction=_opt_float(getattr(res, "conviction", None)),
            research_uncertainty=_opt_float(getattr(res, "uncertainty", None)),
            asof_ts=_opt_float(getattr(res, "asof_ts", getattr(res, "asof", None)))
            or time.time(),
            news_half_life_s=_opt_float(getattr(res, "news_half_life_s", None)),
            key_evidence=(list(ev)[:10] if isinstance(ev, (list, tuple)) else None))

    def evaluate(self, rec: um.MarketRecord) -> SignalResult:
        now = time.time()
        hit = self._cache.get(rec.market_id)
        if hit and (now - hit[0]) < self.cache_ttl_s:
            return hit[1]
        res = self._from_grok(rec) or self._from_storage(rec) or self._offline_stub(rec)
        if res.source == "grok_online":
            self.calls_online += 1
        elif res.source == "grok_cache":
            self.calls_cache += 1
        else:
            self.calls_stub += 1
        self._cache[rec.market_id] = (now, res)
        return res

    def status(self) -> dict:
        return {
            "name": self.name,
            "grok_enabled": self.grok_online,
            "grok_source": ("online_research" if self.grok_online else
                            ("offline_cache" if self.research_mode == "offline_cache"
                             else "disabled")),
            "research_mode": self.research_mode,
            "model": self.model,
            "calls_online": self.calls_online,
            "calls_cache": self.calls_cache,
            "calls_offline_stub": self.calls_stub,
        }


# ---------------------------------------------------------------------------
# Recursive feedback calibrator
# ---------------------------------------------------------------------------

class FeedbackCalibrator:
    """Recursive feedback loop: closed-trade outcomes calibrate future signals.

    Maintains a rolling window of (predicted_prob, win, edge) samples and derives
    an ``edge_adjustment`` multiplier applied to the next cycle's net edge. The
    multiplier rises with a good hit-rate (more trades clear the gate) and falls
    with a poor one — a closed-loop, self-tuning behaviour. State is persisted so
    learning accumulates across ticks and runs."""

    def __init__(self, path: Optional[Path] = None, floor: float = 0.4, cap: float = 1.2,
                 window: int = 50, min_samples: int = 5, enabled: bool = True):
        self.path = Path(path) if path else None
        self.floor, self.cap, self.window = floor, cap, window
        self.min_samples, self.enabled = min_samples, enabled
        self.samples: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.samples = list(data.get("samples", []))[-self.window:]
            except (ValueError, OSError):
                self.samples = []

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"samples": self.samples[-self.window:],
                                             "summary": self.summary()}, default=str),
                                 encoding="utf-8")
        except OSError:
            pass

    def record_outcome(self, *, predicted_prob: float, predicted_edge: float,
                       realized_pnl: float, size_usd: float) -> None:
        win = 1 if realized_pnl > 0 else 0
        captured = (realized_pnl / size_usd) if size_usd else 0.0
        self.samples.append({"p": round(float(predicted_prob), 4), "win": win,
                             "edge": round(float(predicted_edge), 4),
                             "capture": round(captured, 4)})
        self.samples = self.samples[-self.window:]
        self._persist()

    def edge_adjustment(self) -> float:
        if not self.enabled or len(self.samples) < self.min_samples:
            return 1.0
        hit = sum(s["win"] for s in self.samples) / len(self.samples)
        # hit 0.5 -> 1.0 ; 0.7 -> 1.2(cap) ; 0.3 -> 0.8 ; clamped to [floor, cap]
        return round(max(self.floor, min(self.cap, 0.5 + hit)), 3)

    def summary(self) -> dict:
        n = len(self.samples)
        hit = round(sum(s["win"] for s in self.samples) / n, 4) if n else 0.0
        brier = round(sum((s["p"] - s["win"]) ** 2 for s in self.samples) / n, 4) if n else None
        avg_capture = round(sum(s["capture"] for s in self.samples) / n, 4) if n else 0.0
        return {"samples": n, "hit_rate": hit, "brier": brier,
                "avg_edge_capture": avg_capture, "edge_adjustment": self.edge_adjustment(),
                "enabled": self.enabled}


def build_signal_model(kind: str, store=None, seed: int = 42):
    if (kind or "simulated").lower() == "research":
        return ResearchSignalModel(store=store, seed=seed)
    return SimulatedSignalModel(seed=seed)
