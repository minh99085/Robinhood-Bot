"""Post-canary analysis & scaling-VETO framework (Phase 10).

Analyzes every Phase 9 micro-live canary and produces a hard recommendation
(STOP / FIX_AND_REPEAT_SHADOW / REPEAT_DEMO_CANARY_SAME_SIZE /
MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN / MANUAL_REVIEW_FOR_NEXT_PHASE).

Phase 10 NEVER scales size, NEVER enables production execution, and NEVER
enables autonomous live trading. A canary is a success only if the ENTIRE chain
was clean.
"""

from __future__ import annotations

from .analyzer import PostCanaryAnalyzer, analyze_context
from .config import PostCanaryConfig
from .eligibility import compute_eligibility
from .schemas import (FORBIDDEN_RECOMMENDATIONS, PostCanaryAnalysisRequest,
                      PostCanaryAnalysisResult)
from .veto import assert_safe, decide

__all__ = [
    "PostCanaryConfig", "PostCanaryAnalyzer", "analyze_context", "compute_eligibility",
    "PostCanaryAnalysisRequest", "PostCanaryAnalysisResult", "FORBIDDEN_RECOMMENDATIONS",
    "decide", "assert_safe",
]
