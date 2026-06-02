"""Kalshi READ-ONLY authentication + request signing (Phase 6).

This module signs ONLY read-only market-data / metadata requests and the
WebSocket handshake. It deliberately exposes **no** generic trading signer and
**no** order/cancel signing. Key material is never logged, persisted, displayed,
sent to Grok, or written to replay artifacts.

Signing scheme (Kalshi): RSA-PSS(SHA-256) over `timestamp + METHOD + PATH`,
base64-encoded. The WebSocket handshake signs `timestamp + "GET" + "/trade-api/ws/v2"`.
"""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Optional

from ..metadata import KalshiAuthConfig

WS_PATH = "/trade-api/ws/v2"
_PROD_REST = "https://api.elections.kalshi.com/trade-api/v2"
_DEMO_REST = "https://demo-api.kalshi.co/trade-api/v2"
_PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
_DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

# Status strings (contract surfaced on dashboard / API).
DISABLED = "disabled"
DISABLED_MISSING_CREDENTIALS = "disabled_missing_credentials"
DISABLED_MISSING_DEPENDENCY = "disabled_missing_dependency"
READY = "ready"

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                     re.DOTALL)


def redact(text: str) -> str:
    """Strip Kalshi key material / access-key ids from any string before logging."""
    if not text:
        return text
    out = str(text)
    out = _PEM_RE.sub("[REDACTED_PRIVATE_KEY]", out)
    for env in ("KALSHI_ACCESS_KEY_ID", "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_PASSWORD"):
        val = os.getenv(env)
        if val:
            out = out.replace(val, "[REDACTED]")
    return out


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def resolve_urls(environment: str) -> tuple[str, str]:
    env = (environment or "demo").lower()
    rest = _env("KALSHI_REST_BASE_URL") or (_PROD_REST if env == "prod" else _DEMO_REST)
    ws = _env("KALSHI_WS_URL") or (_PROD_WS if env == "prod" else _DEMO_WS)
    return rest, ws


def build_auth_config() -> KalshiAuthConfig:
    environment = _env("KALSHI_ENV", "demo").lower()
    rest, ws = resolve_urls(environment)
    enabled = _env("KALSHI_ENABLED", "0") not in ("0", "false", "False", "")
    key_present = bool(_env("KALSHI_ACCESS_KEY_ID"))
    pk_present = bool(_env("KALSHI_PRIVATE_KEY_PATH") or _env("KALSHI_PRIVATE_KEY_PEM"))
    return KalshiAuthConfig(
        environment=environment, rest_base_url=rest, ws_url=ws,
        access_key_id_present=key_present, private_key_present=pk_present, enabled=enabled)


class ReadOnlyKalshiSigner:
    """Narrow signer for read-only GET requests + the WS handshake ONLY.

    There is intentionally no ``sign_order`` / ``sign_trade`` method here.
    """

    def __init__(self, access_key_id: str, private_key, environment: str = "demo"):
        self._access_key_id = access_key_id
        self._private_key = private_key  # cryptography RSAPrivateKey (never logged)
        self.environment = environment

    # -- message builders (no key needed; safe to unit-test) ------------ #
    @staticmethod
    def rest_signing_message(timestamp_ms: str, method: str, path: str) -> str:
        return f"{timestamp_ms}{method.upper()}{path}"

    @staticmethod
    def ws_signing_message(timestamp_ms: str) -> str:
        return f"{timestamp_ms}GET{WS_PATH}"

    def _sign(self, message: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sig = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return base64.b64encode(sig).decode("ascii")

    def rest_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self._access_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(self.rest_signing_message(ts, method, path)),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def ws_headers(self) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self._access_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(self.ws_signing_message(ts)),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def __repr__(self) -> str:  # never leak key material
        return f"<ReadOnlyKalshiSigner env={self.environment} access_key=[REDACTED] key=[REDACTED]>"

    __str__ = __repr__


def _load_private_key():
    """Load the RSA private key from path or PEM env. Returns (key, error_status)."""
    try:
        from cryptography.hazmat.primitives import serialization
    except Exception:  # noqa: BLE001
        return None, DISABLED_MISSING_DEPENDENCY
    pem_bytes: Optional[bytes] = None
    path = _env("KALSHI_PRIVATE_KEY_PATH")
    if path:
        try:
            pem_bytes = Path(path).read_bytes()
        except OSError:
            return None, DISABLED_MISSING_CREDENTIALS
    elif _env("KALSHI_PRIVATE_KEY_PEM"):
        pem_bytes = _env("KALSHI_PRIVATE_KEY_PEM").encode("utf-8")
    if not pem_bytes:
        return None, DISABLED_MISSING_CREDENTIALS
    password = _env("KALSHI_PRIVATE_KEY_PASSWORD") or None
    try:
        key = serialization.load_pem_private_key(
            pem_bytes, password=password.encode("utf-8") if password else None)
        return key, READY
    except Exception:  # noqa: BLE001 — bad/locked key: degrade, never crash
        return None, DISABLED_MISSING_CREDENTIALS


def load_kalshi_auth() -> tuple[KalshiAuthConfig, Optional[ReadOnlyKalshiSigner], str]:
    """Build (config, signer_or_None, status). Missing creds degrade gracefully."""
    cfg = build_auth_config()
    if not cfg.enabled:
        return cfg, None, DISABLED
    access_key = _env("KALSHI_ACCESS_KEY_ID")
    if not access_key or not cfg.private_key_present:
        return cfg, None, DISABLED_MISSING_CREDENTIALS
    key, status = _load_private_key()
    if key is None:
        return cfg, None, status
    return cfg, ReadOnlyKalshiSigner(access_key, key, cfg.environment), READY
