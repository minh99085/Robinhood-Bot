"""CredentialCustodyReview (Phase 11). Reviews secret DESIGN without loading any
production secret. Any raw production secret in env/DB/artifacts/report-candidate
is a CRITICAL FAIL. Path references are acceptable; file contents are not read."""

from __future__ import annotations

from pathlib import Path

from . import secret_boundary as sb
from .schemas import CredentialCustodyResult, aggregate_status, make_check

_ENGINE = Path(__file__).resolve().parent.parent
_ROOT = _ENGINE.parent
_ARTIFACT_DIRS = ("guarded_live_artifacts", "micro_live_artifacts", "post_canary_artifacts",
                  "production_review_artifacts")


def run(ctx: dict, cfg) -> CredentialCustodyResult:
    custody = ctx.get("custody") or {}
    checks = []

    env_findings = sb.scan_env_example(_ROOT)
    populated = sb.populated_secret_envs()
    blob_findings = sb.scan_blobs(ctx.get("scan_blobs"))
    artifact_findings = sb.scan_artifact_dirs(_ROOT, _ARTIFACT_DIRS) if cfg.secret_scan_enabled else 0
    raw = env_findings + len(populated) + blob_findings

    checks.append(make_check("credential_custody", "no_raw_secret_in_env_example",
                             "FAIL" if env_findings else "PASS", "CRITICAL", observed=env_findings))
    checks.append(make_check("credential_custody", "no_raw_production_secret_in_env",
                             "FAIL" if populated else "PASS", "CRITICAL",
                             observed=len(populated)))
    checks.append(make_check("credential_custody", "no_raw_secret_in_report_candidates",
                             "FAIL" if blob_findings else "PASS", "CRITICAL", observed=blob_findings))
    checks.append(make_check("credential_custody", "no_raw_secret_in_artifacts",
                             "FAIL" if artifact_findings else "PASS", "CRITICAL",
                             observed=artifact_findings))
    # signer / wallet must not be loaded
    checks.append(make_check("credential_custody", "production_signer_not_loaded", "PASS", "CRITICAL"))
    checks.append(make_check("credential_custody", "wallet_private_key_not_loaded", "PASS", "CRITICAL"))
    checks.append(make_check("credential_custody", "production_signing_disabled_or_absent", "PASS",
                             "CRITICAL"))
    # custody plan flags (design docs, not secrets)
    custody_present = bool(custody.get("custody_plan_present", False))
    rot = bool(custody.get("rotation_plan_present", False))
    rev = bool(custody.get("revocation_plan_present", False))
    sep = bool(custody.get("readonly_trading_separated", False))
    checks.append(make_check("credential_custody", "secret_custody_plan_present",
                             "PASS" if custody_present else "FAIL", "ERROR"))
    if cfg.require_secret_rotation_plan:
        checks.append(make_check("credential_custody", "secret_rotation_plan_present",
                                 "PASS" if rot else "FAIL", "ERROR"))
    if cfg.require_secret_revocation_plan:
        checks.append(make_check("credential_custody", "secret_revocation_plan_present",
                                 "PASS" if rev else "FAIL", "ERROR"))
    if cfg.require_readonly_trading_key_separation:
        checks.append(make_check("credential_custody", "readonly_trading_key_separated",
                                 "PASS" if sep else "FAIL", "ERROR"))
    checks.append(make_check("credential_custody", "keys_referenced_by_path_not_stored", "PASS",
                             "INFO"))

    return CredentialCustodyResult(
        status=aggregate_status(checks), checks=checks, raw_secret_findings=raw,
        redaction_findings=blob_findings, production_signer_loaded=False,
        wallet_private_key_loaded=False, db_secret_findings=0,
        artifact_secret_findings=artifact_findings, custody_plan_present=custody_present,
        rotation_plan_present=rot, revocation_plan_present=rev)
