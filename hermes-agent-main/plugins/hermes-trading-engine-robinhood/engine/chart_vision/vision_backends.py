"""Multimodal vision backends for structured chart extraction."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import httpx

from engine.chart_vision.config import ChartVisionConfig
from engine.chart_vision.image_utils import to_base64, to_data_url
from engine.chart_vision.prompts import EXTRACTION_SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger("hermes.robinhood.chart_vision.vision")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json_object(text: str) -> Dict[str, Any]:
    """Parse model text into a JSON object; strip fences if needed."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    m = _JSON_FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    # Find outermost braces
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in model response")
    return json.loads(raw[start : end + 1])


class VisionBackend(ABC):
    @abstractmethod
    def analyze(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        ticker_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return raw parsed JSON dict (not yet Pydantic-validated)."""


class MockVisionBackend(VisionBackend):
    """Deterministic backend for tests / offline demos."""

    def __init__(self, fixed: Optional[Dict[str, Any]] = None) -> None:
        self.fixed = fixed

    def analyze(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        ticker_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.fixed is not None:
            return dict(self.fixed)
        ticker = (ticker_hint or "AAPL").upper()
        return {
            "ticker": ticker,
            "timeframe": "1H",
            "indicators": {
                "rsi": {"value": 57.5, "zone": "neutral", "confidence": 0.75},
                "macd": {
                    "macd_line": 0.35,
                    "signal_line": 0.20,
                    "histogram": 0.15,
                    "cross": "bullish_cross",
                    "confidence": 0.7,
                },
                "extras": {},
            },
            "levels": [
                {"price": 185.0, "kind": "support", "strength": 0.7, "label": "S1"},
                {"price": 195.0, "kind": "resistance", "strength": 0.65, "label": "R1"},
            ],
            "bias": "bullish",
            "confidence": {
                "ticker": 0.85 if ticker_hint else 0.6,
                "timeframe": 0.8,
                "indicators": 0.75,
                "levels": 0.65,
                "bias": 0.7,
                "price": 0.55,
                "overall": 0.7,
            },
            "raw_model_description": (
                f"Mock extraction for {ticker}: mild bullish structure with "
                "RSI mid-range and MACD histogram positive."
            ),
            "extraction_warnings": ["mock_backend"],
            "image_last_price": 190.0,
        }


class OpenAICompatibleVisionBackend(VisionBackend):
    """OpenAI chat/completions-style vision (also used for xAI Grok)."""

    def __init__(self, config: ChartVisionConfig) -> None:
        self.config = config

    def analyze(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        ticker_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.api_key:
            raise RuntimeError(
                f"{self.config.provider} API key missing "
                "(set CHART_VISION_API_KEY or provider key env)"
            )
        data_url = to_data_url(image_bytes, mime_type)
        user_text = build_user_prompt(ticker_hint=ticker_hint)
        payload = {
            "model": self.config.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        url = self.config.api_base.rstrip("/") + "/chat/completions"
        with httpx.Client(timeout=self.config.vision_timeout_s) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return extract_json_object(content)


class AnthropicVisionBackend(VisionBackend):
    def __init__(self, config: ChartVisionConfig) -> None:
        self.config = config

    def analyze(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        ticker_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY / CHART_VISION_API_KEY missing")
        b64 = to_base64(image_bytes)
        # Anthropic expects image/jpeg, image/png, image/gif, image/webp
        media = mime_type if mime_type in (
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
        ) else "image/png"
        user_text = build_user_prompt(ticker_hint=ticker_hint)
        payload = {
            "model": self.config.model,
            "max_tokens": 4096,
            "temperature": 0.1,
            "system": EXTRACTION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        url = self.config.api_base.rstrip("/") + "/v1/messages"
        with httpx.Client(timeout=self.config.vision_timeout_s) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        parts = body.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return extract_json_object(text)


class GoogleVisionBackend(VisionBackend):
    def __init__(self, config: ChartVisionConfig) -> None:
        self.config = config

    def analyze(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        ticker_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.api_key:
            raise RuntimeError("GOOGLE_API_KEY / CHART_VISION_API_KEY missing")
        b64 = to_base64(image_bytes)
        user_text = build_user_prompt(ticker_hint=ticker_hint)
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": EXTRACTION_SYSTEM_PROMPT + "\n\n" + user_text},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        model = self.config.model
        url = (
            f"{self.config.api_base.rstrip('/')}/models/{model}:generateContent"
            f"?key={self.config.api_key}"
        )
        with httpx.Client(timeout=self.config.vision_timeout_s) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise ValueError("Gemini returned no candidates")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return extract_json_object(text)


def get_vision_backend(config: ChartVisionConfig) -> VisionBackend:
    if config.provider == "mock":
        return MockVisionBackend()
    if config.provider == "openai":
        return OpenAICompatibleVisionBackend(config)
    if config.provider == "xai":
        return OpenAICompatibleVisionBackend(config)
    if config.provider == "anthropic":
        return AnthropicVisionBackend(config)
    if config.provider == "google":
        return GoogleVisionBackend(config)
    logger.warning("Unknown provider %s — using mock", config.provider)
    return MockVisionBackend()
