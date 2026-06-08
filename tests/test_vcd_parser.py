"""Unit tests for the VCD parser (`harness.capture.parse_vcd_transitions`).

These cover the edge cases the parser has to handle on real captures:
  - single-channel extraction from sigrok's packed `#<ts> v<id>` initial state
  - sigrok's 100 ps timescale and harness's 1 ns timescale
  - both plain .vcd and gzipped .vcd.gz
  - the fallback var_id 'a'+channel when the header doesn't name the channel
"""
from __future__ import annotations
import gzip
import json
from pathlib import Path
import pytest

from harness.capture import (
    parse_vcd_transitions, _vcd_timescale_to_ns, _VCD_HEADER_CACHE,
)
from tests._helpers import _write_vcd, make_capture


@pytest.fixture(autouse=True)
def _clear_vcd_cache():
    _VCD_HEADER_CACHE.clear()
    yield
    _VCD_HEADER_CACHE.clear()


def test_single_channel_extraction(tmp_vcd_dir):
    """The parser should return only the transitions of the requested channel,
    in (timestamp_ns, value) pairs, sorted by timestamp.

    The harness's `_write_vcd` helper sets the VCD's initial state at t=0
    to the *first* transition's value (so a "starts at 1" test only needs
    to list the 1 once). The parser returns the initial-state line plus
    every subsequent transition."""
    vcd = _write_vcd(tmp_vcd_dir / "x.vcd", {
        0: [(0, 0), (100, 1), (200, 0), (500, 1)],
        1: [(0, 0), (150, 1), (300, 0)],
    })
    t = parse_vcd_transitions(vcd, 0)
    assert t == [(0, 0), (100, 1), (200, 0), (500, 1)]


def test_other_channels_excluded(tmp_vcd_dir):
    """A request for channel 0 must not include transitions from channel 1."""
    vcd = _write_vcd(tmp_vcd_dir / "x.vcd", {
        0: [(0, 0), (100, 1)],
        1: [(0, 0), (150, 1), (300, 0)],
        7: [(0, 0), (200, 1)],
    })
    t = parse_vcd_transitions(vcd, 0)
    assert t == [(0, 0), (100, 1)]
    t7 = parse_vcd_transitions(vcd, 7)
    assert t7 == [(0, 0), (200, 1)]


def test_100ps_timescale_converts_to_ns(tmp_vcd_dir):
    """sigrok emits '100 ps' timescale — the parser must multiply timestamps
    by 0.1 to get ns."""
    vcd = _write_vcd(tmp_vcd_dir / "x.vcd", {0: [(0, 0), (1000, 1), (5000, 0)]},
                     timescale="100 ps")
    t = parse_vcd_transitions(vcd, 0)
    # 1000 * 0.1 = 100 ns,  5000 * 0.1 = 500 ns
    assert t == [(0, 0), (100, 1), (500, 0)]


def test_1ns_timescale_unchanged(tmp_vcd_dir):
    vcd = _write_vcd(tmp_vcd_dir / "x.vcd", {0: [(0, 0), (42, 1), (100, 0)]},
                     timescale="1 ns")
    t = parse_vcd_transitions(vcd, 0)
    assert t == [(0, 0), (42, 1), (100, 0)]


def test_gzipped_vcd_read(tmp_vcd_dir):
    """The harness writes .vcd.gz in production. The parser must read it
    without the caller having to know which form is on disk."""
    import gzip
    plain = _write_vcd(tmp_vcd_dir / "x.vcd", {0: [(0, 0), (50, 1), (200, 0)]})
    gz = tmp_vcd_dir / "x.vcd.gz"
    with open(plain, "rb") as f_in, gzip.GzipFile(gz, "wb") as f_out:
        f_out.write(f_in.read())
    plain.unlink()
    t = parse_vcd_transitions(gz, 0)
    assert t == [(0, 0), (50, 1), (200, 0)]


def test_missing_file_returns_empty(tmp_path):
    """A non-existent VCD should yield [] rather than raise. This protects
    the analysers when a capture is deleted but the report remains."""
    t = parse_vcd_transitions(tmp_path / "nope.vcd", 0)
    assert t == []


def test_packed_initial_state(tmp_vcd_dir):
    """sigrok packs initial-state transitions on the first `#0` line. The
    parser must extract the right channel's initial value."""
    # Manually craft a sigrok-style VCD with packed initial state
    p = tmp_vcd_dir / "packed.vcd"
    p.write_text(
        "$timescale 1 ns $end\n"
        "$scope module logic $end\n"
        "$var wire 1 ! ch0 $end\n"
        "$var wire 1 \" ch1 $end\n"
        "$upscope $end\n"
        "$enddefinitions $end\n"
        "#0 1! 0\"\n"
        "#100 0!\n"
        "#200 1\"\n"
    )
    ch0 = parse_vcd_transitions(p, 0)
    ch1 = parse_vcd_transitions(p, 1)
    assert ch0 == [(0, 1), (100, 0)]
    assert ch1 == [(0, 0), (200, 1)]


def test_fallback_var_id_a_plus_channel(tmp_vcd_dir):
    """If the header doesn't define a channel, the parser falls back to
    var_id = 'a' + channel_int. This is the old-harness behaviour that
    keeps the parser working on non-D0..D7 names."""
    p = tmp_vcd_dir / "old.vcd"
    # Channel 0 only — var_id 'a'
    p.write_text(
        "$timescale 1 ns $end\n"
        "$scope module logic $end\n"
        "$var wire 1 a foo $end\n"
        "$upscope $end\n"
        "$enddefinitions $end\n"
        "#0 0a\n"
        "#50 1a\n"
    )
    t = parse_vcd_transitions(p, 0)
    assert t == [(0, 0), (50, 1)]


def test_empty_vcd_returns_single_initial(tmp_vcd_dir):
    """A VCD with only the initial state emits exactly one transition
    (the t=0 initial value). This matches how the analysers count edges
    on a stuck-low line — they see the initial state and treat the rest
    of the capture as 'no activity'."""
    p = tmp_vcd_dir / "silent.vcd"
    p.write_text(
        "$timescale 1 ns $end\n"
        "$scope module logic $end\n"
        "$var wire 1 a ch0 $end\n"
        "$upscope $end\n"
        "$enddefinitions $end\n"
        "#0 0a\n"
    )
    t = parse_vcd_transitions(p, 0)
    assert t == [(0, 0)]


@pytest.mark.parametrize("spec,expected", [
    ("1 ns",  1.0),
    ("10ns",  10.0),
    ("100 ps", 0.1),
    ("1ps",   0.001),
    ("10 us", 10_000.0),
    ("1ms",   1_000_000.0),
    ("ns",    1.0),     # bare unit
    ("1",     1.0),     # bare number, defaults to ns
    ("",      1.0),     # empty
    ("garbage", 1.0),   # unknown defaults to 1.0
])
def test_timescale_to_ns(spec, expected):
    """The helper that turns VCD timescale strings into ns multipliers."""
    assert _vcd_timescale_to_ns(spec) == expected
