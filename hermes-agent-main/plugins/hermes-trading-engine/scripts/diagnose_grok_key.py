#!/usr/bin/env python3
"""Diagnose why the xAI/Grok key is (not) working — end to end (PAPER ONLY, read-only).

Run inside the container that serves the dashboard:

    docker compose exec hermes-trading-engine python scripts/diagnose_grok_key.py

It reports, for the ACTUAL process environment:
  * whether the key is present, its length, and whether it has stray quotes/whitespace
    (a quoted/whitespace key is the classic "suddenly stopped working" 401 cause),
  * RESEARCH_MODE / GROK_BRAIN_ONLINE,
  * the live xAI ``/v1/models`` HTTP status (401/403 = expired/revoked/invalid key,
    429 = rate-limited / out of credits, 200 = key WORKS so it's a wiring/toggle issue).

Read-only: a single GET to the models endpoint. Never places, sizes, or cancels an
order; the key is never printed (only its length + first/last 2 chars).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    # CANONICAL variable is XAI_API_KEY; GROK_API_KEY is only an optional legacy
    # fallback (never required). The key VALUE is never printed.
    raw = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or ""
    source = ("XAI_API_KEY" if (os.getenv("XAI_API_KEY") or "").strip()
              else ("GROK_API_KEY(legacy)" if (os.getenv("GROK_API_KEY") or "").strip()
                    else None))
    stripped = raw.strip()
    dequoted = stripped
    if len(dequoted) >= 2 and dequoted[0] == dequoted[-1] and dequoted[0] in ("'", '"'):
        dequoted = dequoted[1:-1].strip()

    print("=== xAI/Grok key diagnosis (process environment) ===")
    print(f"xai_api_key_present : {bool(dequoted)}")
    print(f"xai_api_key_source  : {source or 'XAI_API_KEY'}")
    print(f"key_present         : {bool(dequoted)}")
    print(f"raw_len             : {len(raw)}")
    print(f"stripped_len        : {len(stripped)}")
    print(f"dequoted_len        : {len(dequoted)}")
    print(f"had_surrounding_ws  : {raw != stripped}")
    print(f"had_surrounding_quote: {stripped != dequoted}")
    if dequoted:
        print(f"first2 / last2      : {dequoted[:2]!r} / {dequoted[-2:]!r}")
        print(f"looks_like_xai_key  : {dequoted.startswith('xai-')}")
    print(f"RESEARCH_MODE       : {os.getenv('RESEARCH_MODE')}")
    print(f"GROK_BRAIN_ONLINE   : {os.getenv('GROK_BRAIN_ONLINE')}")

    if not dequoted:
        print("\nVERDICT: NO KEY in this process env -> wiring problem (key not reaching "
              "the container). Put XAI_API_KEY=xai-... in .env and "
              "`docker compose up -d --force-recreate`.")
        return 2
    if raw != stripped or stripped != dequoted:
        print("\nNOTE: the key had surrounding whitespace/quotes — the engine now strips "
              "these, but check your .env line is exactly  XAI_API_KEY=xai-...  (no quotes).")

    base = (os.getenv("GROK_BASE_URL") or os.getenv("HTE_GROK_BASE_URL")
            or "https://api.x.ai/v1").rstrip("/")
    print(f"\nCalling {base}/models (read-only) ...")
    try:
        import httpx
        r = httpx.get(f"{base}/models",
                      headers={"Authorization": f"Bearer {dequoted}"}, timeout=20.0)
        code = r.status_code
        print(f"xAI /models HTTP {code}")
        if code == 200:
            try:
                n = len((r.json() or {}).get("data", []))
            except Exception:  # noqa: BLE001
                n = "?"
            print(f"VERDICT: KEY WORKS ({n} models visible). If Grok still shows OFF, it is "
                  "a wiring/toggle issue, not the key — POST /api/grok/on or recreate "
                  "the container so PID 1 has the key.")
            return 0
        if code in (401, 403):
            print(f"body: {r.text[:200]}")
            print("VERDICT: xAI REJECTED the key (expired / revoked / wrong / disabled). "
                  "Generate a fresh key at console.x.ai and update .env.")
            return 1
        if code == 429:
            print(f"body: {r.text[:200]}")
            print("VERDICT: RATE-LIMITED or OUT OF CREDITS — the key is valid but throttled. "
                  "Check your xAI usage/billing.")
            return 1
        print(f"body: {r.text[:200]}")
        print(f"VERDICT: unexpected HTTP {code} — see body above.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"xAI call FAILED: {type(exc).__name__}: {exc}")
        print("VERDICT: the key may be fine but the container could not reach api.x.ai "
              "(network/egress blocked, DNS, or proxy). Check container network access.")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
