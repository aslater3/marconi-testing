"""Unit tests for `analyse_signal_integrity`.

The signal_integrity analyser counts sub-100ns inter-edge intervals to
detect the aliasing signature of a line oscillating faster than 24 MHz
(> 100ns/2). Verdicts: ok (<5%), degraded (5-20%), suspect (≥20%).
"""
from __future__ import annotations
import pytest

from harness.analysis import analyse_signal_integrity
from tests._helpers import (
    make_vcd, make_clean_strobe, make_capture,
)


def test_no_captures_returns_skipped():
    r = analyse_signal_integrity({}, {})
    assert r.get("skipped") is True


def test_clean_signal_ok(tmp_vcd_dir):
    """A 1 kHz strobe has 1 ms periods — 0% sub-100ns intervals."""
    cap = make_clean_strobe(tmp_vcd_dir, "x", strobe_ch=0, freq_hz=1000,
                            duration_s=0.1)
    r = analyse_signal_integrity({cap.name: cap}, {})
    ch0 = r["channels"]["ch0"]
    assert ch0["health"] == "ok"
    assert ch0["pct_sub100ns"] == 0.0
    assert ch0["min_gap_ns"] >= 100
    assert r["summary"]["verdict"].startswith("OK")


def test_suspect_bus_contention_signature(tmp_vcd_dir):
    """A line toggling at 48 MHz has 50% sub-100ns intervals (every
    second gap is ~21 ns at 24 MHz sample rate). This is the bus
    contention tell."""
    # 48 MHz: half-period = 10.4 ns. The 24 MHz LA can only sample
    # every 41.67 ns, so it aliases — half the inter-edge intervals
    # land at ~21 ns (sub-100ns), the other half at ~63 ns (sub-100ns).
    period_ns = 20  # 50 MHz
    half = period_ns // 2
    n_pulses = 1000
    trans = []
    for i in range(n_pulses):
        trans.append((i * period_ns, 1))
        trans.append((i * period_ns + half, 0))
    cap = make_vcd(tmp_vcd_dir, "x", {0: trans}, duration_s=1.0)
    r = analyse_signal_integrity({cap.name: cap}, {})
    ch0 = r["channels"]["ch0"]
    assert ch0["health"] == "suspect"
    assert ch0["pct_sub100ns"] >= 20.0
    assert r["summary"]["verdict"].startswith("SUSPECT")


def test_degraded_mild_aliasing(tmp_vcd_dir):
    """A line with ~10% sub-100ns intervals is 'degraded' (worth a
    scope check) but not yet 'suspect'."""
    # Mix of long and short intervals to land in 5-20%
    trans = []
    t = 0
    for i in range(100):
        # 9 long intervals (1000 ns) + 1 short (50 ns) = 10% sub-100ns
        for _ in range(9):
            trans.append((t, 1))
            t += 1000
            trans.append((t, 0))
            t += 1000
        # short pair
        trans.append((t, 1))
        t += 50
        trans.append((t, 0))
        t += 50
    cap = make_vcd(tmp_vcd_dir, "x", {0: trans}, duration_s=1.0)
    r = analyse_signal_integrity({cap.name: cap}, {})
    ch0 = r["channels"]["ch0"]
    # 10% of intervals are sub-100ns → degraded (5-20%)
    assert ch0["health"] == "degraded"
    assert 5.0 <= ch0["pct_sub100ns"] < 20.0


def test_few_edges_health_ok(tmp_vcd_dir):
    """A channel with <2 edges has no intervals to measure — health=ok
    with a note."""
    cap = make_vcd(tmp_vcd_dir, "x", {0: [(0, 1)]}, duration_s=1.0)
    r = analyse_signal_integrity({cap.name: cap}, {})
    ch0 = r["channels"]["ch0"]
    assert ch0["health"] == "ok"
    assert "fewer than 2" in ch0["notes"]


def test_summary_pct_sub100ns_aggregates_across_channels(tmp_vcd_dir):
    """The summary's pct_sub100ns_overall should aggregate all channels."""
    # CH0: 50% sub-100ns, CH1: 0% sub-100ns
    # Overall: ~25%
    period_ns = 20
    half = period_ns // 2
    fast_trans = []
    for i in range(100):
        fast_trans.append((i * period_ns, 1))
        fast_trans.append((i * period_ns + half, 0))
    slow_trans = [(0, 0), (1000, 1), (2000, 0)] * 10
    cap = make_vcd(tmp_vcd_dir, "x",
                   {0: fast_trans, 1: slow_trans}, duration_s=1.0)
    r = analyse_signal_integrity({cap.name: cap}, {})
    # CH0 is suspect, CH1 is ok
    assert r["channels"]["ch0"]["health"] == "suspect"
    assert r["channels"]["ch1"]["health"] == "ok"
    # The overall is between 0 and 100
    assert 0 < r["summary"]["pct_sub100ns_overall"] < 100


def test_median_gap_is_robust_to_aliasing(tmp_vcd_dir):
    """The median gap should reflect the typical period, not the
    sub-sample aliased values. A 10 kHz strobe has median ~50 µs."""
    cap = make_clean_strobe(tmp_vcd_dir, "x", strobe_ch=0, freq_hz=10_000,
                            duration_s=0.1)
    r = analyse_signal_integrity({cap.name: cap}, {})
    ch0 = r["channels"]["ch0"]
    # 10 kHz = 100 µs period, half-period = 50 µs
    # The analyser sorts gaps and picks the middle one
    assert 40_000 < ch0["median_gap_ns"] < 60_000
