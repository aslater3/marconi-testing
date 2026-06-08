"""Unit tests for `analyse_contention`.

The contention analyser looks for transitions on a 'suspect' channel
that occur within 1000 ns of any chip-select channel transition. This
is the heuristic for bus-contention: the suspect line moves only when
a CS is asserted, indicating two drivers fighting for the bus.
"""
from __future__ import annotations
import pytest

from harness.analysis import analyse_contention
from tests._helpers import make_vcd


def test_no_captures_returns_skipped():
    r = analyse_contention({}, {})
    assert r.get("skipped") is True


def test_no_correlation_when_suspect_idle(tmp_vcd_dir):
    """If the suspect line never transitions while CS lines are
    toggling, no events are flagged — bus is clean."""
    # CS0 toggles, suspect (ch0) stays at 0
    cap = make_vcd(tmp_vcd_dir, "x", {
        1: [(0, 0), (100, 1), (200, 0), (300, 1), (400, 0)],
    }, duration_s=1.0)
    r = analyse_contention({cap.name: cap},
                           {"suspect_channel": 0, "cs_channels": [1]})
    assert r["summary"]["n_correlated_events"] == 0
    assert "clean" in r["summary"]["verdict"].lower()


def test_correlation_flagged(tmp_vcd_dir):
    """A suspect line that toggles within 1000 ns of a CS toggle is
    flagged as a correlated event (possible contention)."""
    # CS1 toggles at t=100, suspect (ch0) toggles at t=150 (within 50 ns)
    cap = make_vcd(tmp_vcd_dir, "x", {
        0: [(0, 0), (150, 1), (250, 0)],
        1: [(0, 0), (100, 1), (200, 0)],
    }, duration_s=1.0)
    r = analyse_contention({cap.name: cap},
                           {"suspect_channel": 0, "cs_channels": [1]})
    assert r["summary"]["n_correlated_events"] >= 1
    # The verdict should be "some contention" or "high"
    assert "contention" in r["summary"]["verdict"].lower()


def test_high_contention_verdict(tmp_vcd_dir):
    """More than 10 correlated events → 'HIGH contention activity'."""
    # 20 suspect toggles paired with 20 CS toggles, all within 100 ns
    cs_trans = []
    sus_trans = []
    for i in range(20):
        t = i * 200
        cs_trans.append((t, 1))
        cs_trans.append((t + 50, 0))
        sus_trans.append((t + 25, 1))
        sus_trans.append((t + 75, 0))
    cap = make_vcd(tmp_vcd_dir, "x",
                   {0: sus_trans, 1: cs_trans}, duration_s=1.0)
    r = analyse_contention({cap.name: cap},
                           {"suspect_channel": 0, "cs_channels": [1]})
    assert r["summary"]["n_correlated_events"] > 10
    assert "HIGH" in r["summary"]["verdict"].upper()


def test_event_delta_ns_signed(tmp_vcd_dir):
    """The delta_ns in each event is signed: positive means CS came
    AFTER the suspect, negative means CS came BEFORE."""
    # CS at t=200, suspect at t=100 → delta = 100 ns (CS after suspect)
    cap = make_vcd(tmp_vcd_dir, "x", {
        0: [(0, 0), (100, 1), (150, 0)],
        1: [(0, 0), (200, 1), (250, 0)],
    }, duration_s=1.0)
    r = analyse_contention({cap.name: cap},
                           {"suspect_channel": 0, "cs_channels": [1]})
    # Filter to events where the suspect is the rising edge (value=1) AND
    # the CS is the rising edge (value=1) — that's the contention signature
    suspect_rise_matched_cs_rise = [
        e for e in r["events"]
        if e["suspect_value"] == 1 and e["cs_value"] == 1
    ]
    assert suspect_rise_matched_cs_rise
    # CS rising came 100 ns after suspect rising
    assert suspect_rise_matched_cs_rise[0]["delta_ns"] == 100
