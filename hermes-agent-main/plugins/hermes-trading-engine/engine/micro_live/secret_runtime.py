"""Secret runtime (Phase 9). Trading secrets are loaded ONLY after every lock
passes, kept in memory, never persisted, never logged. Provides a TRADING signer
that is separate from the Phase 6 read-only signer."""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Optional

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                     re.DOTALL)
_HEX_KEY_RE = re.compile(r"0x[0-9a-fA-F]{40,}")

# Env vars whose VALUES must never appear in logs/reports/artifacts/API.
SECRET_ENV_VARS = (
    "KALSHI_TRADING_ACCESS_KEY_ID", "KALSHI_TRADING_PRIVATE_KEY_PEM",
    "KALSHI_TRADING_PRIVATE_KEY_PASSWORD", "KALSHI_ACCESS_KEY_ID",
    "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_PASSWORD",
    "POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE", "POLYMARKET_FUNDER_ADDRESS",
    "MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK",
)


def redact(text) -> str:
    if text is None:
        return text
    out = str(text)
    out = _PEM_RE.sub("[REDACTED_PRIVATE_KEY]", out)
    out = _HEX_KEY_RE.sub("[REDACTED_KEY]", out)
    for env in SECRET_ENV_VARS:
        val = os.getenv(env)
        if val:
            out = out.replace(val, "[REDACTED]")
    return out


def redact_dict(d: dict) -> dict:
    """Return a copy with secret-looking keys redacted (defense in depth)."""
    sensitive = ("key", "secret", "passphrase", "password", "token", "signature",
                 "private", "pem", "wallet", "signer", "credential")
    out = {}
    for k, v in (d or {}).items():
        kl = str(k).lower()
        if any(s in kl for s in sensitive):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, str):
            out[k] = redact(v)
        else:
            out[k] = v
    return out


class KalshiTradingSigner:
    """RSA-PSS(SHA-256) signer for Kalshi *trading* requests. Distinct from the
    Phase 6 read-only signer; only instantiated by ``load_kalshi_trading_signer``
    after all locks pass. Never logs key material."""

    def __init__(self, access_key_id: str, private_key, environment: str = "demo"):
        self._access_key_id = access_key_id
        self._private_key = private_key
        self.environment = environment

    @staticmethod
    def signing_message(timestamp_ms: str, method: str, path: str) -> str:
        return f"{timestamp_ms}{method.upper()}{path}"

    def _sign(self, message: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        sig = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {"KALSHI-ACCESS-KEY": self._access_key_id,
                "KALSHI-ACCESS-SIGNATURE": self._sign(self.signing_message(ts, method, path)),
                "KALSHI-ACCESS-TIMESTAMP": ts}

    def __repr__(self) -> str:
        return f"<KalshiTradingSigner env={self.environment} key=[REDACTED]>"

    __str__ = __repr__


def load_kalshi_trading_signer(locks_ok: bool) -> tuple[Optional[KalshiTradingSigner], str]:
    """Return (signer_or_None, status). Refuses unless locks pass and the Kalshi
    micro-live runtime flag + trading credentials are present."""
    if not locks_ok:
        return None, "locks_not_open"
    if os.getenv("KALSHI_MICRO_LIVE_ENABLED", "0") in ("0", "false", "False", ""):
        return None, "kalshi_micro_live_disabled"
    access = (os.getenv("KALSHI_TRADING_ACCESS_KEY_ID") or "").strip()
    path = (os.getenv("KALSHI_TRADING_PRIVATE_KEY_PATH") or "").strip()
    pem = os.getenv("KALSHI_TRADING_PRIVATE_KEY_PEM") or ""
    if not access or not (path or pem):
        return None, "missing_trading_credentials"
    try:
        from cryptography.hazmat.primitives import serialization
    except Exception:  # noqa: BLE001
        return None, "missing_dependency"
    pem_bytes: Optional[bytes] = None
    if path:
        try:
            pem_bytes = Path(path).read_bytes()
        except OSError:
            return None, "missing_trading_credentials"
    elif pem:
        pem_bytes = pem.encode("utf-8")
    if not pem_bytes:
        return None, "missing_trading_credentials"
    password = os.getenv("KALSHI_TRADING_PRIVATE_KEY_PASSWORD") or None
    try:
        key = serialization.load_pem_private_key(
            pem_bytes, password=password.encode("utf-8") if password else None)
    except Exception:  # noqa: BLE001
        return None, "invalid_trading_key"
    env = (os.getenv("KALSHI_MICRO_LIVE_ENV", "demo") or "demo").lower()
    return KalshiTradingSigner(access, key, env), "ready"
