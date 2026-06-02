"""OnlineLearner — explainable, bucketed online statistics (no neural nets).

Learns from every paper decision (trade AND no-trade) and every closed trade:

* bucketed probability calibration (predicted vs realised win-rate)
* per-category reliability (EWMA win-rate -> a small model bias)
* edge-bucket PnL
* spread / liquidity / ambiguity / evidence bucket performance
* no-trade reason counts
* markout aggregates by horizon (5s, 30s, 1m, 5m, 15m, 1h)

Everything is plain stats so the behaviour is fully auditable. State persists to
JSON so learning accumulates across ticks and runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .metrics import (ambiguity_bucket, edge_bucket, evidence_bucket,
                      liquidity_bucket, prob_bucket, spread_bucket)

_MARKOUT_HORIZONS = ("5s", "30s", "1m", "5m", "15m", "1h")


class OnlineLearner:
    def __init__(self, path: Optional[Path] = None, ewma_alpha: float = 0.2,
                 min_bucket_samples: int = 3):
        self.path = Path(path) if path else None
        self.alpha = float(ewma_alpha)
        self.min_bucket_samples = int(min_bucket_samples)
        self.decisions = 0
        self.trades = 0
        self.no_trades = 0
        self.closed = 0
        self.no_trade_reasons: dict = {}
        self.prob_buckets: dict = {}        # bucket -> {n, sum_pred, wins}
        self.categories: dict = {}          # cat -> {n, wins, reliability, ewma_capture}
        self.edge_buckets: dict = {}        # bucket -> {n, pnl, wins}
        self.spread_buckets: dict = {}      # bucket -> {n, pnl}
        self.liquidity_buckets: dict = {}   # bucket -> {n, pnl}
        self.ambiguity_buckets: dict = {}   # bucket -> {n, pnl}
        self.evidence_buckets: dict = {}    # bucket -> {n, pnl}
        self.markouts: dict = {h: {"n": 0, "sum": 0.0} for h in _MARKOUT_HORIZONS}
        # hierarchical signal-resolver telemetry (strategy mix + alpha attribution)
        self.signal_strategies: dict = {}      # strategy -> {selected, traded}
        self.alpha_attribution: dict = {}      # alpha source -> running sum
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return
            for k in ("decisions", "trades", "no_trades", "closed"):
                setattr(self, k, int(d.get(k, 0)))
            self.no_trade_reasons = dict(d.get("no_trade_reasons", {}))
            self.prob_buckets = dict(d.get("prob_buckets", {}))
            self.categories = dict(d.get("categories", {}))
            self.edge_buckets = dict(d.get("edge_buckets", {}))
            self.spread_buckets = dict(d.get("spread_buckets", {}))
            self.liquidity_buckets = dict(d.get("liquidity_buckets", {}))
            self.ambiguity_buckets = dict(d.get("ambiguity_buckets", {}))
            self.evidence_buckets = dict(d.get("evidence_buckets", {}))
            self.markouts = dict(d.get("markouts", self.markouts))
            self.signal_strategies = dict(d.get("signal_strategies", {}))
            self.alpha_attribution = dict(d.get("alpha_attribution", {}))

    def persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state(), default=str), encoding="utf-8")
        except OSError:
            pass

    # -- recording -----------------------------------------------------------
    def record_decision(self, *, traded: bool, reason: str = "") -> None:
        self.decisions += 1
        if traded:
            self.trades += 1
        else:
            self.no_trades += 1
            self.no_trade_reasons[reason] = self.no_trade_reasons.get(reason, 0) + 1

    def record_outcome(self, *, predicted_prob: float, win: bool, realized_pnl: float,
                       category: str = "uncategorized", net_edge: float = 0.0,
                       spread: float = 0.0, liquidity: float = 0.0,
                       ambiguity: float = 0.0, evidence: float = 0.0,
                       markouts: Optional[dict] = None) -> None:
        self.closed += 1
        w = 1 if win else 0

        b = self.prob_buckets.setdefault(prob_bucket(predicted_prob),
                                         {"n": 0, "sum_pred": 0.0, "wins": 0})
        b["n"] += 1
        b["sum_pred"] += float(predicted_prob)
        b["wins"] += w

        c = self.categories.setdefault(category or "uncategorized",
                                       {"n": 0, "wins": 0, "reliability": 0.5,
                                        "ewma_capture": 0.0})
        c["n"] += 1
        c["wins"] += w
        c["reliability"] = (1 - self.alpha) * c["reliability"] + self.alpha * w
        cap = realized_pnl  # absolute pnl proxy for capture
        c["ewma_capture"] = (1 - self.alpha) * c["ewma_capture"] + self.alpha * cap

        for store, key in ((self.edge_buckets, edge_bucket(net_edge)),
                           (self.spread_buckets, spread_bucket(spread)),
                           (self.liquidity_buckets, liquidity_bucket(liquidity)),
                           (self.ambiguity_buckets, ambiguity_bucket(ambiguity)),
                           (self.evidence_buckets, evidence_bucket(evidence))):
            e = store.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0})
            e["n"] += 1
            e["pnl"] = round(e["pnl"] + float(realized_pnl), 6)
            e["wins"] += w

        for h, v in (markouts or {}).items():
            if h in self.markouts and v is not None:
                m = self.markouts[h]
                m["n"] += 1
                m["sum"] = round(m["sum"] + float(v), 6)

    def record_signal(self, *, strategy: str, attribution: Optional[dict] = None,
                      traded: bool = False) -> None:
        """Record the resolved strategy + its alpha attribution (Strategy
        Optimization). ``strategy`` is one of bregman_arbitrage /
        statistical_mispricing / directional / none."""
        s = self.signal_strategies.setdefault(strategy or "none",
                                              {"selected": 0, "traded": 0})
        s["selected"] += 1
        if traded:
            s["traded"] += 1
        for src, val in (attribution or {}).items():
            try:
                self.alpha_attribution[src] = round(
                    self.alpha_attribution.get(src, 0.0) + float(val), 6)
            except (TypeError, ValueError):
                continue

    # -- reads / model feedback ---------------------------------------------
    def category_reliability(self) -> dict:
        return {k: round(v.get("reliability", 0.5), 4) for k, v in self.categories.items()}

    def category_bias(self, category: str) -> float:
        """Small learned tilt for ``p_model`` (±0.04 max). 0 with no data."""
        c = self.categories.get(category or "uncategorized")
        if not c or c.get("n", 0) < self.min_bucket_samples:
            return 0.0
        return round((c.get("reliability", 0.5) - 0.5) * 0.08, 4)

    def calibration_table(self) -> list:
        rows = []
        for bucket, v in sorted(self.prob_buckets.items()):
            n = v["n"]
            if n <= 0:
                continue
            pred = v["sum_pred"] / n
            actual = v["wins"] / n
            rows.append({"bucket": bucket, "n": n, "predicted": round(pred, 4),
                         "actual": round(actual, 4), "gap": round(abs(pred - actual), 4)})
        return rows

    def calibration_error(self) -> float:
        rows = [r for r in self.calibration_table() if r["n"] >= self.min_bucket_samples]
        if not rows:
            return 0.0
        return round(sum(r["gap"] for r in rows) / len(rows), 4)

    def _resolved_pairs(self) -> list:
        """Reconstruct ``(predicted, outcome)`` pairs from the bucket stats so a
        calibration model can be fitted for the training report (Strategy
        Optimization & Robustness Testing)."""
        pairs: list = []
        for v in self.prob_buckets.values():
            n = int(v.get("n", 0))
            if n <= 0:
                continue
            pred = v.get("sum_pred", 0.0) / n
            wins = int(v.get("wins", 0))
            pairs += [(pred, 1)] * wins + [(pred, 0)] * (n - wins)
        return pairs

    def calibration_artifact(self, *, method: str = "auto") -> dict:
        """Fit + export a calibration artifact (method, slope/intercept,
        reliability buckets, before/after metrics) for the training report.

        Deterministic, offline. Falls back to the conservative shrink calibrator
        when there are too few resolved samples (Risk Management invariant)."""
        from engine.calibration_models import InstitutionalCalibrator

        cal = InstitutionalCalibrator(method=method,
                                      min_samples=self.min_bucket_samples)
        cal.fit(self._resolved_pairs())
        return cal.to_artifact()

    def markout_summary(self) -> dict:
        return {h: round(v["sum"] / v["n"], 6) if v["n"] else None
                for h, v in self.markouts.items()}

    def state(self) -> dict:
        return {
            "decisions": self.decisions, "trades": self.trades,
            "no_trades": self.no_trades, "closed": self.closed,
            "no_trade_reasons": self.no_trade_reasons,
            "prob_buckets": self.prob_buckets, "categories": self.categories,
            "edge_buckets": self.edge_buckets, "spread_buckets": self.spread_buckets,
            "liquidity_buckets": self.liquidity_buckets,
            "ambiguity_buckets": self.ambiguity_buckets,
            "evidence_buckets": self.evidence_buckets, "markouts": self.markouts,
            "signal_strategies": self.signal_strategies,
            "alpha_attribution": self.alpha_attribution,
        }

    def summary(self) -> dict:
        return {
            "decisions": self.decisions, "trades": self.trades,
            "no_trades": self.no_trades, "closed": self.closed,
            "calibration_error": self.calibration_error(),
            "calibration": self.calibration_table(),
            "calibration_artifact": self.calibration_artifact(),
            "category_reliability": self.category_reliability(),
            "edge_buckets": self.edge_buckets,
            "no_trade_reasons": self.no_trade_reasons,
            "markouts": self.markout_summary(),
            "signal_strategies": self.signal_strategies,
            "alpha_attribution": self.alpha_attribution,
        }
