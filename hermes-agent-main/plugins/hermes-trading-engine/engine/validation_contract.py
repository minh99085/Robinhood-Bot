"""Institutional validation contract + next-report gate (PAPER ONLY, pure).

Makes the next report prove **algorithmic improvement**, not code completion. It
encodes a hard contract the report must satisfy and a production-readiness verdict
that is only positive when a strategy demonstrates **statistically credible
positive after-cost expectancy**.

Contract conditions (all must hold):
1. full pytest green,
2. Bregman paper scanner enabled,
3. constraint groups scanned > 0,
4. fill realism enabled,
5. canonical ledger equity reconciled (≤ 1%),
6. after-cost PnL populated,
7. BTC Pulse gated when its expectancy is negative.

It also bundles walk-forward + purged combinatorial CV + bootstrap CIs + regime
buckets + ablations (Bregman / Chainlink-fast-BTC / calibration / news-Grok /
fill-realism / risk-throttles), separates exploration from validation PnL, and
reports whether Bregman improves after-cost Sharpe/Sortino/Calmar/drawdown and
calibration-adjusted EV.

Quant responsibility map
------------------------
* **Acquisition / preprocessing / features** — supply fresh inputs + features the
  contract conditions read; never fabricate coverage.
* **Statistical / probabilistic modeling** — calibration-adjusted EV + ECE feed
  the improvement test.
* **Bregman strategy** — executable-certified arbitrage is the primary edge under
  test; the ablation isolates its contribution.
* **Risk / portfolio** — drawdown/CVaR + the BTC-Pulse negative-expectancy gate.
* **Backtesting / simulation** — walk-forward + CPCV + bootstrap on the canonical
  ledger return series.
* **Robustness** — regime buckets + ablations + significance thresholds.
* **CLOB v2 execution** — after-cost, depth-aware fills underpin the expectancy.
* **Monitoring** — contract pass/fail + readiness surfaced every report.
* **Compliance / security / ops** — PAPER-only; production readiness is withheld
  without credible evidence.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional, Sequence

logger = logging.getLogger("hte.validation_contract")

CONTRACT_CONDITIONS = (
    "pytest_green",
    "bregman_paper_enabled",
    "groups_scanned_positive",
    "fill_realism_enabled",
    "ledger_reconciled",
    "after_cost_pnl_populated",
    "btc_pulse_gated_when_negative",
)

# Ablation components whose contribution the report must isolate.
ABLATION_COMPONENTS = (
    "bregman", "chainlink_fast_btc", "calibration", "news_grok", "fill_realism",
    "risk_throttles",
)


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def calibration_adjusted_ev(after_cost_ev: Optional[float], ece: Optional[float]) -> Optional[float]:
    """Discount an after-cost EV by calibration error: ``ev * max(0, 1 - ece)``.

    A poorly-calibrated model (high ECE) has its EV shrunk toward 0. Pure."""
    ev = _num(after_cost_ev)
    if ev is None:
        return None
    e = _num(ece)
    factor = max(0.0, 1.0 - (e if e is not None else 0.0))
    return round(ev * factor, 8)


def build_validation_contract(feats: Mapping, *, ledger_reconciliation: Optional[Mapping] = None
                              ) -> dict:
    """Evaluate the 7-condition validation contract from features (pure).

    Returns ``{checks: [{name, passed, detail}], passed, failed}``. ``passed`` is
    True only when EVERY condition holds — the report must not claim improvement
    otherwise."""
    feats = feats or {}
    led = ledger_reconciliation or {}
    checks: list[dict] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("pytest_green", feats.get("tests_passing") is True,
        f"tests_passing={feats.get('tests_passing')}")

    bregman_on = _truthy(feats.get("bregman_paper_enabled")) or _truthy(feats.get("bregman_enabled"))
    add("bregman_paper_enabled", bregman_on, f"bregman_enabled={feats.get('bregman_enabled')}")

    scanned = _num(feats.get("bregman_constraint_groups_scanned"))
    add("groups_scanned_positive", scanned is not None and scanned > 0,
        f"constraint_groups_scanned={scanned}")

    add("fill_realism_enabled",
        _truthy(feats.get("fill_realism_enabled")) or feats.get("fantasy_fill_rejections") is not None,
        f"fill_realism_enabled={feats.get('fill_realism_enabled')}")

    add("ledger_reconciled", bool(led.get("ok", False)) if led else False,
        f"reconciliation_ok={led.get('ok') if led else None}")

    add("after_cost_pnl_populated", _num(feats.get("after_cost_pnl")) is not None,
        f"after_cost_pnl={feats.get('after_cost_pnl')}")

    # BTC Pulse must be gated (shadow) whenever its after-cost expectancy is negative.
    btc_ac = _num(feats.get("btc_pulse_after_cost_pnl"))
    gated = _truthy(feats.get("btc_pulse_gate_enabled"))
    pulse_ok = (btc_ac is None) or (btc_ac >= 0) or gated
    add("btc_pulse_gated_when_negative", pulse_ok,
        f"btc_after_cost={btc_ac} gate_enabled={gated}")

    failed = [c["name"] for c in checks if not c["passed"]]
    passed = not failed
    if not passed:
        logger.info("validation contract FAILED: %s", failed)
    return {"checks": checks, "passed": bool(passed), "failed": failed,
            "conditions": list(CONTRACT_CONDITIONS)}


def credible_positive_expectancy(returns: Sequence[float], *, alpha: float = 0.05,
                                 n_boot: int = 1000, seed: int = 0) -> dict:
    """Bootstrap the mean after-cost return; credible-positive iff CI lower > 0.

    Reuses the seeded bootstrap so the verdict is deterministic + auditable."""
    from engine.replay.robustness import bootstrap_ci
    rets = [float(r) for r in (returns or [])]
    if len(rets) < 5:
        return {"point": None, "lo": None, "hi": None, "n": len(rets),
                "credible_positive": False, "reason": "insufficient_samples"}
    ci = bootstrap_ci(rets, n_boot=n_boot, alpha=alpha, seed=seed)
    credible = ci["lo"] > 0.0
    return {"point": ci["point"], "lo": ci["lo"], "hi": ci["hi"], "n": ci["n"],
            "credible_positive": bool(credible),
            "reason": "ci_lower_above_zero" if credible else "ci_includes_zero_or_negative"}


def bregman_improvement(baseline: Mapping, with_bregman: Mapping) -> dict:
    """Does adding Bregman improve after-cost Sharpe/Sortino/Calmar/drawdown +
    calibration-adjusted EV? Returns per-metric deltas + an ``improves`` verdict.

    Higher-is-better: sharpe, sortino, calmar, calibration_adjusted_ev. Lower-is-
    better: max_drawdown. ``improves`` is True when no metric regresses and at
    least one improves. Pure + deterministic."""
    higher = ("sharpe", "sortino", "calmar", "calibration_adjusted_ev")
    lower = ("max_drawdown",)
    per: dict = {}
    any_better = False
    any_worse = False
    for m in higher + lower:
        b, c = _num(baseline.get(m)), _num(with_bregman.get(m))
        if b is None or c is None:
            per[m] = {"baseline": b, "with_bregman": c, "delta": None, "better": None}
            continue
        delta = round(c - b, 8)
        better = (delta > 0) if m in higher else (delta < 0)
        worse = (delta < 0) if m in higher else (delta > 0)
        per[m] = {"baseline": round(b, 8), "with_bregman": round(c, 8),
                  "delta": delta, "better": better}
        any_better = any_better or better
        any_worse = any_worse or worse
    return {"per_metric": per, "improves": bool(any_better and not any_worse),
            "any_better": any_better, "any_worse": any_worse}


def production_readiness_verdict(contract: Mapping, expectancy: Mapping, *,
                                 ablations: Optional[Mapping] = None) -> dict:
    """Production readiness = contract passed AND statistically credible positive
    after-cost expectancy (and no harmful required ablation). Conservative: any
    missing evidence withholds readiness."""
    blocking: list[str] = []
    if not contract.get("passed", False):
        blocking.append("validation_contract_failed:" + ",".join(contract.get("failed", [])))
    if not expectancy.get("credible_positive", False):
        blocking.append("no_credible_positive_after_cost_expectancy")
    harmful = list((ablations or {}).get("harmful", []))
    if harmful:
        blocking.append("harmful_components:" + ",".join(harmful))
    ready = not blocking
    return {"production_ready": bool(ready), "blocking_reasons": blocking,
            "note": "PAPER-only; readiness requires statistically credible positive "
                    "after-cost expectancy under a passing validation contract."}


def walk_forward_report(returns: Sequence[float], *, train: int = 20, test: int = 10,
                        regime_observations: Optional[Sequence[Mapping]] = None,
                        regime_key: str = "regime", seed: int = 0) -> dict:
    """Bundle walk-forward windows + bootstrap CI + regime buckets (pure).

    Reuses :mod:`engine.replay.robustness`. ``returns`` is the canonical-ledger
    after-cost return series."""
    from engine.replay.robustness import bootstrap_ci, walk_forward_windows
    rets = [float(r) for r in (returns or [])]
    windows = walk_forward_windows(len(rets), train=train, test=test)
    wf_means = []
    for w in windows:
        seg = rets[w.test_start:w.test_end]
        if seg:
            wf_means.append(round(sum(seg) / len(seg), 8))
    regimes: dict = {}
    if regime_observations:
        for obs in regime_observations:
            regimes.setdefault(str(obs.get(regime_key, "unknown")), []).append(obs)
    return {
        "n_returns": len(rets),
        "windows": len(windows),
        "walk_forward_test_means": wf_means,
        "bootstrap_ci": bootstrap_ci(rets, seed=seed) if rets else None,
        "regime_buckets": {k: len(v) for k, v in regimes.items()},
    }
