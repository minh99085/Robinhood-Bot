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

import copy
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hte.training.online_learner")

from .metrics import (ambiguity_bucket, edge_bucket, evidence_bucket,
                      liquidity_bucket, prob_bucket, spread_bucket)
from .settlement import is_trainable_state

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
        # Settlement-label quality: only clean (trainable) labels mutate state;
        # dirty labels (void/ambiguous/unresolved/partially_invalid/stale) are
        # counted here and otherwise ignored (no calibration/bucket pollution).
        self.suppressed_outcomes = 0
        self.label_states: dict = {}
        self.no_trade_reasons: dict = {}
        self.variant_decisions: dict = {}   # variant -> {traded, no_trade}
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
        # anti-overfitting: stable-state checkpoint + auto-rollback bookkeeping
        self.rollbacks = 0
        self._stable_snapshot: Optional[dict] = None
        self._stable_score: Optional[float] = None
        self._load()

    # -- persistence ---------------------------------------------------------
    def _apply_state(self, d: dict) -> None:
        """Load every learned-state field from a state dict (used by both the
        on-disk loader and :meth:`restore` for anti-overfit rollback)."""
        for k in ("decisions", "trades", "no_trades", "closed",
                  "suppressed_outcomes"):
            setattr(self, k, int(d.get(k, 0)))
        self.label_states = dict(d.get("label_states", {}))
        self.no_trade_reasons = dict(d.get("no_trade_reasons", {}))
        self.variant_decisions = dict(d.get("variant_decisions", {}))
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

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return
            self._apply_state(d)

    def persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state(), default=str), encoding="utf-8")
        except OSError:
            pass

    # -- anti-overfitting: stable snapshot + automatic rollback --------------
    def snapshot(self) -> dict:
        """Deep copy of the current learned state (for a stable checkpoint)."""
        return copy.deepcopy(self.state())

    def restore(self, snap: dict) -> None:
        """Restore learned state from a snapshot taken by :meth:`snapshot`."""
        self._apply_state(copy.deepcopy(snap))

    def checkpoint_stable(self, *, validation_error: Optional[float] = None) -> None:
        """Mark the current learned state as the last KNOWN-GOOD checkpoint.

        ``validation_error`` is the out-of-sample calibration error that
        certified this state; if omitted, the in-sample calibration error is
        used. :meth:`maybe_rollback` reverts to this checkpoint when a later
        validation error degrades past tolerance — the anti-overfit safety net
        for aggressive paper learning.
        """
        self._stable_snapshot = self.snapshot()
        self._stable_score = (float(validation_error) if validation_error is not None
                              else self.calibration_error())

    def maybe_rollback(self, validation_error: float, *, tolerance: float = 0.05) -> bool:
        """Roll back to the last stable checkpoint when validation degrades.

        Returns True iff a rollback was performed. No-op when there is no stable
        checkpoint or the validation error is within ``tolerance`` of the
        checkpoint's certified error.
        """
        if self._stable_snapshot is None:
            return False
        base = self._stable_score if self._stable_score is not None else 0.0
        if float(validation_error) > base + float(tolerance):
            self.restore(self._stable_snapshot)
            self.rollbacks += 1
            logger.info("online_learner rollback: val_err=%.4f > stable=%.4f + tol=%.4f",
                        float(validation_error), base, float(tolerance))
            return True
        return False

    # -- recording -----------------------------------------------------------
    def record_decision(self, *, traded: bool, reason: str = "",
                        variant: Optional[str] = None) -> None:
        self.decisions += 1
        if traded:
            self.trades += 1
        else:
            self.no_trades += 1
            self.no_trade_reasons[reason] = self.no_trade_reasons.get(reason, 0) + 1
        # optional per-variant decision tally for controlled experiments
        if variant:
            v = self.variant_decisions.setdefault(variant, {"traded": 0, "no_trade": 0})
            v["traded" if traded else "no_trade"] += 1

    def record_outcome(self, *, predicted_prob: float, win: bool, realized_pnl: float,
                       category: str = "uncategorized", net_edge: float = 0.0,
                       spread: float = 0.0, liquidity: float = 0.0,
                       ambiguity: float = 0.0, evidence: float = 0.0,
                       markouts: Optional[dict] = None,
                       label_state: Optional[str] = None,
                       trainable: Optional[bool] = None) -> bool:
        """Record a closed-trade outcome. Returns True if it trained the model.

        SETTLEMENT-TRUTH GUARD: a closed position only becomes a training label
        when it carries a clean (``resolved_yes`` / ``resolved_no``) settlement
        label. ``label_state=None`` is treated as a legacy clean label for
        back-compat. Dirty labels are counted in ``suppressed_outcomes`` and make
        NO mutation to calibration / category / bucket / markout state — this is
        what stops aggressive paper trading from poisoning the probability stack.
        """
        if label_state is not None:
            self.label_states[label_state] = self.label_states.get(label_state, 0) + 1
        if trainable is None:
            trainable = is_trainable_state(label_state)
        if not trainable:
            self.suppressed_outcomes += 1
            return False

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
        return True

    def label_quality(self) -> dict:
        """Settlement-label quality summary (coverage / suppression / states)."""
        terminal = self.closed + self.suppressed_outcomes
        suppressed_unresolved = int(self.label_states.get("unresolved", 0))
        labelled = sum(self.label_states.values())
        return {
            "clean_trained": self.closed,
            "suppressed": self.suppressed_outcomes,
            "label_states": dict(self.label_states),
            "suppression_rate": round(self.suppressed_outcomes / terminal, 4) if terminal else 0.0,
            "ambiguous_rate": round(self.label_states.get("ambiguous", 0) / labelled, 4)
            if labelled else 0.0,
            "label_coverage": round((labelled - suppressed_unresolved) / labelled, 4)
            if labelled else 0.0,
        }

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

    def calibration_instability(self) -> float:
        """Calibration INSTABILITY in [0,1]: the dispersion (std) of the
        per-probability-bucket reliability gaps, normalized. 0 = uniformly
        calibrated across buckets; high = calibration error swings bucket-to-bucket
        (an unstable calibrator the Bayesian shrink must distrust). Feeds
        :func:`engine.training.probability_stack.bayesian_shrink`."""
        rows = [r for r in self.calibration_table() if r["n"] >= self.min_bucket_samples]
        gaps = [float(r["gap"]) for r in rows]
        if len(gaps) < 2:
            return 0.0
        mean = sum(gaps) / len(gaps)
        var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        # normalize: a 0.25 std is treated as fully unstable
        return round(max(0.0, min(1.0, (var ** 0.5) / 0.25)), 6)

    # -- active-learning feedback-value inputs -------------------------------
    def category_samples(self, category: str) -> int:
        """Number of resolved samples recorded for a category (drives the
        per-category under-sampling term of the active-learning feedback value)."""
        c = self.categories.get(category or "uncategorized")
        return int(c.get("n", 0)) if c else 0

    def category_sample_counts(self) -> dict:
        return {k: int(v.get("n", 0)) for k, v in self.categories.items()}

    def calibration_gap_at(self, prob: float) -> float:
        """Reliability gap |predicted - actual| for the probability bucket that
        contains ``prob`` (0 when too few samples). A weakly-calibrated region is
        exactly where an exploratory paper trade teaches the model the most."""
        b = self.prob_buckets.get(prob_bucket(prob))
        if not b or int(b.get("n", 0)) < self.min_bucket_samples:
            return 0.0
        n = int(b["n"])
        pred = b.get("sum_pred", 0.0) / n
        actual = int(b.get("wins", 0)) / n
        return round(abs(pred - actual), 6)

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
            "suppressed_outcomes": self.suppressed_outcomes,
            "label_states": self.label_states,
            "no_trade_reasons": self.no_trade_reasons,
            "variant_decisions": self.variant_decisions,
            "prob_buckets": self.prob_buckets, "categories": self.categories,
            "edge_buckets": self.edge_buckets, "spread_buckets": self.spread_buckets,
            "liquidity_buckets": self.liquidity_buckets,
            "ambiguity_buckets": self.ambiguity_buckets,
            "evidence_buckets": self.evidence_buckets, "markouts": self.markouts,
            "signal_strategies": self.signal_strategies,
            "alpha_attribution": self.alpha_attribution,
            "rollbacks": self.rollbacks,
        }

    def summary(self) -> dict:
        return {
            "decisions": self.decisions, "trades": self.trades,
            "no_trades": self.no_trades, "closed": self.closed,
            "suppressed_outcomes": self.suppressed_outcomes,
            "label_quality": self.label_quality(),
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
