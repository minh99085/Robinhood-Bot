"""Measured Grok calibration -> calibration-weighted research trust (PAPER ONLY).

Grok stays ADVISORY: this module only decides HOW MUCH to trust Grok's probability
when blending it into ``p_raw`` (and therefore the edge). It does NOT place, size, or
gate any trade, and it can never bypass a hard realism/risk/Bregman gate.

Mechanism (quant: calibration-weighted stacking):
* Every closed trade whose probability came from a real Grok source records Grok's
  predicted probability FOR THE TAKEN SIDE and the realized win/loss label.
* We keep a rolling window and compute the Brier score (lower = better calibrated).
* ``trust_weight`` maps Brier skill (vs the uninformative 0.25 baseline = "always
  0.5") to a clamped multiplier in ``[trust_min, 1.0]``. Well-calibrated Grok earns
  up to full weight; poorly-calibrated Grok is floored down. Until ``min_samples``
  outcomes exist we return ``trust_default`` (default 1.0 = behave as before — Grok
  must EARN a reduction from data, and can never exceed its prior weight).

Deterministic + dependency-free; persists to a small JSON so trust survives restarts.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Optional

GROK_SOURCES = ("grok_online", "grok_cache")
UNINFORMATIVE_BRIER = 0.25          # Brier of always predicting 0.5


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class GrokCalibration:
    """Rolling Brier of Grok's directional probability -> a clamped trust multiplier."""

    def __init__(self, path: Optional[str] = None, *, window: int = 200,
                 min_samples: int = 20, trust_min: float = 0.2,
                 trust_default: float = 1.0, enabled: bool = True):
        self.path = Path(path) if path else None
        self.window = max(10, int(window))
        self.min_samples = max(1, int(min_samples))
        self.trust_min = _clamp01(trust_min)
        self.trust_default = _clamp01(trust_default)
        self.enabled = bool(enabled)
        self._records: deque = deque(maxlen=self.window)
        self._load()

    # -- helpers -------------------------------------------------------- #
    @staticmethod
    def is_grok(source) -> bool:
        return str(source or "") in GROK_SOURCES

    @staticmethod
    def directional_prob(p_research: float, side: str) -> float:
        """Grok's probability FOR THE TAKEN SIDE: p(YES) for a YES trade, 1-p(YES)
        for a NO trade. This is what we calibrate against the realized win/loss."""
        p = _clamp01(p_research)
        return p if str(side or "YES").upper() == "YES" else (1.0 - p)

    # -- recording ------------------------------------------------------ #
    def record(self, *, predicted_prob: float, won: bool, source: str,
               category: Optional[str] = None) -> None:
        """Record one closed-trade outcome (only for real Grok sources)."""
        if not self.is_grok(source):
            return
        self._records.append({"p": _clamp01(predicted_prob), "won": 1 if won else 0,
                              "source": str(source), "category": category})
        self._save()

    def record_position(self, *, p_research: float, side: str, won: bool,
                        source: str, category: Optional[str] = None) -> None:
        """Convenience: record a closed position using its taken side."""
        self.record(predicted_prob=self.directional_prob(p_research, side), won=won,
                    source=source, category=category)

    # -- metrics -------------------------------------------------------- #
    @staticmethod
    def _brier(recs: list) -> Optional[float]:
        if not recs:
            return None
        return round(sum((r["p"] - r["won"]) ** 2 for r in recs) / len(recs), 6)

    def sample_count(self) -> int:
        return len(self._records)

    def brier(self) -> Optional[float]:
        return self._brier(list(self._records))

    def trust_weight(self, *, source: Optional[str] = None,
                     category: Optional[str] = None) -> float:
        """Calibration-weighted trust multiplier in [trust_min, 1.0]. Uses a per-
        category window when it has enough category samples, else the global window;
        returns ``trust_default`` until ``min_samples`` outcomes exist."""
        if not self.enabled:
            return 1.0
        recs = list(self._records)
        if category is not None:
            cat = [r for r in recs if r.get("category") == category]
            if len(cat) >= self.min_samples:
                recs = cat
        if len(recs) < self.min_samples:
            return round(self.trust_default, 4)
        b = self._brier(recs)
        if b is None:
            return round(self.trust_default, 4)
        # skill vs the uninformative 0.25 baseline: brier 0 -> 1.0 ; >= 0.25 -> 0.0
        skill = _clamp01((UNINFORMATIVE_BRIER - b) / UNINFORMATIVE_BRIER)
        return round(self.trust_min + (1.0 - self.trust_min) * skill, 4)

    def metrics(self) -> dict:
        return {
            "grok_calibration_enabled": self.enabled,
            "grok_calibration_samples": self.sample_count(),
            "grok_calibration_min_samples": self.min_samples,
            "grok_brier_score": self.brier(),
            "grok_uninformative_brier": UNINFORMATIVE_BRIER,
            "grok_trust_weight": self.trust_weight(),
            "grok_trust_min": self.trust_min,
            "grok_trust_default": self.trust_default,
            "grok_calibration_measured": self.sample_count() >= self.min_samples,
            "advisory_only": True,
        }

    # -- persistence ---------------------------------------------------- #
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for r in (data.get("records", []) or [])[-self.window:]:
                self._records.append({"p": _clamp01(r.get("p", 0.5)),
                                      "won": 1 if r.get("won") else 0,
                                      "source": str(r.get("source", "")),
                                      "category": r.get("category")})
        except Exception:  # noqa: BLE001 — calibration must never break startup
            pass

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"records": list(self._records)}), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001 — persistence must never break a close
            pass
