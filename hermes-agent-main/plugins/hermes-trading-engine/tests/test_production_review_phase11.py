"""Phase 11 — production-canary DESIGN REVIEW tests.

All inputs are mocked dossiers/fixtures. No production network, no production
orders, no production cancels, no production signing, no funds moved, no
credentials. Verifies the review is design-only and can never authorize
production execution, size increase, or autonomous live trading.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.production_review import (FORBIDDEN_PRODUCTION_RECOMMENDATIONS,  # noqa: E402
                                      ProductionReviewConfig, ProductionReviewRequest, run_review)
from engine.storage import Store  # noqa: E402

_FIXTURE = _ROOT / "tests" / "fixtures" / "sample_production_review_ready_dossier.json"


def ready_ctx() -> dict:
    return copy.deepcopy(json.loads(_FIXTURE.read_text()))


def review(ctx, *, store=None, write_report=False):
    return run_review(store, ProductionReviewConfig.from_env(), fixture=ctx,
                      request=ProductionReviewRequest(), write_report=write_report)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "pr.db")


# --- 1-4 invariants -------------------------------------------------------- #
def test_production_review_disabled_execution_by_default():
    res = review(ready_ctx())
    assert res.eligible_for_production_execution is False


def test_production_review_never_returns_execution_approval():
    for mut in (lambda c: c, lambda c: c.update({"evidence": {}}) or c):
        res = review(mut(ready_ctx()))
        assert res.recommendation not in FORBIDDEN_PRODUCTION_RECOMMENDATIONS
        assert "EXECUTION" not in res.recommendation and "ENABLE" not in res.recommendation


def test_production_review_never_returns_size_increase():
    res = review(ready_ctx())
    assert res.eligible_for_size_increase is False


def test_production_review_never_returns_autonomous_live():
    res = review(ready_ctx())
    assert res.eligible_for_autonomous_live is False


# --- 5-13 evidence gating -------------------------------------------------- #
def test_production_review_missing_post_canary_blocks():
    c = ready_ctx()
    c["evidence"]["post_canary_eligibility_status"] = "not_eligible"
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_production_review_requires_clean_demo_canaries():
    c = ready_ctx()
    c["evidence"]["clean_demo_canary_count"] = 1
    res = review(c)
    assert res.recommendation == "FIX_AND_REPEAT_DEMO_CANARIES"


def test_production_review_blocks_unresolved_canaries():
    c = ready_ctx()
    c["evidence"]["unresolved_canary_count"] = 2
    res = review(c)
    assert res.recommendation in ("FIX_AND_REPEAT_DEMO_CANARIES", "NOT_READY")
    assert res.recommendation not in FORBIDDEN_PRODUCTION_RECOMMENDATIONS


def test_production_review_blocks_failed_canaries():
    c = ready_ctx()
    c["evidence"]["failed_canary_count"] = 1
    res = review(c)
    assert res.recommendation == "FIX_AND_REPEAT_DEMO_CANARIES"


def test_production_review_requires_renewed_shadow():
    c = ready_ctx()
    c["evidence"]["renewed_shadow_hours"] = None
    res = review(c)
    assert res.recommendation == "FIX_AND_REPEAT_SHADOW"


def test_production_review_blocks_stale_evidence():
    c = ready_ctx()
    c["evidence"]["stale_evidence"] = ["shadow_report"]
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_production_review_requires_phase8_conformance():
    c = ready_ctx()
    c["evidence"]["guarded_live_conformance_status"] = "FAIL"
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_production_review_requires_phase9_conformance():
    c = ready_ctx()
    c["evidence"]["micro_live_conformance_status"] = "FAIL"
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_production_review_requires_phase10_eligibility():
    c = ready_ctx()
    c["evidence"]["post_canary_eligibility_status"] = "not_eligible"
    res = review(c)
    assert res.recommendation == "NOT_READY"


# --- 14-19 attestations ---------------------------------------------------- #
def test_jurisdiction_attestation_required():
    c = ready_ctx()
    c["attestations"] = [a for a in c["attestations"] if "jurisdiction" not in a["confirmation_text"].lower()]
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_jurisdiction_attestation_cannot_be_bot():
    c = ready_ctx()
    for a in c["attestations"]:
        a["reviewer_id"] = "grok"
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_jurisdiction_attestation_expires():
    c = ready_ctx()
    for a in c["attestations"]:
        a["expires_ts_ms"] = 1  # long expired
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_account_readiness_attestation_required():
    c = ready_ctx()
    c["attestations"] = [a for a in c["attestations"] if "no funds were moved" not in a["confirmation_text"].lower()]
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_account_readiness_does_not_move_funds():
    res = review(ready_ctx())
    assert res.account_readiness.no_funds_moved is True


def test_venue_terms_attestation_required():
    c = ready_ctx()
    c["attestations"] = [a for a in c["attestations"] if "venue terms" not in a["confirmation_text"].lower()]
    res = review(c)
    assert res.recommendation in ("NOT_READY", "FIX_AND_REPEAT_DEMO_CANARIES")
    assert res.recommendation not in FORBIDDEN_PRODUCTION_RECOMMENDATIONS


# --- 20-26 endpoint separation --------------------------------------------- #
def test_endpoint_separation_no_api_submit_routes():
    res = review(ready_ctx())
    assert res.endpoint_separation.api_submit_routes_found == 0


def test_endpoint_separation_no_api_cancel_routes():
    src = (_ROOT / "engine" / "app.py").read_text()
    for tok in ("/api/production-review/cancel", "/api/production-review/submit"):
        for line in src.splitlines():
            if tok in line and ".post(" in line.lower():
                raise AssertionError(f"forbidden route {tok}")


def test_endpoint_separation_no_dashboard_submit_controls():
    res = review(ready_ctx())
    assert res.endpoint_separation.dashboard_submit_controls_found == 0


def test_endpoint_separation_no_strategy_production_path():
    res = review(ready_ctx())
    assert res.endpoint_separation.strategy_production_paths_found == 0


def test_endpoint_separation_no_grok_production_path():
    res = review(ready_ctx())
    assert res.endpoint_separation.grok_production_paths_found == 0


def test_endpoint_separation_blocks_production_order_endpoint():
    res = review(ready_ctx())
    assert res.endpoint_separation.production_order_endpoint_reachable is False


def test_endpoint_separation_blocks_deposit_withdraw_transfer():
    from engine.micro_live.network_guard import NetworkGuard
    from engine.micro_live.errors import ForbiddenEndpointError
    g = NetworkGuard(allow_production=False)
    for url in ("https://x/deposit", "https://x/withdraw", "https://x/transfer", "https://x/bridge",
                "https://x/allowance"):
        with pytest.raises(ForbiddenEndpointError):
            g.record("POST", url)


# --- 27-31 credential custody ---------------------------------------------- #
def test_credential_custody_detects_raw_private_key():
    c = ready_ctx()
    c["scan_blobs"] = ["-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"]
    res = review(c)
    assert res.credential_custody.raw_secret_findings >= 1
    assert res.recommendation == "NOT_READY"


def test_credential_custody_detects_wallet_private_key():
    c = ready_ctx()
    c["scan_blobs"] = ["wallet=0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef0011"]
    res = review(c)
    assert res.credential_custody.raw_secret_findings >= 1


def test_credential_custody_allows_secret_path_reference():
    c = ready_ctx()
    c["scan_blobs"] = ["KALSHI_TRADING_PRIVATE_KEY_PATH=/secrets/kalshi/key.pem"]
    res = review(c)
    assert res.credential_custody.raw_secret_findings == 0


def test_credential_custody_requires_rotation_plan():
    c = ready_ctx()
    c["custody"]["rotation_plan_present"] = False
    res = review(c)
    assert res.credential_custody.status in ("FAIL", "WARN")
    assert res.recommendation != "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"


def test_credential_custody_requires_revocation_plan():
    c = ready_ctx()
    c["custody"]["revocation_plan_present"] = False
    res = review(c)
    assert res.recommendation != "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"


# --- 32-37 mock-only conformance ------------------------------------------- #
def _conf(traps=None):
    from engine.production_review import production_conformance as pc
    return pc.run(ProductionReviewConfig.from_env(), traps=traps)


def test_production_conformance_mock_only():
    r = _conf()
    assert r.mock_only is True and r.real_network_calls == 0 and r.status == "PASS"


def test_production_conformance_fails_on_real_network_call():
    assert _conf({"real_network_call": True}).status == "FAIL"


def test_production_conformance_fails_on_production_order_call():
    assert _conf({"production_order_call": True}).status == "FAIL"


def test_production_conformance_fails_on_production_signer_call():
    assert _conf({"production_signer_call": True}).status == "FAIL"


def test_production_conformance_fails_if_dashboard_button_exists():
    assert _conf({"dashboard_button": True}).status == "FAIL"


def test_production_conformance_fails_if_api_submit_route_exists():
    assert _conf({"api_submit_route": True}).status == "FAIL"


# --- 38-40 operational readiness ------------------------------------------- #
def test_operational_readiness_requires_incident_response(monkeypatch, tmp_path):
    from engine.production_review import operational_readiness as opr
    monkeypatch.setattr(opr, "ensure_runbooks", lambda root=None: tmp_path)  # empty dir
    res = opr.run({}, ProductionReviewConfig.from_env())
    assert res.incident_response_present is False and res.status in ("FAIL", "WARN")


def test_operational_readiness_requires_rollback_plan(monkeypatch, tmp_path):
    from engine.production_review import operational_readiness as opr
    monkeypatch.setattr(opr, "ensure_runbooks", lambda root=None: tmp_path)
    res = opr.run({}, ProductionReviewConfig.from_env())
    assert res.rollback_plan_present is False


def test_operational_readiness_requires_manual_exchange_checklist(monkeypatch, tmp_path):
    from engine.production_review import operational_readiness as opr
    monkeypatch.setattr(opr, "ensure_runbooks", lambda root=None: tmp_path)
    res = opr.run({}, ProductionReviewConfig.from_env())
    assert res.manual_exchange_ui_checklist_present is False


# --- 41-44 change control + human checklist -------------------------------- #
def test_change_control_required():
    c = ready_ctx()
    c["change_control"] = None
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_change_control_no_execution_statement_required():
    c = ready_ctx()
    c["change_control"]["no_execution_statement"] = ""
    res = review(c)
    assert res.recommendation == "NOT_READY"


def test_human_checklist_required():
    c = ready_ctx()
    c["human_checklist"] = None
    res = review(c)
    assert res.recommendation in ("READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW", "NOT_READY")
    assert res.recommendation != "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"


def test_human_checklist_cannot_be_bot():
    c = ready_ctx()
    c["human_checklist"]["reviewer_id"] = "bot"
    res = review(c)
    assert res.recommendation != "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"


# --- 45-46 recommendation bounds ------------------------------------------- #
def test_review_can_recommend_phase12_draft_only():
    res = review(ready_ctx())
    assert res.recommendation == "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN"
    assert res.eligible_to_draft_phase12_plan is True


def test_review_cannot_recommend_phase12_execution():
    res = review(ready_ctx())
    assert res.eligible_for_production_execution is False
    assert res.recommendation not in FORBIDDEN_PRODUCTION_RECOMMENDATIONS


# --- 47-51 report ---------------------------------------------------------- #
def test_review_report_artifacts_created(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_OUTPUT_DIR", str(tmp_path / "art"))
    res = review(ready_ctx(), store=store, write_report=True)
    rep = [r for r in store.get_production_review_reports(10) if r["review_id"] == res.review_id][0]
    d = Path(rep["report_path"]).parent
    for f in ("production_review_summary.json", "production_review_report.md",
              "production_review_checks.csv", "evidence_summary.json", "endpoint_separation.json",
              "credential_custody.json", "production_conformance.json", "operational_readiness.json",
              "account_readiness.json", "venue_permissions.json",
              "jurisdiction_attestations_redacted.json", "change_control.json",
              "human_checklist.json", "phase12_scope_template.md", "production_dossier_index.md"):
        assert (d / f).exists(), f


def _report_md(store, res):
    rep = [r for r in store.get_production_review_reports(10) if r["review_id"] == res.review_id][0]
    return Path(rep["report_path"]).read_text()


def test_review_report_says_no_production_execution(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_OUTPUT_DIR", str(tmp_path / "art"))
    res = review(ready_ctx(), store=store, write_report=True)
    assert "Production execution is not implemented in Phase 11" in _report_md(store, res)


def test_review_report_says_no_size_increase(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_OUTPUT_DIR", str(tmp_path / "art"))
    res = review(ready_ctx(), store=store, write_report=True)
    assert "No size increase is approved" in _report_md(store, res)


def test_review_report_says_no_autonomous_live(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_OUTPUT_DIR", str(tmp_path / "art"))
    res = review(ready_ctx(), store=store, write_report=True)
    assert "No autonomous live trading is approved" in _report_md(store, res)


def test_review_report_redacts_secrets(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_OUTPUT_DIR", str(tmp_path / "art"))
    c = ready_ctx()
    c["human_checklist"]["confirmation_text"] = "ack 0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef0099"
    res = review(c, store=store, write_report=True)
    blob = ""
    rep = [r for r in store.get_production_review_reports(10) if r["review_id"] == res.review_id][0]
    for f in Path(rep["report_path"]).parent.glob("*"):
        blob += f.read_text()
    assert "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef0099" not in blob


# --- 52-54 api ------------------------------------------------------------- #
def test_production_review_api_has_no_submit_cancel_scale_routes():
    src = (_ROOT / "engine" / "app.py").read_text()
    for tok in ("/api/production-review/submit", "/api/production-review/cancel",
                "/api/production-review/enable-production", "/api/production-review/increase-size",
                "/api/production-review/scale", "/api/production-review/arm-production",
                "/api/production-review/live-order"):
        assert tok not in src, f"forbidden route {tok}"
    assert "/api/production-review/run" in src and "/api/production-review/status" in src


def test_production_review_api_redacts_secrets():
    res = review(ready_ctx())
    blob = json.dumps(res.record(), default=str)
    assert "PRIVATE KEY" not in blob and "0xdeadbeef" not in blob


def test_production_review_run_endpoint_no_order_calls():
    # the analysis/report/loader code must never call a real order/cancel/sign path
    # (production_conformance defines a DISABLED stub that only raises — excluded)
    for mod in ("dossier", "evidence_loader", "report"):
        src = (_ROOT / "engine" / "production_review" / f"{mod}.py").read_text().lower()
        for bad in (".submit_fok_canary_order(", "create_order(", "post_order(", "cancel_order("):
            assert bad not in src, f"{mod} references {bad}"


# --- 55-57 CLI ------------------------------------------------------------- #
def _run_cli(name, argv):
    scripts_dir = str(_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(f"_pr_{name}",
                                                  _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(argv)


def test_production_review_cli_help():
    for name in ("production_review_run", "production_review_conformance", "production_review_attest",
                 "production_review_checklist", "production_review_report",
                 "production_review_export_dossier", "production_review_veto"):
        with pytest.raises(SystemExit) as e:
            _run_cli(name, ["--help"])
        assert e.value.code == 0


def test_production_review_veto_exit_codes(store):
    db = str(store.db_path)
    store.add_production_review_run({
        "review_id": "rv-notready", "ts_ms": 1, "status": "FAIL", "recommendation": "NOT_READY",
        "generated_by": "t", "hard_fail_count": 1, "warning_count": 0, "blocked_count": 0,
        "eligible_to_draft_phase12_plan": 0, "blocking_reasons_json": "[]",
        "next_required_actions_json": "[]", "summary_json": "{}"})
    assert _run_cli("production_review_veto", ["--review-id", "rv-notready", "--db", db]) != 0
    store.add_production_review_run({
        "review_id": "rv-approved", "ts_ms": 2, "status": "PASS_DESIGN_REVIEW_ONLY",
        "recommendation": "APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN", "generated_by": "t",
        "hard_fail_count": 0, "warning_count": 0, "blocked_count": 0,
        "eligible_to_draft_phase12_plan": 1, "blocking_reasons_json": "[]",
        "next_required_actions_json": "[]", "summary_json": "{}"})
    assert _run_cli("production_review_veto", [
        "--review-id", "rv-approved", "--db", db,
        "--fail-on-not-approved-to-draft-phase12"]) == 0


def test_export_dossier_creates_index(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_OUTPUT_DIR", str(tmp_path / "art"))
    res = review(ready_ctx(), store=store, write_report=True)
    out = tmp_path / "dossier"
    rc = _run_cli("production_review_export_dossier",
                  ["--review-id", res.review_id, "--db", str(store.db_path), "--out", str(out)])
    assert rc == 0 and (out / "production_dossier_index.md").exists()


# --- 58-60 runbooks / env / keys ------------------------------------------- #
def test_runbooks_templates_created_without_secrets(tmp_path):
    from engine.production_review.operational_readiness import ensure_runbooks
    from engine.production_review import secret_boundary as sb
    base = ensure_runbooks(tmp_path)
    files = list(base.glob("*.md"))
    assert files
    for f in files:
        assert sb.scan_text(f.read_text()) == 0


def test_env_attempt_to_enable_production_fails_review(monkeypatch):
    monkeypatch.setenv("PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION", "1")
    res = review(ready_ctx())
    assert res.recommendation == "NOT_READY"
    assert res.eligible_for_production_execution is False


def test_no_production_private_key_loaded_in_tests():
    res = review(ready_ctx())
    assert res.credential_custody.production_signer_loaded is False
    assert res.credential_custody.wallet_private_key_loaded is False


# --- 61-63 regression / import --------------------------------------------- #
def test_existing_post_canary_tests_still_pass():
    from engine.post_canary import PostCanaryConfig, compute_eligibility
    e = compute_eligibility(None, PostCanaryConfig(), "kalshi", "demo")
    assert e.eligible_size_increase is False


def test_existing_micro_live_tests_still_pass():
    from engine.micro_live import MicroLiveConfig, all_pass, check_locks
    assert not all_pass(check_locks(MicroLiveConfig()))


def test_compile_and_import_production_review_modules():
    import engine.production_review.account_readiness  # noqa: F401
    import engine.production_review.artifacts  # noqa: F401
    import engine.production_review.audit  # noqa: F401
    import engine.production_review.change_control  # noqa: F401
    import engine.production_review.config  # noqa: F401
    import engine.production_review.credential_custody  # noqa: F401
    import engine.production_review.dossier  # noqa: F401
    import engine.production_review.endpoint_separation  # noqa: F401
    import engine.production_review.evidence_loader  # noqa: F401
    import engine.production_review.human_checklist  # noqa: F401
    import engine.production_review.incident_response  # noqa: F401
    import engine.production_review.jurisdiction  # noqa: F401
    import engine.production_review.operational_readiness  # noqa: F401
    import engine.production_review.production_conformance  # noqa: F401
    import engine.production_review.report  # noqa: F401
    import engine.production_review.rollback_plan  # noqa: F401
    import engine.production_review.schemas  # noqa: F401
    import engine.production_review.secret_boundary  # noqa: F401
    import engine.production_review.venue_permissions  # noqa: F401
    import engine.production_review.veto  # noqa: F401
    assert True
