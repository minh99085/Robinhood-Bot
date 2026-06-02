"""Polymarket micro-live broker (Phase 9) — SAFE FALLBACK (Option 2).

The Polymarket CLOB live order flow requires EIP-712 wallet signing via an
external SDK (``py-clob-client``) that is NOT a dependency of this repo. Rather
than ship a half-wired signer, this broker is intentionally NOT-IMPLEMENTED for
live signing: it can validate a would-be FOK payload shape but CANNOT sign or
submit. Any submit attempt raises NotImplementedLiveSigning. Tests assert it
cannot submit.

This means Phase 9 cannot move real funds on Polymarket. Implementing live
Polymarket execution is a deliberate future step requiring the signing
dependency, key handling review, and its own conformance pass."""

from __future__ import annotations

from typing import Optional

from .errors import MicroLiveDisabled, NotImplementedLiveSigning
from .live_broker_base import LiveBrokerBase

POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED = "POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED"


class PolymarketLiveBroker(LiveBrokerBase):
    venue = "polymarket"

    @staticmethod
    def signing_available() -> bool:
        try:  # pragma: no cover - dependency intentionally absent
            import py_clob_client  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def validate_payload(self, payload) -> list[str]:
        """Validate FOK shape only — never signs or submits."""
        errs = []
        if getattr(payload, "order_type", None) != "FOK":
            errs.append("order_type_must_be_FOK")
        if getattr(payload, "time_in_force", None) != "fill_or_kill":
            errs.append("tif_must_be_fill_or_kill")
        return errs

    def submit_fok_canary_order(self, payload, client_order_id: str) -> dict:
        self._require_locks("submit_fok_canary_order")
        raise NotImplementedLiveSigning(POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED)

    def get_account_snapshot(self) -> dict:
        raise NotImplementedLiveSigning(POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED)

    def emergency_cancel_order(self, order_id: str) -> dict:
        raise NotImplementedLiveSigning(POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED)

    def emergency_cancel_all_for_market(self, market_ticker: str) -> dict:
        raise NotImplementedLiveSigning(POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED)

    def health(self) -> dict:
        h = super().health()
        h["live_signing"] = "not_implemented"
        h["reason"] = POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED
        return h
