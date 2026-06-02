"""Read-only Kalshi smoke helper (Phase 6).

Verifies config, optionally syncs a few open markets and listens to public
market-data channels for a short window. NEVER calls order endpoints. Used by
``scripts/kalshi_readonly_smoke.py``.
"""

from __future__ import annotations

from typing import Optional

from ..metadata import MarketFilter
from . import KalshiVenueAdapter
from .auth import READY


def run_smoke(store=None, *, max_markets: int = 5, tickers: Optional[list[str]] = None,
              seconds: int = 30, do_sync: bool = True) -> dict:
    adapter = KalshiVenueAdapter(store=store)
    status = adapter.get_status()
    summary = {
        "venue": "kalshi", "enabled": status.enabled, "status": status.status,
        "environment": adapter.config.environment, "markets_synced": 0,
        "messages_received": 0, "seq_gaps": 0, "books_built": 0, "stale_markets": 0,
        "parse_errors": 0,
    }
    if adapter.auth_status != READY:
        summary["detail"] = adapter.auth_status
        return summary
    if do_sync:
        res = adapter.sync_metadata(MarketFilter(venue="kalshi", status="open", limit=max_markets))
        summary["markets_synced"] = res.markets_synced
    # NOTE: a real run would connect the WS loop here for `seconds`; left to the
    # script/runtime to avoid blocking. Health counters are read-only.
    if adapter.ws is not None:
        md = adapter.ws.status_snapshot()
        summary.update({"messages_received": md.messages_received, "seq_gaps": md.seq_gap_count,
                        "books_built": len(adapter.ws.books), "stale_markets": md.stale_count,
                        "parse_errors": md.parse_errors})
    return summary
