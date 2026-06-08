"""Paper-only Bregman scan loop (PAPER ONLY, pure, deterministic).

This is the *activation path* for Bregman coherence arbitrage: on every market
refresh cycle it ingests the scanned markets, builds a constraint graph, projects
+ certifies, and emits scan telemetry. It is the FIRST-class edge engine and is
**independent of BTC Pulse, Grok, and news** — none of those need to be enabled
for Bregman to scan.

In paper mode the scanner is ON by default; it can only be turned off by explicit
config, and then it records a ``disabled_reason`` (no silent disable). Telemetry
guarantees that after a scan with valid market data:
``bregman_paper_enabled is True``, ``arbitrage_disabled is False``,
``constraint_groups_scanned > 0``, and every skipped group has a typed reason.

Quant responsibilities (full chain)
-----------------------------------
* **Data acquisition & ingestion** — read-only market snapshots (polymarket-client
  v2 gamma shape) are fed in; this module never calls the network.
* **Preprocessing / features** — :func:`build_constraint_graph` normalizes markets
  into outcomes + typed relationships (complement/MECE/range), recording typed
  skips for anything unusable.
* **Statistical / probabilistic modeling** — calibrated probabilities may rank
  candidates upstream; the scan itself is model-free coherence.
* **Bregman signal generation** — project to the coherent set, certify worst-case
  after-fee profit, emit only certified, fill-feasible opportunities (Bregman is
  the priority strategy).
* **Risk / portfolio** — certified opportunities still pass the deterministic
  RiskEngine + portfolio caps before any (paper) sizing.
* **Backtesting / robustness** — scan telemetry feeds the audit + benchmarks.
* **CLOB v2 execution** — execution feasibility is checked separately (paper
  multi-leg planner); the scanner only flags candidates.
* **Monitoring** — telemetry (scanned/skipped/certified) is written every cycle.
* **Compliance / security / ops** — PAPER-only; no wallet/order path; disabling
  the scanner requires an explicit, logged reason.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from engine.arbitrage.certificate import FeeModel
from engine.arbitrage.constraint_discovery import discover_constraints
from engine.strategies.bregman import BregmanResult, BregmanStrategy

logger = logging.getLogger("hte.strategies.bregman_scanner")


class BregmanPaperScanner:
    """Runs a paper Bregman coherence scan over market snapshots each cycle."""

    def __init__(self, *, enabled: bool = True, disabled_reason: Optional[str] = None,
                 fee_model: Optional[FeeModel] = None, profit_floor: float = 0.005,
                 min_depth_usd: float = 1.0, decay_half_life_s: float = 300.0,
                 venue_supports_atomic_multileg: bool = False):
        self.enabled = bool(enabled)
        self.disabled_reason = disabled_reason if not self.enabled else None
        if not self.enabled and not self.disabled_reason:
            self.disabled_reason = "disabled_without_reason"
        self.min_depth_usd = float(min_depth_usd)
        self.decay_half_life_s = float(decay_half_life_s)
        self.strategy = BregmanStrategy(
            fee_model=fee_model, profit_floor=float(profit_floor),
            decay_half_life_s=self.decay_half_life_s,
            venue_supports_atomic_multileg=bool(venue_supports_atomic_multileg))
        self.last_result: Optional[BregmanResult] = None
        self.last_discovery = None
        self.last_telemetry: dict = {}
        self.scans = 0
        if not self.enabled:
            logger.warning("BregmanPaperScanner DISABLED by config: %s", self.disabled_reason)

    def scan(self, markets: Sequence[dict], *, now: Optional[float] = None) -> dict:
        """Scan ``markets`` once and return telemetry (pure; never trades).

        When disabled, returns telemetry with ``bregman_paper_enabled=False`` and
        the recorded ``disabled_reason``. Otherwise builds the constraint graph,
        runs the Bregman strategy, and reports scanned/skipped/certified counts.
        """
        now = time.time() if now is None else now
        markets = list(markets or [])
        if not self.enabled:
            tel = self._disabled_telemetry(len(markets))
            self.last_telemetry = tel
            return tel

        fee_bps = float(getattr(self.strategy.fee_model, "taker_fee_bps", 0.0))
        disc = discover_constraints(markets, now_ms=int(now * 1000),
                                    fee_bps=fee_bps, min_depth_usd=self.min_depth_usd)
        graph, skipped = disc.graph, disc.skipped
        self.last_discovery = disc
        result = self.strategy.evaluate(graph, now=now)
        self.last_result = result
        self.scans += 1
        diag = result.audit_diagnostics(half_life_s=self.decay_half_life_s)
        dm = disc.metrics

        import os as _os
        abcas_mode = ("aggressive_paper"
                      if str(_os.getenv("AGGRESSIVE_PAPER_TRAINING", "")).strip().lower()
                      in ("1", "true", "yes", "on") else "paper")
        tel = {
            "enabled": True,
            "bregman_paper_enabled": True,
            "arbitrage_disabled": False,
            "disabled_reason": None,
            # --- ABCAS (Adaptive Bregman Combinatorial Arbitrage System) branding ---
            "abcas_enabled": True,
            "abcas_mode": abcas_mode,
            "normalized_markets": int(dm.get("normalized_markets", 0)),
            "constraint_groups_discovered": int(dm["groups_discovered"]),
            "sample_skipped_market_ids": dm.get("sample_skipped_market_ids", []),
            "abcas_feedback_samples": int(diag["constraint_groups_scanned"])
            + len(skipped),
            "constraint_groups_scanned": int(diag["constraint_groups_scanned"]),
            "groups_discovered": int(dm["groups_discovered"]),
            "groups_skipped": len(skipped),
            "skipped_groups": skipped,
            "group_type_counts": dm["group_type_counts"],
            "avg_outcomes_per_group": dm["avg_outcomes_per_group"],
            "malformed_groups_rejected": dm["malformed_groups_rejected"],
            "metadata_coverage": dm["metadata_coverage"],
            "book_coverage": dm["book_coverage"],
            "skip_reasons": dm["skip_reasons"],
            # precise, non-contradictory price/outcome diagnostics (TASK 1/2/7)
            "skip_reason_samples": dm.get("skip_reason_samples", {}),
            "non_numeric_price_count": dm.get("non_numeric_price_count", 0),
            "insufficient_outcomes_count": dm.get("insufficient_outcomes_count", 0),
            "malformed_group_count": dm.get("malformed_group_count", 0),
            "parsed_price_success_rate": dm.get("parsed_price_success_rate", 1.0),
            "incoherent_groups": int(diag["incoherent_groups"]),
            "candidate_arbitrages": int(diag["candidate_arbitrages"]),
            "certified_arbitrages": int(diag["certified_arbitrages"]),
            "executable_depth_certified": int(diag["executable_depth_certified"]),
            "rejected_fees_spread_depth_slippage": int(diag["rejected_fees_spread_depth_slippage"]),
            "rejection_reasons": diag["rejection_reasons"],
            "expected_min_profit": diag["expected_min_profit"],
            "worst_case_payoff": diag["worst_case_payoff"],
            "execution_atomicity_risk": bool(diag["execution_atomicity_risk"]),
            "opportunity_decay_half_life_s": diag["opportunity_decay_half_life_s"],
            "certified_profit": result.certified_profit,
            "normalized_quotes": len(disc.normalized_quotes),
            "markets_seen": len(markets),
            "scan_ts": round(now, 3),
            "scans": self.scans,
        }
        self.last_telemetry = tel
        logger.info("bregman scan: scanned=%d skipped=%d candidates=%d certified=%d",
                    tel["constraint_groups_scanned"], tel["groups_skipped"],
                    tel["candidate_arbitrages"], tel["certified_arbitrages"])
        return tel

    def tradeable_signals(self, *, now: Optional[float] = None) -> list:
        """Certified + fill-feasible opportunities from the last scan (router Tier-1)."""
        if self.last_result is None:
            return []
        return self.strategy.tradeable(self.last_result, now=now)

    def _disabled_telemetry(self, markets_seen: int) -> dict:
        return {
            "enabled": False,
            "bregman_paper_enabled": False,
            "arbitrage_disabled": True,
            "disabled_reason": self.disabled_reason,
            "abcas_enabled": False,
            "abcas_mode": "disabled",
            "normalized_markets": 0,
            "constraint_groups_discovered": 0,
            "sample_skipped_market_ids": [],
            "abcas_feedback_samples": 0,
            "constraint_groups_scanned": 0,
            "groups_discovered": 0,
            "groups_skipped": 0,
            "skipped_groups": [],
            "group_type_counts": {},
            "avg_outcomes_per_group": 0.0,
            "malformed_groups_rejected": 0,
            "metadata_coverage": 0.0,
            "book_coverage": 0.0,
            "skip_reasons": {},
            "skip_reason_samples": {},
            "non_numeric_price_count": 0,
            "insufficient_outcomes_count": 0,
            "malformed_group_count": 0,
            "parsed_price_success_rate": 1.0,
            "normalized_quotes": 0,
            "incoherent_groups": 0,
            "candidate_arbitrages": 0,
            "certified_arbitrages": 0,
            "executable_depth_certified": 0,
            "rejected_fees_spread_depth_slippage": 0,
            "rejection_reasons": {},
            "expected_min_profit": 0.0,
            "worst_case_payoff": 0.0,
            "execution_atomicity_risk": False,
            "opportunity_decay_half_life_s": self.decay_half_life_s,
            "certified_profit": 0.0,
            "markets_seen": int(markets_seen),
            "scan_ts": round(time.time(), 3),
            "scans": self.scans,
        }


def scanner_from_env(env: Optional[dict] = None) -> BregmanPaperScanner:
    """Build a scanner from environment (PAPER default ON).

    ``BREGMAN_PAPER_SCAN_ENABLED=0`` (or ``HTE_MODE`` not paper) disables it with a
    logged reason; otherwise it is enabled. Never enables a live path.
    """
    import os
    env = env if env is not None else os.environ
    mode = str(env.get("HTE_MODE", "paper")).strip().lower()
    raw = str(env.get("BREGMAN_PAPER_SCAN_ENABLED", "1")).strip().lower()
    explicitly_off = raw in ("0", "false", "no", "off")
    if mode != "paper":
        return BregmanPaperScanner(enabled=False,
                                   disabled_reason=f"mode={mode} is not paper")
    if explicitly_off:
        return BregmanPaperScanner(enabled=False,
                                   disabled_reason="BREGMAN_PAPER_SCAN_ENABLED=0 (config)")
    return BregmanPaperScanner(enabled=True)
