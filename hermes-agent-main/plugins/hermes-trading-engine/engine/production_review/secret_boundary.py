"""Secret-boundary scanning helpers (Phase 11). Detects raw secrets in candidate
report/artifact/env content. Path REFERENCES are acceptable; raw key material is
not. Never logs or persists the detected secret value."""

from __future__ import annotations

import os
import re
from pathlib import Path

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_HEX_KEY_RE = re.compile(r"0x[0-9a-fA-F]{40,}")
_SEED_RE = re.compile(r"\b(?:[a-z]+\s+){11,}[a-z]+\b")  # 12+ lowercase words (seed phrase-ish)
_BEARER_RE = re.compile(r"bearer\s+[A-Za-z0-9_\-\.=]{20,}", re.I)
_API_SECRET_RE = re.compile(r"(api[_-]?secret|client[_-]?secret)\s*[=:]\s*\S{8,}", re.I)
_POPULATED_KEY_ENV = (
    "POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE",
    "KALSHI_TRADING_PRIVATE_KEY_PEM", "KALSHI_TRADING_PRIVATE_KEY_PASSWORD",
    "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_PASSWORD",
)


def scan_text(text: str) -> int:
    """Return count of raw secret-like findings in text."""
    if not text:
        return 0
    n = 0
    for rx in (_PEM_RE, _HEX_KEY_RE, _BEARER_RE, _API_SECRET_RE):
        n += len(rx.findall(text))
    return n


def scan_blobs(blobs) -> int:
    return sum(scan_text(str(b)) for b in (blobs or []))


def populated_secret_envs() -> list[str]:
    """Env vars that hold RAW production key material (should be empty in Phase 11)."""
    return [e for e in _POPULATED_KEY_ENV if (os.getenv(e) or "").strip()]


def scan_env_example(root: Path) -> int:
    """Raw private-key material committed to .env.example would be a hard fail."""
    p = root / ".env.example"
    try:
        txt = p.read_text()
    except OSError:
        return 0
    # only flag actual key BLOCKS / hex keys, not empty KEY= lines
    return len(_PEM_RE.findall(txt)) + len(_HEX_KEY_RE.findall(txt))


def scan_artifact_dirs(root: Path, dirs) -> int:
    n = 0
    for d in dirs:
        base = root / d
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f.suffix in (".json", ".md", ".csv", ".txt"):
                try:
                    n += scan_text(f.read_text(errors="ignore"))
                except OSError:
                    continue
    return n
