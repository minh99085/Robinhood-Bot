"""
Full pipeline: image → vision → MCP validation → Monte Carlo → decision.

Monte Carlo runs in a worker thread via the Monte-Carlo-Sim package when
``MONTE_CARLO_SIM_PATH`` is configured. Execution never bypasses safety gates;
this module only produces recommendations (unless mode is gated_execution,
which still requires SafeRobinhoodClient for any place_* call).
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.extractor import analyze_tradingview_chart
from engine.chart_vision.mcp_validator import fetch_mcp_snapshot, validate_extraction
from engine.chart_vision.models import (
    AnalyzeChartResponse,
    ChartExtractionResult,
    MCPMarketSnapshot,
    ValidationResult,
    ValidationStatus,
)
from engine.chart_vision.vision_backends import VisionBackend
from engine.robinhood.audit_log import AuditLog

logger = logging.getLogger("hermes.robinhood.chart_vision.pipeline")

_MC_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mc_chart_vision")


def _ensure_mc_path(mc_path: str) -> bool:
    p = Path(mc_path)
    if not p.is_dir():
        return False
    sp = str(p.resolve())
    if sp not in sys.path:
        sys.path.insert(0, sp)
    return True


def _run_mc_decision(
    extraction: ChartExtractionResult,
    mcp: Optional[MCPMarketSnapshot],
    validation: Optional[ValidationResult],
    config: ChartVisionConfig,
) -> Dict[str, Any]:
    """Import Monte-Carlo-Sim and run chart_vision_pipeline (sync)."""
    if not _ensure_mc_path(config.monte_carlo_sim_path):
        return {
            "error": f"MONTE_CARLO_SIM_PATH not found: {config.monte_carlo_sim_path}",
            "skipped": True,
        }
    try:
        from chart_vision_models import (  # type: ignore
            ChartExtractionResult as MCExtraction,
            MCPMarketSnapshot as MCMCP,
            ValidationResult as MCValidation,
        )
        from chart_vision_pipeline import run_chart_vision_mc  # type: ignore
    except ImportError as exc:
        return {"error": f"Monte-Carlo-Sim import failed: {exc}", "skipped": True}

    mc_extraction = MCExtraction.model_validate(extraction.model_dump(mode="json"))
    mc_mcp = (
        MCMCP.model_validate(mcp.model_dump(mode="json")) if mcp is not None else None
    )
    mc_val = (
        MCValidation.model_validate(validation.model_dump(mode="json"))
        if validation is not None
        else None
    )

    paths = config.mc_paths
    if config.execution_mode == "log_only":
        # Still allow MC for observability but callers may use fewer paths via env
        pass

    decision = run_chart_vision_mc(
        mc_extraction,
        mcp=mc_mcp,
        validation=mc_val,
        paths=paths,
        horizon_days=config.mc_horizon_days,
        seed=config.mc_seed,
        execution_mode=config.execution_mode,
        max_order_notional_usd=config.max_order_notional_usd,
        max_position_pct=config.max_position_pct,
        risk_per_trade_pct=config.risk_per_trade_pct,
    )
    return decision.model_dump(mode="json")


async def run_full_pipeline(
    *,
    image_base64: Optional[str] = None,
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    mime_type: Optional[str] = None,
    ticker_hint: Optional[str] = None,
    run_validation: bool = True,
    run_monte_carlo: Optional[bool] = None,
    mc_paths: Optional[int] = None,
    execution_mode: Optional[str] = None,
    config: Optional[ChartVisionConfig] = None,
    backend: Optional[VisionBackend] = None,
    mcp_client: Any = None,
    audit: Optional[AuditLog] = None,
) -> AnalyzeChartResponse:
    """
    End-to-end chart analysis.

    Parameters
    ----------
    mcp_client :
        ``SafeRobinhoodClient`` or adapter with ``async call_tool``.
        If None, validation is skipped (or rejected if require_mcp).
    """
    cfg = config or ChartVisionConfig.from_env()
    if execution_mode:
        # frozen dataclass — rebuild lightly
        from dataclasses import replace

        if execution_mode in ("log_only", "recommendation_only", "gated_execution"):
            cfg = replace(cfg, execution_mode=execution_mode)  # type: ignore[arg-type]
    if mc_paths is not None and mc_paths > 0:
        from dataclasses import replace

        cfg = replace(cfg, mc_paths=int(mc_paths))
    do_mc = cfg.run_monte_carlo if run_monte_carlo is None else bool(run_monte_carlo)

    audit_id = str(uuid.uuid4())
    warnings: list[str] = []
    t0 = time.time()

    if audit:
        audit.record(
            "chart_vision_start",
            tool="analyze_tradingview_chart",
            details={
                "audit_id": audit_id,
                "has_url": bool(image_url),
                "has_path": bool(image_path),
                "has_b64": bool(image_base64),
                "ticker_hint": ticker_hint,
            },
        )

    try:
        extraction = analyze_tradingview_chart(
            image_base64=image_base64,
            image_url=image_url,
            image_path=image_path,
            mime_type=mime_type,
            ticker_hint=ticker_hint,
            config=cfg,
            backend=backend,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("vision extraction failed")
        if audit:
            audit.record(
                "chart_vision_error",
                tool="analyze_tradingview_chart",
                reason=str(exc),
                details={"audit_id": audit_id},
            )
        return AnalyzeChartResponse(
            ok=False,
            error=f"extraction_failed: {exc}",
            audit_id=audit_id,
        )

    mcp_snap: Optional[MCPMarketSnapshot] = None
    validation: Optional[ValidationResult] = None

    if run_validation:
        if mcp_client is not None:
            try:
                mcp_snap = await fetch_mcp_snapshot(mcp_client, extraction.ticker)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"mcp_fetch_failed: {exc}")
                logger.warning("MCP fetch failed: %s", exc)
                mcp_snap = MCPMarketSnapshot(
                    ticker=extraction.ticker, errors=[str(exc)]
                )
            validation = validate_extraction(
                extraction, mcp_snap, cfg, mcp_available=True
            )
        else:
            validation = validate_extraction(
                extraction, None, cfg, mcp_available=False
            )
            warnings.append("no_mcp_client")

        if audit and validation:
            audit.record(
                "chart_vision_validation",
                tool="analyze_tradingview_chart",
                allowed=validation.status != ValidationStatus.REJECTED,
                reason=validation.status.value,
                details={
                    "audit_id": audit_id,
                    "discrepancies": [
                        d.model_dump(mode="json") for d in validation.discrepancies
                    ],
                    "adjusted_confidence": validation.adjusted_confidence,
                    "notes": validation.notes,
                },
            )

    decision: Optional[Dict[str, Any]] = None
    if do_mc:
        if (
            validation is not None
            and validation.status == ValidationStatus.REJECTED
            and cfg.execution_mode != "log_only"
        ):
            # Still run MC for auditability with forced flat mapping
            warnings.append("mc_run_after_reject_for_audit")
        try:
            loop_decision = await _run_mc_async(
                extraction, mcp_snap, validation, cfg
            )
            decision = loop_decision
            if decision.get("skipped"):
                warnings.append(decision.get("error") or "mc_skipped")
                decision = None
        except Exception as exc:  # noqa: BLE001
            logger.exception("MC pipeline failed")
            warnings.append(f"mc_failed: {exc}")

    elapsed = time.time() - t0
    if audit:
        audit.record(
            "chart_vision_complete",
            tool="analyze_tradingview_chart",
            allowed=True,
            details={
                "audit_id": audit_id,
                "ticker": extraction.ticker,
                "bias": extraction.bias.value,
                "validation": validation.status.value if validation else None,
                "has_decision": decision is not None,
                "action": (decision or {}).get("action"),
                "elapsed_s": round(elapsed, 3),
                "execution_mode": cfg.execution_mode,
                "warnings": warnings,
            },
        )

    return AnalyzeChartResponse(
        ok=True,
        extraction=extraction,
        validation=validation,
        mcp=mcp_snap,
        decision=decision,
        warnings=warnings,
        audit_id=audit_id,
    )


async def _run_mc_async(
    extraction: ChartExtractionResult,
    mcp: Optional[MCPMarketSnapshot],
    validation: Optional[ValidationResult],
    config: ChartVisionConfig,
) -> Dict[str, Any]:
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _MC_EXECUTOR,
        lambda: _run_mc_decision(extraction, mcp, validation, config),
    )
