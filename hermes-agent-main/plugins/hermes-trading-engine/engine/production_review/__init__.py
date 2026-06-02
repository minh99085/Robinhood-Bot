"""Production-canary DESIGN REVIEW package (Phase 11).

Determines whether the system is organizationally, operationally, technically,
and safety-wise ready to *design* a future production canary. It produces a
review dossier, mock-only production conformance, attestations, endpoint-
separation + credential-custody audits, runbooks, change control, human
checklist, and a formal veto.

Phase 11 NEVER implements or authorizes production order submission, production
cancellation, production signing, size increase, or autonomous live trading.
"""

from __future__ import annotations

from .config import ProductionExecutionNotImplemented, ProductionReviewConfig
from .dossier import ProductionReviewer, run_review
from .schemas import (FORBIDDEN_PRODUCTION_RECOMMENDATIONS, ProductionReviewRequest,
                      ProductionReviewResult)
from .veto import assert_safe, decide

__all__ = [
    "ProductionReviewConfig", "ProductionExecutionNotImplemented", "ProductionReviewer",
    "run_review", "ProductionReviewRequest", "ProductionReviewResult",
    "FORBIDDEN_PRODUCTION_RECOMMENDATIONS", "assert_safe", "decide",
]
