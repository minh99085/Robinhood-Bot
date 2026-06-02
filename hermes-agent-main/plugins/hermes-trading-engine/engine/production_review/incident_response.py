"""IncidentResponsePlan (Phase 11). Generates a structured incident-response
template. Does NOT execute any incident action."""

from __future__ import annotations


def content() -> str:
    return """# Incident Response Plan (TEMPLATE — human review required)

> Phase 11 is design review only. This plan does not execute any action.
> Production execution remains UNIMPLEMENTED.

## Severity levels
- SEV1: funds at risk / unknown live order / secret leak
- SEV2: reconciliation mismatch / venue outage during an open canary
- SEV3: degraded market data / elevated warnings

## Who stops the system
- [ ] Primary operator: __________
- [ ] Backup operator: __________

## Immediate actions
1. Set kill switches: create `KILL_SWITCH`, `GUARDED_LIVE_KILL_SWITCH`, `MICRO_LIVE_KILL_SWITCH`.
2. Stop the engine / dashboard process.
3. Verify NO new orders can be submitted (Phase 9 CLI-only; production unimplemented).
4. Check the venue UI/app manually for any open orders or fills.
5. If a micro-live demo order may be open: run the Phase 9 emergency-cancel CLI (demo only).

## Credential revocation
- [ ] Revoke/rotate read-only keys via secret manager (path reference only).
- [ ] Revoke/rotate trading keys via secret manager (production keys are NOT loaded by the bot).

## Data capture
- [ ] Export DB snapshot and artifacts.
- [ ] Capture audit_events, post-canary analyses, and reconciliation records.

## Communication
- Notify: __________  Channel: __________

## Postmortem
- Use `postmortem_template.md`.

## No-new-orders policy
After any incident, NO new canary may run until post-canary analysis + manual review clear it.
"""
