"""Execution eligibility is a verifiable certificate, not a JSON flag.

A verdict may only ever become a real order when: it comes from the one
canonical engine (meta_label_v2), carries gauntlet_pass:true plus a
certificate whose report_hash matches the actual gauntlet report on disk,
and that report is ready:true with the verdict's ticker in its validated
universe. Anything else — including every legacy-engine verdict, which is
quarantined outright — is paper-only. Paper logging itself is unaffected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.mc_bridge import (
    LEDGER_FILENAME,
    BridgeState,
    execution_allowed,
    process_once,
    verdict_id,
)


def make_config(tmp_path: Path) -> RobinhoodConfig:
    cfg = RobinhoodConfig.from_env()
    return replace(
        cfg,
        data_dir=str(tmp_path / "data"),
        live_trading_enabled=False,
        max_order_notional_usd=1000.0,
    )


def make_verdict(ticker="AAPL", **overrides) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    v = {
        "timestamp_utc": now.isoformat(),
        "engine": "meta_label_v2",
        "ticker": ticker,
        "verdict": "TRADE",
        "side": "long",
        "horizon_days": 5,
        "s0": 100.0,
        "sizing": {"shares": 3},
    }
    v.update(overrides)
    return v


def write_report(path: Path, *, ready=True, tickers=("AAPL", "NVDA")) -> str:
    """Write a gauntlet report; return its sha256 (the certificate hash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ready": ready, "tickers": list(tickers)}),
                    encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def certified(ticker="AAPL", report_hash="", **overrides) -> dict:
    return make_verdict(ticker=ticker, gauntlet_pass=True,
                        certificate={"report_hash": report_hash}, **overrides)


def _ledger_rows(cfg) -> list[dict]:
    path = Path(cfg.data_dir) / LEDGER_FILENAME
    return [json.loads(l) for l in path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# execution_allowed: the full certificate chain
# ---------------------------------------------------------------------------


def test_verified_certificate_is_eligible(tmp_path):
    report = tmp_path / "gauntlet_report.json"
    h = write_report(report)
    ok, reason = execution_allowed(certified(report_hash=h), report)
    assert ok and "verified" in reason


def test_every_broken_link_in_the_chain_refuses(tmp_path):
    report = tmp_path / "gauntlet_report.json"
    h = write_report(report)

    cases = [
        (make_verdict(engine="legacy_drift"), report, "never executable"),
        (make_verdict(), report, "no gauntlet-pass marker"),
        (make_verdict(gauntlet_pass=True), report, "unverifiable"),
        (certified(report_hash=h), None, "not available"),
        (certified(report_hash="0" * 64), report, "stale or tampered"),
        (certified(ticker="TSLA", report_hash=h), report,
         "not in the validated universe"),
    ]
    for verdict, rpt, expected in cases:
        ok, reason = execution_allowed(verdict, rpt)
        assert not ok, expected
        assert expected in reason

    # ready:false report refuses even with a matching hash
    h2 = write_report(report, ready=False)
    ok, reason = execution_allowed(certified(report_hash=h2), report)
    assert not ok and "not ready" in reason


# ---------------------------------------------------------------------------
# process_once: quarantine + eligibility in the ledger
# ---------------------------------------------------------------------------


def test_legacy_engine_is_quarantined_entirely(tmp_path):
    cfg = make_config(tmp_path)
    vdir = tmp_path / "outputs" / "verdicts"
    vdir.mkdir(parents=True)
    (vdir / "old_AAPL.json").write_text(
        json.dumps(make_verdict(engine="")))          # unstamped legacy
    (vdir / "drift_SPY.json").write_text(
        json.dumps(make_verdict(ticker="SPY", engine="legacy_drift_v1")))

    summary = process_once([vdir], cfg)
    assert summary["skipped"] == 2 and summary["planned"] == 0
    rows = _ledger_rows(cfg)
    assert all("quarantined" in r["outcome"] for r in rows)
    assert all(r["execution_eligible"] is False for r in rows)


def test_v2_verdict_paper_logged_and_certified_eligible(tmp_path):
    cfg = make_config(tmp_path)
    outputs = tmp_path / "outputs"
    vdir = outputs / "verdicts"
    vdir.mkdir(parents=True)
    h = write_report(outputs / "gauntlet_report.json", tickers=["NVDA"])
    (vdir / "a_AAPL.json").write_text(json.dumps(make_verdict()))  # no cert
    (vdir / "b_NVDA.json").write_text(
        json.dumps(certified(ticker="NVDA", report_hash=h)))

    summary = process_once([vdir], cfg)
    assert summary["planned"] == 2      # paper logging unaffected either way

    rows = {r["ticker"]: r for r in _ledger_rows(cfg) if r.get("verdict_id")}
    assert rows["AAPL"]["execution_eligible"] is False
    assert rows["NVDA"]["execution_eligible"] is True
    assert "verified" in rows["NVDA"]["eligibility_reason"]


def test_same_filename_in_both_dirs_is_two_verdicts(tmp_path):
    cfg = make_config(tmp_path)
    outputs = tmp_path / "outputs"
    d1, d2 = outputs / "verdicts", outputs / "paper_verdicts"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    name = "20990101T000000Z_AAPL.json"
    (d1 / name).write_text(json.dumps(make_verdict()))
    (d2 / name).write_text(json.dumps(make_verdict()))

    assert verdict_id(d1 / name) != verdict_id(d2 / name)
    summary = process_once([d1, d2], cfg)
    assert summary["new"] == 2


def test_legacy_bare_filename_state_not_reprocessed(tmp_path):
    cfg = make_config(tmp_path)
    vdir = tmp_path / "outputs" / "verdicts"
    vdir.mkdir(parents=True)
    name = "20990101T000000Z_AAPL.json"
    (vdir / name).write_text(json.dumps(make_verdict()))

    state = BridgeState.load(cfg.data_dir)
    state.mark(name, "paper_planned")   # pre-upgrade bare-filename id
    state.save()

    summary = process_once([vdir], cfg)
    assert summary["new"] == 0
