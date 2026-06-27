"""Tier-1 baseline quant cohort gate."""

from engine.pulse.engine import PulseEngine, PulseConfig


class _FakeEsnap:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _eng(**kw):
    defaults = {
        "baseline_cohort_gate_enabled": True,
        "baseline_up_tv_gate_enabled": True,
        "baseline_cohort_ttc_min_s": 180.0,
        "baseline_cohort_ttc_max_s": 240.0,
    }
    defaults.update(kw)
    return PulseEngine(PulseConfig(**defaults))


def test_15m_fast_lane_ttc_band_180_240_scaled():
    eng = _eng(baseline_cohort_15m_fast_lane=True,
               baseline_cohort_15m_ttc_min_s=180.0, baseline_cohort_15m_ttc_max_s=240.0)
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=650.0, tv_feature=None, window_seconds=900)
    assert ok and r == ""
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=420.0, tv_feature=None, window_seconds=900)
    assert not ok and r == "baseline_cohort_ttc_too_early"
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=800.0, tv_feature=None, window_seconds=900)
    assert not ok and r == "baseline_cohort_ttc_too_late"


def test_15m_fast_lane_blocks_medium_edge_when_high_required():
    eng = _eng(baseline_cohort_15m_fast_lane=True,
               baseline_cohort_require_high_edge=True)
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="medium", cex_agreement_bucket="strong"),
        ttc_s=600.0, tv_feature=None, window_seconds=900)
    assert not ok and r == "baseline_cohort_edge_not_high"
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="low", cex_agreement_bucket="moderate"),
        ttc_s=600.0, tv_feature=None, window_seconds=900)
    assert not ok and r == "baseline_cohort_edge_not_high"


def test_15m_fast_lane_symmetric_when_up_restrictions_off():
    eng = _eng(baseline_cohort_15m_fast_lane=True,
               directional_up_restrictions_enabled=False,
               baseline_up_tv_gate_enabled=False,
               baseline_cohort_require_high_edge=False,
               baseline_cohort_require_strong_cex=False)
    ok, r = eng._baseline_quant_cohort_ok(
        side="up",
        esnap=_FakeEsnap(pulse_edge_score_bucket="medium", cex_agreement_bucket="moderate"),
        ttc_s=600.0, tv_feature=None, window_seconds=900)
    assert ok and r == ""


def test_15m_fast_lane_up_strict_when_restrictions_on():
    eng = _eng(baseline_cohort_15m_fast_lane=True,
               directional_up_restrictions_enabled=True,
               baseline_up_tv_gate_enabled=True)
    ok, r = eng._baseline_quant_cohort_ok(
        side="up",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=600.0, tv_feature=None, window_seconds=900)
    assert not ok and r == "baseline_up_tv_missing"


def test_blocks_medium_edge_and_late_ttc():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="medium", cex_agreement_bucket="strong"),
        ttc_s=200.0, tv_feature=None)
    assert not ok and r == "baseline_cohort_edge_not_high"
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=260.0, tv_feature=None)
    assert not ok and r == "baseline_cohort_ttc_too_late"


def test_allows_proven_down_cohort():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=200.0, tv_feature=None)
    assert ok and r == ""


def test_up_requires_tv_strong():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="up",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=200.0,
        tv_feature={"direction": "UP", "strength": 0.9, "signal_level": "UP_STRONG"})
    assert ok
    ok, r = eng._baseline_quant_cohort_ok(
        side="up",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=200.0,
        tv_feature={"direction": "UP", "strength": 0.5, "signal_level": "UP_WEAK"})
    assert not ok


def test_down_blocks_bullish_range_top_stack():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=200.0,
        tv_feature={
            "signal_level": "UP_STRONG",
            "mtf_alignment": "bullish_aligned",
            "range_state": "range_top",
        })
    assert not ok and r == "baseline_down_tv_bullish_mtf"


def test_down_allows_bearish_proven_stack():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="very_high", cex_agreement_bucket="strong"),
        ttc_s=200.0,
        tv_feature={
            "signal_level": "DOWN_STRONG",
            "mtf_alignment": "bearish_aligned",
            "range_state": "range_bottom",
        })
    assert ok and r == ""


def test_down_blocks_volume_active():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=200.0,
        tv_feature={"volume_state": "active", "signal_level": "DOWN_STRONG"})
    assert not ok and r == "baseline_down_tv_volume_active"


def test_down_blocks_not_stale_divergence():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(
            pulse_edge_score_bucket="high",
            cex_agreement_bucket="strong",
            stale_divergence_class="not_stale",
        ),
        ttc_s=200.0, tv_feature=None)
    assert not ok and r == "baseline_down_not_stale"
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(
            pulse_edge_score_bucket="high",
            cex_agreement_bucket="strong",
            stale_divergence_class="already_priced",
        ),
        ttc_s=200.0, tv_feature=None)
    assert ok and r == ""


def test_down_blocks_mid_entry_band():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(
            pulse_edge_score_bucket="high",
            cex_agreement_bucket="strong",
            stale_divergence_class="already_priced",
        ),
        ttc_s=200.0, tv_feature=None, ask_price=0.57)
    assert not ok and r == "baseline_down_mid_entry_band"
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(
            pulse_edge_score_bucket="high",
            cex_agreement_bucket="strong",
            stale_divergence_class="already_priced",
        ),
        ttc_s=200.0, tv_feature=None, ask_price=0.62)
    assert ok and r == ""


def test_down_blocks_bullish_mtf():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(
            pulse_edge_score_bucket="high",
            cex_agreement_bucket="strong",
            stale_divergence_class="already_priced",
        ),
        ttc_s=200.0,
        tv_feature={
            "signal_level": "DOWN_STRONG",
            "mtf_alignment": "bullish_aligned",
            "range_state": "range_middle",
        })
    assert not ok and r == "baseline_down_tv_bullish_mtf"


def test_down_blocks_up_strong_range_top_mixed_mtf():
    eng = _eng()
    ok, r = eng._baseline_quant_cohort_ok(
        side="down",
        esnap=_FakeEsnap(pulse_edge_score_bucket="high", cex_agreement_bucket="strong"),
        ttc_s=200.0,
        tv_feature={
            "signal_level": "UP_STRONG",
            "mtf_alignment": "mixed",
            "range_state": "range_top",
            "volume_state": "dead",
        })
    assert not ok and r == "baseline_down_tv_up_strong_range_top"