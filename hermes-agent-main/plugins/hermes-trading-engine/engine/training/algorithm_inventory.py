"""Deterministic algorithm inventory.

Audits which quantitative algorithm paths are actually wired in the engine —
probability, scan, ranking, edge, sizing, risk, fill, replay, learner, feedback,
Chainlink, Bregman arbitrage, and the (permanently disabled) legacy
cross-exchange arbitrage path. Pure + offline: it only inspects importability
and attribute presence (no network, no side effects), so it is safe to call from
tests, reports, and monitoring.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("hte.algorithm_inventory")


@dataclass
class ComponentStatus:
    name: str
    present: bool
    status: str            # "active" | "absent" | "disabled"
    module: str
    detail: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _has(module: str, attr: Optional[str] = None) -> tuple:
    """Return (present, detail). Checks the module is importable and (optionally)
    that it exposes ``attr``."""
    try:
        if importlib.util.find_spec(module) is None:
            return False, "module not found"
    except (ImportError, ValueError, ModuleNotFoundError):
        return False, "module not found"
    if attr is None:
        return True, "module present"
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001
        return False, f"import error: {exc}"
    return (hasattr(mod, attr),
            f"{attr} present" if hasattr(mod, attr) else f"{attr} missing")


def _legacy_arb_disabled() -> tuple:
    try:
        mod = importlib.import_module("engine.arb.execution")
        disabled = bool(getattr(mod, "ARBITRAGE_PERMANENTLY_DISABLED", False))
        return disabled, ("permanently disabled" if disabled else "ENABLED (unexpected)")
    except Exception as exc:  # noqa: BLE001
        return True, f"unavailable ({exc})"


# (component_key, module, attribute)
_COMPONENTS = (
    ("probability", "engine.training.probability_stack", "ProbabilityStack"),
    ("scan", "engine.training.market_scanner", "MarketScanner"),
    ("ranking", "engine.training.candidate_ranker", "CandidateRanker"),
    ("edge", "engine.training.edge_engine", "EdgeEngine"),
    ("sizing", "engine.training.paper_policy", "PaperPolicy"),
    ("risk", "engine.risk", "RiskEngine"),
    ("fill", "engine.execution.paper_broker", None),
    ("replay", "engine.replay.runner", None),
    ("learner", "engine.training.online_learner", "OnlineLearner"),
    ("feedback", "engine.training.feedback_loop", "FeedbackLoop"),
    ("research_probability", "engine.research.probability", "ProbabilityEstimator"),
    ("research_ensemble", "engine.research.ensemble", None),
    ("calibration", "engine.research.calibration_adapter", "CalibrationAdapter"),
    ("orderbook", "engine.market_data.orderbook", None),
    ("slippage", "engine.execution.slippage", None),
    ("oms", "engine.execution.oms", None),
)


def algorithm_inventory() -> dict:
    """Return a deterministic snapshot of active/absent/disabled algorithm paths.

    Notable detections:
    * ``chainlink`` — present (oracle feature layer added; default-off input).
    * ``bregman_arbitrage`` — ACTIVE (flagship Polymarket Bregman arbitrage:
      simplex grouping + divergence + fully-hedged opportunity certification).
    * ``legacy_cross_exchange_arbitrage`` — DISABLED (permanent module constant).
    """
    components: list = []
    for key, module, attr in _COMPONENTS:
        present, detail = _has(module, attr)
        components.append(ComponentStatus(
            name=key, present=present, status="active" if present else "absent",
            module=module, detail=detail))

    # Chainlink scanner (added as an additive feature layer; default-off input).
    cl_present, cl_detail = _has("engine.chainlink_scanner", "ChainlinkScanner")
    components.append(ComponentStatus(
        name="chainlink", present=cl_present,
        status="active" if cl_present else "absent",
        module="engine.chainlink_scanner",
        detail=(cl_detail + " (feature-only; default OFF)" if cl_present
                else "chainlink scanner ABSENT")))

    # Bregman arbitrage — the flagship Polymarket strategy (certification engine).
    bregman_present, bregman_detail = _has(
        "engine.training.bregman_execution", "BregmanArbitrageEngine")
    components.append(ComponentStatus(
        name="bregman_arbitrage", present=bregman_present,
        status="active" if bregman_present else "absent",
        module="engine.training.bregman_execution",
        detail=("flagship Polymarket Bregman arbitrage (certified, fully-hedged)"
                if bregman_present else "Bregman arbitrage engine ABSENT")))

    # Legacy cross-exchange arbitrage — must be permanently disabled.
    arb_disabled, arb_detail = _legacy_arb_disabled()
    components.append(ComponentStatus(
        name="legacy_cross_exchange_arbitrage", present=True,
        status="disabled" if arb_disabled else "active",
        module="engine.arb.execution", detail=arb_detail))

    by_name = {c.name: c for c in components}
    summary = {
        "components": {c.name: c.to_dict() for c in components},
        "active": sorted(c.name for c in components if c.status == "active"),
        "absent": sorted(c.name for c in components if c.status == "absent"),
        "disabled": sorted(c.name for c in components if c.status == "disabled"),
        "chainlink_present": by_name["chainlink"].present,
        "bregman_present": by_name["bregman_arbitrage"].present,
        "legacy_arb_disabled": by_name["legacy_cross_exchange_arbitrage"].status == "disabled",
        "gaps": [],
    }
    if not summary["chainlink_present"]:
        summary["gaps"].append("chainlink_scanner_absent")
    if not summary["bregman_present"]:
        summary["gaps"].append("bregman_arbitrage_not_implemented")
    log.debug("algorithm_inventory: active=%s disabled=%s gaps=%s",
              summary["active"], summary["disabled"], summary["gaps"])
    return summary
