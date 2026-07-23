"""Core analyze_tradingview_chart extraction (vision → validated JSON)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.image_utils import load_image_bytes
from engine.chart_vision.models import Bias, ChartExtractionResult
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

    Double reading (radiology-style): the image is read
    ``config.ensemble_reads`` times independently and the reads are merged
    under agreement rules — reads that agree pass through; disagreement on
    ticker/bias/RSI/price lowers confidence instead of trusting one look.

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
    n_reads = max(1, int(getattr(cfg, "ensemble_reads", 1)))
    reads = [
        _read_once(vb, image_bytes, mime, ticker_hint, cfg)
        for _ in range(n_reads)
    ]
    if len(reads) == 1:
        return reads[0]
    return merge_reads(reads)


def _read_once(
    vb: VisionBackend,
    image_bytes: bytes,
    mime: str,
    ticker_hint: Optional[str],
    cfg: ChartVisionConfig,
) -> ChartExtractionResult:
    """One vision read → validated ChartExtractionResult."""
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


def merge_reads(reads: List[ChartExtractionResult]) -> ChartExtractionResult:
    """Merge N independent reads of the same chart under agreement rules.

    Deterministic, no tunable knobs: confidence starts at the MINIMUM of the
    reads (weakest look) and each disagreement multiplies it down. Values
    that agree are averaged (cancelling read noise); a bias split falls back
    to neutral rather than picking a side.
    """
    base = reads[0].model_copy(deep=True)
    overall = min(float(r.confidence.overall) for r in reads)
    factor = 1.0
    warnings: List[str] = [f"ensemble_reads:{len(reads)}"]

    tickers = {r.ticker for r in reads if r.ticker and r.ticker != "UNKNOWN"}
    if len(tickers) > 1:
        factor *= 0.5
        warnings.append("ensemble_ticker_disagreement:" + "|".join(sorted(tickers)))

    biases = {r.bias for r in reads}
    if len(biases) > 1:
        base.bias = Bias.NEUTRAL
        factor *= 0.7
        warnings.append(
            "ensemble_bias_disagreement:" + "|".join(sorted(b.value for b in biases)))

    rsis = [r.indicators.rsi.value for r in reads
            if r.indicators.rsi.value is not None]
    if len(rsis) == len(reads) and rsis:
        spread = max(rsis) - min(rsis)
        base.indicators.rsi.value = round(sum(rsis) / len(rsis), 1)
        if spread > 8.0:
            factor *= 0.7
            warnings.append(f"ensemble_rsi_disagreement:spread={spread:.0f}")

    prices = [r.image_last_price for r in reads if r.image_last_price]
    if len(prices) == len(reads) and prices:
        mean_p = sum(prices) / len(prices)
        base.image_last_price = round(mean_p, 4)
        if mean_p > 0 and (max(prices) - min(prices)) / mean_p > 0.01:
            factor *= 0.8
            warnings.append("ensemble_price_disagreement")

    base.confidence.overall = max(0.0, min(1.0, overall * factor))
    base.extraction_warnings.extend(warnings)
    return base
