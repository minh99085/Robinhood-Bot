"""Learned Selectivity Gate v1 for the BTC 5-min pulse (PAPER ONLY).

The bot trades too many weak buckets (~52% win rate, negative edge). This gate sits BETWEEN the
strategy's directional decision and the execution gate, and uses the bot's OWN live settled-trade
evidence (per entry-time bucket) to REJECT (or, rarely, EXPLORE) candidates whose bucket has
enough samples and is proven losing. It can only make the bot MORE selective — it can never
create, force, resize, or fast-track a trade, and the strict execution gate remains the final
authority. Evidence is live (from the ledger/settlements), never hard-coded.

Also provides probability calibration (shrink the digital fair toward the empirical bucket outcome
once a bucket has enough samples) and a counterfactual replay over the existing ledger.
"""

from __future__ import annotations

import math
import random
from typing import Optional


def _wilson_upper(wins: int, n: int, z: float) -> float:
    """One-sided upper bound of the Wilson score interval for a binomial proportion."""
    if n <= 0:
        return 1.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return min(1.0, center + margin)


def breakeven_win_rate(avg_win: float, avg_loss: float) -> float:
    """The win-rate at which a bucket is EV-neutral given its OWN realized payoff: a win nets
    ``avg_win`` while a loss costs ``avg_loss`` (full stake), so breakeven = avg_loss/(avg_win+avg_loss).
    For a fixed-stake binary at price>0.5, avg_loss>avg_win so breakeven>0.5 (e.g. 3.75/5 -> 0.571)."""
    denom = float(avg_win) + float(avg_loss)
    if denom <= 0:
        return 0.5
    return float(avg_loss) / denom

# entry-time bucket dimensions the gate learns over (all present on the position's research tags)
DEFAULT_DIMS = ("hurst_regime", "zscore_bucket", "ttc_bucket", "confidence_tier", "spread_bucket",
                "depth_bucket", "markov_state", "edge_quality_bucket", "stale_divergence",
                "direction")


class SelectivityEvidence:
    """Per-(dimension, bucket) settled-trade evidence: win-rate, PnL, avg win/loss, avg EV-after-
    cost, and empirical up-rate. Fed at settlement; seeded from the existing ledger on startup."""

    def __init__(self, dims=DEFAULT_DIMS):
        self.dims = tuple(dims)
        self.buckets: dict = {d: {} for d in self.dims}

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
                "ev": 0.0, "up": 0}

    def record(self, tags: dict, *, won: bool, pnl: float, ev_after_cost: Optional[float] = None,
               outcome_up: Optional[bool] = None) -> None:
        won = bool(won)
        pnl = float(pnl or 0.0)
        ev = float(ev_after_cost or 0.0)
        for d in self.dims:
            b = (tags or {}).get(d)
            if b is None:
                continue
            s = self.buckets[d].setdefault(str(b), self._stat())
            s["n"] += 1
            s["wins"] += int(won)
            s["pnl"] = round(s["pnl"] + pnl, 6)
            if pnl > 0:
                s["gross_win"] = round(s["gross_win"] + pnl, 6)
            elif pnl < 0:
                s["gross_loss"] = round(s["gross_loss"] + (-pnl), 6)
            s["ev"] = round(s["ev"] + ev, 6)
            if outcome_up is not None:
                s["up"] += int(bool(outcome_up))

    @property
    def has_data(self) -> bool:
        return any(self.buckets[d] for d in self.dims)

    def stat(self, dim: str, bucket) -> Optional[dict]:
        s = self.buckets.get(dim, {}).get(str(bucket))
        if not s or s["n"] == 0:
            return None
        n = s["n"]
        losses = n - s["wins"]
        return {"n": n, "win_rate": round(s["wins"] / n, 4), "pnl_usd": round(s["pnl"], 4),
                "avg_win": round(s["gross_win"] / s["wins"], 6) if s["wins"] else 0.0,
                "avg_loss": round(s["gross_loss"] / losses, 6) if losses else 0.0,
                "avg_ev_after_cost": round(s["ev"] / n, 6), "up_rate": round(s["up"] / n, 4)}

    def to_state(self) -> dict:
        return {"dims": list(self.dims),
                "buckets": {d: {b: dict(s) for b, s in self.buckets[d].items()} for d in self.dims}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.buckets = {d: {} for d in self.dims}
        for d in self.dims:
            for b, s in (data.get("buckets") or {}).get(d, {}).items():
                st = self._stat()
                for k in st:
                    st[k] = (int(s.get(k, 0)) if k in ("n", "wins", "up")
                             else float(s.get(k, 0.0) or 0.0))
                self.buckets[d][b] = st


def calibrate_fair(raw_p_up: Optional[float], tags: dict, evidence: SelectivityEvidence, *,
                   min_samples: int = 30, max_shrink: float = 0.5) -> "tuple":
    """Shrink the raw digital P(up) toward the empirical up-rate of the most-sampled relevant
    bucket once that bucket clears ``min_samples``. Returns (raw, calibrated, diag|None)."""
    if raw_p_up is None:
        return raw_p_up, raw_p_up, None
    best = None
    for d in evidence.dims:
        b = (tags or {}).get(d)
        if b is None:
            continue
        st = evidence.stat(d, b)
        if st and st["n"] >= min_samples and (best is None or st["n"] > best[2]["n"]):
            best = (d, b, st)
    if best is None:
        return raw_p_up, round(float(raw_p_up), 4), None
    d, b, st = best
    n = st["n"]
    w = min(float(max_shrink), n / (n + float(min_samples)))
    cal = (1.0 - w) * float(raw_p_up) + w * float(st["up_rate"])
    return round(float(raw_p_up), 4), round(cal, 4), {
        "dimension": d, "bucket": b, "n": n, "empirical_up_rate": st["up_rate"],
        "shrink_weight": round(w, 4)}


class LearnedSelectivityGate:
    """Rejects/penalizes candidates in proven-losing buckets; can only make the bot MORE selective.
    Never trades, resizes, or bypasses the execution gate."""

    def __init__(self, *, enabled: bool = True, min_samples: int = 30, min_win_rate: float = 0.52,
                 exploration_rate: float = 0.05, confidence_z: float = 1.64,
                 seed: Optional[int] = None):
        self.enabled = bool(enabled)
        self.min_samples = int(min_samples)
        self.min_win_rate = float(min_win_rate)
        self.confidence_z = float(confidence_z)        # one-sided z for the "confidently losing" test
        self.exploration_rate = max(0.0, min(0.05, float(exploration_rate)))   # hard cap 5%
        self.accepted = 0
        self.rejected = 0
        self.explored = 0
        self.reject_reasons: dict = {}
        self.by_decision: dict = {}        # gate_decision -> settled {n,wins,pnl} (excl. headline)
        self._rng = random.Random(seed)

    def _assess(self, st: dict) -> dict:
        """Statistically-grounded verdict for one bucket stat. A bucket is 'confidently losing' only
        when (a) it actually lost money AND (b) we're confident (one-sided Wilson upper bound at
        ``confidence_z``) its win-rate is below its OWN breakeven win-rate. This replaces the old
        brittle test (`pnl<0` or `win_rate<0.52`, plus a `avg_loss>avg_win` asymmetry rule that was
        STRUCTURALLY always-true for fixed-stake binaries, so it vetoed nearly every bucket)."""
        n = int(st["n"]); wr = float(st["win_rate"])
        wins = int(round(wr * n))
        be = breakeven_win_rate(st["avg_win"], st["avg_loss"])
        upper = _wilson_upper(wins, n, self.confidence_z)
        ev_per_trade = round(wr * float(st["avg_win"]) - (1.0 - wr) * float(st["avg_loss"]), 4)
        confidently_losing = (st["pnl_usd"] < 0) and (upper < be)
        return {"n": n, "win_rate": round(wr, 4), "pnl_usd": st["pnl_usd"],
                "avg_win": st["avg_win"], "avg_loss": st["avg_loss"],
                "breakeven_win_rate": round(be, 4), "win_rate_upper_ci": round(upper, 4),
                "ev_per_trade": ev_per_trade, "confidently_losing": confidently_losing}

    def _bad_buckets(self, tags: dict, evidence: SelectivityEvidence) -> list:
        """Buckets that are CONFIDENTLY losing (enough samples + win-rate confidently below their own
        breakeven). Pure; no counters/RNG. Coin-flip / not-significant buckets are NOT rejected."""
        bad = []
        for d in evidence.dims:
            b = (tags or {}).get(d)
            if b is None:
                continue
            st = evidence.stat(d, b)
            if not st or st["n"] < self.min_samples:
                continue
            a = self._assess(st)
            if a["confidently_losing"]:
                bad.append({"dimension": d, "bucket": str(b), **a})
        return bad

    def evaluate(self, tags: dict, evidence: SelectivityEvidence) -> dict:
        """Return {decision: accept|reject|explore, reasons, bad_buckets}. Accept when the gate is
        disabled or no bucket is proven bad (cold start accepts everything)."""
        if not self.enabled:
            self.accepted += 1
            return {"decision": "accept", "reasons": ["gate_disabled"], "bad_buckets": []}
        bad = self._bad_buckets(tags, evidence)
        if not bad:
            self.accepted += 1
            return {"decision": "accept", "reasons": [], "bad_buckets": []}
        reason = "bad_bucket:%s=%s" % (bad[0]["dimension"], bad[0]["bucket"])
        if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
            self.explored += 1                    # diagnostic exploration, tracked separately
            return {"decision": "explore", "reasons": [reason], "bad_buckets": bad,
                    "exploration": True}
        self.rejected += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
        return {"decision": "reject", "reasons": [reason], "bad_buckets": bad}

    def record_settled(self, gate_decision: Optional[str], *, won: bool, pnl: float) -> None:
        s = self.by_decision.setdefault(str(gate_decision or "passed"),
                                        {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += int(bool(won))
        s["pnl"] = round(s["pnl"] + float(pnl or 0.0), 6)

    def counterfactual_replay(self, evidence: SelectivityEvidence, positions: list) -> dict:
        """Replay the gate over historical settled positions using the FINAL accumulated evidence
        (in-sample, diagnostic): how many would be rejected, losses avoided, and the resulting
        counterfactual win-rate / PnL of the trades that would remain."""
        accepted, rejected, losses_avoided, pnl_removed = [], 0, 0, 0.0
        reasons: dict = {}
        for p in positions:
            tags = p.get("tags") or {}
            won = bool(p.get("won"))
            pnl = float(p.get("pnl") or 0.0)
            bad = self._bad_buckets(tags, evidence)
            if bad:
                rejected += 1
                pnl_removed += pnl
                if not won:
                    losses_avoided += 1
                r = "bad_bucket:%s=%s" % (bad[0]["dimension"], bad[0]["bucket"])
                reasons[r] = reasons.get(r, 0) + 1
            else:
                accepted.append((won, pnl))
        base_n = len(positions)
        base_wins = sum(1 for p in positions if p.get("won"))
        base_pnl = sum(float(p.get("pnl") or 0.0) for p in positions)
        cf_n = len(accepted)
        cf_wins = sum(1 for w, _ in accepted if w)
        cf_pnl = sum(pnl for _, pnl in accepted)
        return {
            "replayed": base_n, "trades_rejected": rejected, "losses_avoided": losses_avoided,
            "pnl_removed_by_rejects": round(pnl_removed, 4),
            "counterfactual_trades": cf_n,
            "counterfactual_win_rate": (round(cf_wins / cf_n, 4) if cf_n else None),
            "counterfactual_pnl_usd": round(cf_pnl, 4),
            "baseline_trades": base_n,
            "baseline_win_rate": (round(base_wins / base_n, 4) if base_n else None),
            "baseline_pnl_usd": round(base_pnl, 4),
            "reject_reasons_by_bucket": reasons,
            "note": "in-sample replay using final accumulated bucket evidence (diagnostic estimate)",
        }

    def report(self, *, evidence: Optional[SelectivityEvidence] = None,
               positions: Optional[list] = None) -> dict:
        def dec(s):
            return {"n": s["n"], "win_rate": (round(s["wins"] / s["n"], 4) if s["n"] else None),
                    "pnl_usd": round(s["pnl"], 4)}
        pnl_by = {k: dec(v) for k, v in self.by_decision.items()}
        out = {
            "enabled": self.enabled, "observe_only_metrics": True, "affects_trading": self.enabled,
            "can_force_trade": False, "execution_gate_still_authoritative": True,
            "min_samples": self.min_samples, "min_win_rate": self.min_win_rate,
            "confidence_z": self.confidence_z, "decision_rule": "confidently_below_breakeven",
            "exploration_rate": self.exploration_rate,
            "accepted": self.accepted, "rejected": self.rejected, "explored": self.explored,
            "reject_reasons": dict(self.reject_reasons),
            "pnl_by_gate_decision": pnl_by,
            "win_rate_by_gate_decision": {k: v["win_rate"] for k, v in pnl_by.items()},
            "note": ("rejects ONLY buckets confidently below their own breakeven win-rate (Wilson "
                     "upper bound < breakeven AND net-negative PnL); coin-flip / not-significant "
                     "buckets are NOT rejected. Can only make the bot MORE selective — never trades, "
                     "resizes, or bypasses the execution gate. Exploration tracked separately."),
        }
        if evidence is not None:
            out["bucket_evidence"] = self.bucket_evidence(evidence)
        if evidence is not None and positions is not None:
            out["counterfactual"] = self.counterfactual_replay(evidence, positions)
        return out

    def bucket_evidence(self, evidence: SelectivityEvidence, *, min_samples: Optional[int] = None,
                        top: int = 8) -> dict:
        """Auditable per-bucket evidence the gate actually uses: for every dimension/bucket with
        enough samples, the realized stat + breakeven + win-rate upper CI + EV/trade + whether it is
        'confidently_losing'. Lets the operator see WHY a bucket is (or is NOT) blocked — resolving
        apparent contradictions with sub-sample reports (e.g. signal-only by_hurst_regime)."""
        ms = self.min_samples if min_samples is None else int(min_samples)
        rows = []
        for d in evidence.dims:
            for b in evidence.buckets.get(d, {}):
                st = evidence.stat(d, b)
                if not st or st["n"] < ms:
                    continue
                rows.append({"dimension": d, "bucket": str(b), **self._assess(st)})
        rows.sort(key=lambda r: (not r["confidently_losing"], r["ev_per_trade"]))
        return {"min_samples": ms, "rule": "blocked iff confidently_losing",
                "buckets": rows[:top]}

    def to_state(self) -> dict:
        return {"accepted": self.accepted, "rejected": self.rejected, "explored": self.explored,
                "reject_reasons": dict(self.reject_reasons),
                "by_decision": {k: dict(v) for k, v in self.by_decision.items()}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.accepted = int(data.get("accepted", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.reject_reasons = {k: int(v or 0) for k, v in (data.get("reject_reasons") or {}).items()}
        self.by_decision = {k: {"n": int(v.get("n", 0) or 0), "wins": int(v.get("wins", 0) or 0),
                                "pnl": float(v.get("pnl", 0.0) or 0.0)}
                            for k, v in (data.get("by_decision") or {}).items()}
