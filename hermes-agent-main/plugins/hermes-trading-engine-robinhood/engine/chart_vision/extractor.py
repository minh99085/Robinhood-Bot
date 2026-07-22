"""Core analyze_tradingview_chart extraction (vision → validated JSON)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import ValidationError

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.image_utils import load_image_bytes
from engine.chart_vision.models import ChartExtractionResult
from engine.chart_vision.vision_backends import VisionBackend, get_vision_backend

logger = logging.getLogger("hermes.robinhood.chart_vision.extractor")


def analyze_tradingview_chart(
    *,
    image_base64: Optional[str] = None,
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    mime_type: Optional[str] = None,
    ticker_hint: Optional[str] = None,
    config: Optional[ChartVisionConfig] = None,
    backend: Optional[VisionBackend] = None,
) -> ChartExtractionResult:
    """
    Extract structured chart state from a TradingView image.

    Returns a validated :class:`ChartExtractionResult`. Raises on hard failures
    (unreadable image, invalid model JSON that cannot be coerced).
    """
    cfg = config or ChartVisionConfig.from_env()
    if not cfg.enabled:
        raise RuntimeError("Chart vision is disabled (CHART_VISION_ENABLED=0)")

    image_bytes, mime = load_image_bytes(
        image_base64=image_base64,
        image_url=image_url,
        image_path=image_path,
        mime_type=mime_type,
        timeout_s=min(cfg.vision_timeout_s, 60.0),
    )
    if len(image_bytes) < 32:
        raise ValueError("image payload too small")

    vb = backend or get_vision_backend(cfg)
    raw: Dict[str, Any] = vb.analyze(
        image_bytes, mime, ticker_hint=ticker_hint
    )

    # Light coercion before Pydantic
    if ticker_hint and not raw.get("ticker"):
        raw["ticker"] = ticker_hint
    if "confidence" not in raw or not isinstance(raw.get("confidence"), dict):
        raw["confidence"] = {"overall": 0.4}
    if "indicators" not in raw:
        raw["indicators"] = {}
    if "levels" not in raw:
        raw["levels"] = []
    if "extraction_warnings" not in raw:
        raw["extraction_warnings"] = []
    if "raw_model_description" not in raw:
        raw["raw_model_description"] = ""

    try:
        result = ChartExtractionResult.model_validate(raw)
    except ValidationError as exc:
        logger.warning("extraction validation failed: %s", exc)
        # Attempt minimal salvage
        salvage = {
            "ticker": str(raw.get("ticker") or ticker_hint or "UNKNOWN"),
            "timeframe": str(raw.get("timeframe") or "unknown"),
            "bias": raw.get("bias") or "unclear",
            "confidence": {"overall": 0.2},
            "raw_model_description": str(raw.get("raw_model_description") or ""),
            "extraction_warnings": [
                "pydantic_validation_failed",
                str(exc)[:500],
            ],
            "indicators": raw.get("indicators") or {},
            "levels": raw.get("levels") if isinstance(raw.get("levels"), list) else [],
            "image_last_price": raw.get("image_last_price"),
        }
        result = ChartExtractionResult.model_validate(salvage)

    result.provider = cfg.provider
    result.model = cfg.model
    if result.ticker == "UNKNOWN":
        result.extraction_warnings.append("ticker_unresolved")
    return result
