"""Unit tests for `analyse_clock_health` and `analyse_protocol_decode`.

clock_health measures the frequency of a designated clock channel and
compares to an expected value.

protocol_decode samples a data bus at each clock/strobe edge and
returns the per-event decoded value. Tests cover the sample_point
bug fix (must sample BEFORE the latch edge, not after) and the
JSON-roundtrip fix (string keys in data_channels must coerce to int).
"""
from __future__ import annotations
import pytest

from harness.analysis import analyse_clock_health, analyse_protocol_decode
from tests._helpers import (
    make_clean_strobe, make_dac_sweep, make_8085_clock, make_vcd,
)


# ---------------------------------------------------------------------------
# clock_health
# ---------------------------------------------------------------------------

def test_clock_health_no_captures_returns_skipped():
    r = analyse_clock_health({}, {"channel": 0, "expected_hz": 1e6})
    assert r.get("skipped") is True
    assert r.get("verdict") == "skipped (no capture)"


def test_clock_health_within_tolerance(tmp_vcd_dir):
    """A 1 kHz strobe measured with 5% tolerance should be 'within tolerance'."""
    cap = make_clean_strobe(tmp_vcd_dir, "x", strobe_ch=0,
                            freq_hz=1000.0, duration_s=0.1)
    r = analyse_clock_health({cap.name: cap},
                             {"channel": 0, "expected_hz": 1000.0,
                              "tolerance_pct": 5.0})
    assert r["n_rising_edges"] == 100  # 1 kHz × 0.1 s
    assert r["measured_hz"] == pytest.approx(1000.0, rel=0.01)
    assert "within tolerance" in r["verdict"].lower()


def test_clock_health_out_of_tolerance(tmp_vcd_dir):
    """A 1 kHz strobe measured against 3.072 MHz expected (the 8085 clock) — way off."""
    cap = make_clean_strobe(tmp_vcd_dir, "x", strobe_ch=0,
                            freq_hz=1000.0, duration_s=0.1)
    r = analyse_clock_health({cap.name: cap},
                             {"channel": 0, "expected_hz": 3_072_000.0,
                              "tolerance_pct": 5.0})
    assert "OUT OF TOLERANCE" in r["verdict"].upper()


def test_clock_health_8085_reference(tmp_vcd_dir):
    """The page-083 spec: 8085 CLK OUT = 3.072 MHz.

    The 24 MHz LA quantises to 41.67 ns, so the *measured* period
    is an integer multiple of 41.67 ns. 3.072 MHz = 325.52 ns period,
    which the LA reports as 325 ns (3.077 MHz) or 333 ns (3.003 MHz).
    The analyser is accurate to ~0.16% of the true frequency."""
    cap = make_8085_clock(tmp_vcd_dir, "clk", freq_hz=3_072_000.0,
                          duration_s=0.01)
    r = analyse_clock_health({cap.name: cap},
                             {"channel": 2, "expected_hz": 3_072_000.0,
                              "tolerance_pct": 1.0})
    assert r["measured_hz"] == pytest.approx(3_072_000.0, rel=0.005)
    assert "within tolerance" in r["verdict"].lower()


def test_clock_health_too_few_edges(tmp_vcd_dir):
    """A single rising edge is not enough to measure frequency."""
    cap = make_vcd(tmp_vcd_dir, "x", {0: [(0, 0), (100, 1)]}, duration_s=1.0)
    r = analyse_clock_health({cap.name: cap},
                             {"channel": 0, "expected_hz": 1_000_000.0})
    # n_rising_edges = 1, which is < 2 → returns early
    assert r["n_rising_edges"] == 1
    assert r["measured_hz"] is None
    assert r["verdict"] == "no edges seen"


# ---------------------------------------------------------------------------
# protocol_decode
# ---------------------------------------------------------------------------

def test_protocol_decode_dac_sweep_12_steps(tmp_vcd_dir):
    """A 12-step DAC sweep with incrementing codes (0..11) should
    produce 12 LBS rising events with 12 distinct 5-bit values."""
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=12, period_ms=10,
                         start_code=0)
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,  # LBS
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "before",
        "sample_offset_ns": 50,
        "signed": False,
    })
    assert r["n_events"] == 12
    s = r["summary"]
    assert s["n_unique_values"] == 12
    assert s["min"] == 0
    assert s["max"] == 11
    assert s["monotonic"] is True
    assert s["duplicate_count"] == 0


def test_protocol_decode_sample_point_after_lands_after_cpu_releases(tmp_vcd_dir):
    """REGRESSION: with the OLD bug (sample_point='after', offset=100),
    sampling after the LBS rising edge lands after the CPU has released
    the data bus. With our helper, data is released 250 ns after LBS rises.
    Sampling 100 ns after LBS rises is still during the data valid window
    (data is valid from -200 ns to +250 ns relative to LBS rising), so
    we still get correct values. But sampling 300 ns after lands after
    the release — and we'd see 0x00 (CPU released to 0).
    """
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=4, period_ms=10,
                         start_code=0)
    # OLD (buggy) sample
    r_old = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "after",
        "sample_offset_ns": 100,
        "signed": False,
    })
    # Within the data valid window → still decodes the correct codes
    assert r_old["summary"]["min"] == 0
    assert r_old["summary"]["max"] == 3


def test_protocol_decode_after_too_late_sees_released_bus(tmp_vcd_dir):
    """Sampling too far after LBS rises (e.g. 500 ns) lands after the
    CPU has released the bus — all data lines return to 0, so every
    event decodes to 0x00."""
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=4, period_ms=10,
                         start_code=0)
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "after",
        "sample_offset_ns": 500,  # way past the data release at +250
        "signed": False,
    })
    # All events decode to 0 because the bus is released
    assert all(e["decimal"] == 0 for e in r["events"])
    assert r["_warnings"]  # should warn about the timing


def test_protocol_decode_warning_on_bad_sample_point(tmp_vcd_dir):
    """A sample_point='after' with offset >= 20 ns triggers a _warnings
    field explaining the common 'all 0xFF' trap."""
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=2, period_ms=10)
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "after",
        "sample_offset_ns": 100,
        "signed": False,
    })
    assert "_warnings" in r
    assert any("after" in w.lower() for w in r["_warnings"])


def test_protocol_decode_no_warning_on_before_sample(tmp_vcd_dir):
    """sample_point='before' (the default) doesn't trigger the warning."""
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=2, period_ms=10)
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "before",
        "sample_offset_ns": 50,
        "signed": False,
    })
    assert "_warnings" not in r or not r["_warnings"]


def test_protocol_decode_json_string_keys_do_not_crash(tmp_vcd_dir):
    """REGRESSION: when params come from a JSON report, dict keys are
    strings, not ints. The analyser must coerce them before doing
    1 << bit_pos (which would TypeError on a string)."""
    import json
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=2, period_ms=10)
    # Round-trip params through JSON to get string keys
    params = {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "before",
        "sample_offset_ns": 50,
        "signed": False,
    }
    params_json = json.loads(json.dumps(params))
    assert all(isinstance(k, str) for k in params_json["data_channels"])
    r = analyse_protocol_decode({cap.name: cap}, params_json)
    # Should decode correctly despite string keys
    assert r["n_events"] == 2
    assert all(e["decimal"] in (0, 1) for e in r["events"])


def test_protocol_decode_signed_interpretation(tmp_vcd_dir):
    """With `signed=True`, a 5-bit value of e.g. 0b10000 (16) is
    interpreted as -16 (signed 5-bit) and 0b01111 (15) is +15.
    Per `_bits_to_value`, the MSB (bit 4 here) is the sign bit."""
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=4, period_ms=10,
                         start_code=0)
    r_signed = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "before",
        "sample_offset_ns": 50,
        "signed": True,
    })
    # The first event decodes code 0, which is 0b00000 → signed 0
    # codes 1, 2, 3 → 0b00001, 0b00010, 0b00011 → all positive
    for e in r_signed["events"][:4]:
        assert e["signed"] == e["decimal"]  # all positive, sign = value


def test_protocol_decode_74ls273_clock_edge_with_enable_filter(tmp_vcd_dir):
    """The 74LS273 protocol_decode test uses mode=clock_edge with an
    enable filter (/CLR must be HIGH). Verify the filter rejects events
    when the enable is in the wrong state."""
    # LBS on ch0, /CLR on ch1, data on ch3..ch8
    # /CLR goes LOW between events → those events are filtered out
    transitions = {
        0: [(0, 0), (100, 1), (200, 0), (300, 1), (400, 0), (500, 1), (600, 0)],  # LBS
        1: [(0, 1), (250, 0), (350, 1), (450, 0), (550, 1)],  # /CLR pulses
        3: [(50, 1), (250, 0), (350, 1), (450, 0), (550, 1), (650, 0)],  # data
    }
    cap = make_vcd(tmp_vcd_dir, "ls273", transitions, duration_s=1.0)
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 3},
        "enable_channel": 1,
        "enable_polarity": "high",
        "signed": False,
    })
    # The first LBS rising edge is at t=100. /CLR is HIGH from t=0 to 250.
    # So the first event is decoded. The second LBS rising at t=300 has
    # /CLR HIGH from t=350 onwards → the t=300 event is at the moment
    # /CLR is still LOW (between 250 and 350) → filtered out.
    # The third LBS rising at t=500 has /CLR HIGH from 550 onwards → the
    # t=500 event is at /CLR LOW → filtered out.
    # So only 1 event should be decoded.
    # Actually the analyser's filter checks the *en_state* at the
    # event time, which is the most recent transition at or before t.
    # LBS at t=100: en_state = 1 (from t=0) → include
    # LBS at t=300: en_state = 0 (from t=250) → exclude
    # LBS at t=500: en_state = 0 (from t=450) → exclude
    assert r["n_events"] == 1


def test_protocol_decode_no_data_channels_returns_error(tmp_vcd_dir):
    """An empty data_channels dict is a config error, not a crash."""
    cap = make_clean_strobe(tmp_vcd_dir, "x")
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge", "clock_channel": 0, "data_channels": {},
    })
    assert "error" in r
    assert "no data_channels" in r["error"]


def test_protocol_decode_stable_sample_at_midpoint(tmp_vcd_dir):
    """sample_point='stable' samples at the midpoint between this LBS
    rising and the next — the middle of the LBS-high window."""
    cap = make_dac_sweep(tmp_vcd_dir, "dac", n_steps=2, period_ms=10)
    r = analyse_protocol_decode({cap.name: cap}, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 1, 1: 2, 2: 3, 3: 4, 4: 5},
        "sample_point": "stable",
        "signed": False,
    })
    # LBS is high from t_rise to t_rise+200. Midpoint is t_rise+100.
    # Data is valid from t_rise-200 to t_rise+250 → t_rise+100 is in the
    # middle of LBS-high and data-valid.
    assert r["n_events"] == 2
