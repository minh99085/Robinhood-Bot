# Rollback Plan (TEMPLATE — human review required)

> Phase 11 is design review only. Production execution remains UNIMPLEMENTED.

## Target safe states (most to least restrictive)
1. Disabled bot (no processes running)
2. Read-only market data only
3. Paper-only
4. Demo-only (Phase 9 micro-live demo, disabled by default)
5. Shadow-only

## Config rollback
- [ ] Set `MICRO_LIVE_ENABLED=0`, `MICRO_LIVE_BUILD_ENABLED` stays a False code constant.
- [ ] Set `SHADOW_ENABLED` / venue flags to safe values.
- [ ] Confirm `PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION` is unset (it is ignored anyway).

## Docker rollback
- [ ] `docker compose down`
- [ ] `docker compose up --build` with the previous known-good image tag.

## Key revocation (placeholder — manual, outside the bot)
- [ ] Revoke/rotate trading keys in the secret manager.
- [ ] Confirm no production key is referenced by the running config.

## Data backup / capture
- [ ] Export SQLite DB and all `*_artifacts/` directories.

## Kill switch verification
- [ ] Confirm kill switch files are honored (Phase 9/10 locks block submission).

## Sign-off
- Operator: __________  Date: __________
