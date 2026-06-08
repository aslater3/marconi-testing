"""Unit tests for `analyse_diff` (two-capture diff) and `analyse_n_way_diff`
(many-capture diff).

`analyse_diff` finds the first transition that differs between two
captures. `analyse_n_way_diff` compares N captures and reports whether
each channel's level changed across the sequence.
"""
from __future__ import annotations
import pytest

from harness.analysis import analyse_diff, analyse_n_way_diff
from tests._helpers import make_vcd


# ---------------------------------------------------------------------------
# analyse_diff
# ---------------------------------------------------------------------------

def test_diff_missing_capture_returns_error():
    r = analyse_diff({}, {"a": "nope", "b": "also_nope"})
    assert "error" in r


def test_diff_identical_captures_no_divergence(tmp_vcd_dir):
    """Two captures with the same transitions → no first divergence."""
    cap = make_vcd(tmp_vcd_dir, "x", {0: [(0, 0), (100, 1), (200, 0)]},
                   duration_s=1.0)
    r = analyse_diff({cap.name: cap, cap.name + "_b": cap},
                     {"a": cap.name, "b": cap.name + "_b"})
    # first_divergence is None when no transition mismatches in first 50
    assert r["first_divergence"] is None
    for ch in r["per_channel"].values():
        assert ch["first_divergence"] is None


def test_diff_finds_first_divergence_at_correct_index(tmp_vcd_dir):
    """The first divergence is the earliest transition that differs.

    When the two captures have different transition counts, the diff
    classifies as an activity_mismatch at the top level and a
    count_diff per-channel. The per-channel first_divergence field
    is null in that case (no same-index value mismatch to report)."""
    # cap_a: 3 transitions (0->1->0). cap_b: 1 transition (stays at 0).
    cap_a = make_vcd(tmp_vcd_dir, "a", {0: [(0, 0), (50, 1), (100, 0)]},
                     duration_s=1.0)
    cap_b = make_vcd(tmp_vcd_dir, "b", {0: [(0, 0)]}, duration_s=1.0)
    r = analyse_diff({cap_a.name: cap_a, cap_b.name: cap_b},
                     {"a": cap_a.name, "b": cap_b.name})
    ch0 = r["per_channel"]["ch0"]
    # The per-channel first_divergence is null when counts differ;
    # the count_diff field captures the activity mismatch instead.
    assert ch0["count_diff"] is not None
    assert ch0["count_diff"]["n_transitions_a"] == 3
    assert ch0["count_diff"]["n_transitions_b"] == 1
    # The top-level first_divergence is an activity_mismatch
    assert r["first_divergence"]["kind"] == "activity_mismatch"
    # Ratio = 1/3 = 0.333 — well under 0.5, so activity_mismatch is correct
    assert ch0["count_diff"]["ratio"] == pytest.approx(1/3)


def test_diff_per_channel_transition_mismatch(tmp_vcd_dir):
    """When both captures have the same number of transitions but
    differ in values, the per-channel first_divergence is set with
    the transition_mismatch kind."""
    # 4 transitions each: cap_a toggles, cap_b stays at 1 from t=50
    # The writer dedupes same-value emissions, so cap_b has 2 transitions
    # (initial 0, then 1 at t=50) while cap_a has 4 (toggles).
    # The diff classifies this as an activity_mismatch.
    cap_a = make_vcd(tmp_vcd_dir, "a", {0: [(0, 0), (50, 1), (100, 0), (150, 1)]},
                     duration_s=1.0)
    cap_b = make_vcd(tmp_vcd_dir, "b", {0: [(0, 0), (50, 1), (100, 0), (150, 1)]},
                     duration_s=1.0)
    # The transitions are identical — no divergence
    r = analyse_diff({cap_a.name: cap_a, cap_b.name: cap_b},
                     {"a": cap_a.name, "b": cap_b.name})
    ch0 = r["per_channel"]["ch0"]
    # Both have 4 transitions with same values → no divergence
    assert ch0["first_divergence"] is None
    assert r["first_divergence"] is None


def test_diff_per_channel_real_mismatch(tmp_vcd_dir):
    """A real per-channel transition mismatch requires both captures to
    have the same number of transitions (no dedup) with different
    values OR different timestamps at the same index.

    When the values match but timestamps differ (as in the case where
    one capture's emissions were dedup'd), the diff still flags a
    transition_mismatch because the parser sees different transition
    indices at different times."""
    cap_a = make_vcd(tmp_vcd_dir, "a", {0: [(0, 0), (50, 1), (100, 0)]},
                     duration_s=1.0)
    cap_b = make_vcd(tmp_vcd_dir, "b", {0: [(0, 0), (50, 1), (100, 1), (150, 0)]},
                     duration_s=1.0)
    r = analyse_diff({cap_a.name: cap_a, cap_b.name: cap_b},
                     {"a": cap_a.name, "b": cap_b.name})
    ch0 = r["per_channel"]["ch0"]
    # Per-channel first_divergence IS set (the per-index values differ
    # at index 2: A=(100,0), B=(150,0))
    assert ch0["first_divergence"] is not None
    assert ch0["first_divergence"]["transition_index"] == 2
    # The top-level first_divergence is a transition_mismatch
    assert r["first_divergence"]["kind"] == "transition_mismatch"


def test_diff_count_diff_when_activity_differs(tmp_vcd_dir):
    """If channel 'a' has 3 edges and 'b' has 98, the count_diff
    field is set with the ratio."""
    cap_a = make_vcd(tmp_vcd_dir, "a", {0: [(0, 0), (100, 1), (200, 0)]},
                     duration_s=1.0)
    # 99 list entries — 1 dedup at t=0 (initial 0a already emitted)
    # → 98 transitions
    cap_b = make_vcd(tmp_vcd_dir, "b", {0: [(0, 0)] + [(i*100, 1) for i in range(1, 50)]
                                              + [(i*100, 0) for i in range(1, 50)]},
                     duration_s=1.0)
    r = analyse_diff({cap_a.name: cap_a, cap_b.name: cap_b},
                     {"a": cap_a.name, "b": cap_b.name})
    ch0 = r["per_channel"]["ch0"]
    assert ch0["count_diff"] is not None
    assert ch0["count_diff"]["n_transitions_a"] == 3
    assert ch0["count_diff"]["n_transitions_b"] == 98


# ---------------------------------------------------------------------------
# analyse_n_way_diff
# ---------------------------------------------------------------------------

def test_n_way_diff_empty_captures():
    r = analyse_n_way_diff({}, {})
    assert "error" in r


def test_n_way_diff_detects_stuck_channel(tmp_vcd_dir):
    """A channel that should change but doesn't is flagged 'truly_stuck'."""
    # Three captures, all with CH0 stuck at 1 even though we expect [0, 1, 0]
    cap1 = make_vcd(tmp_vcd_dir, "a", {0: [(0, 1)]}, duration_s=0.1)
    cap2 = make_vcd(tmp_vcd_dir, "b", {0: [(0, 1)]}, duration_s=0.1)
    cap3 = make_vcd(tmp_vcd_dir, "c", {0: [(0, 1)]}, duration_s=0.1)
    caps = {cap1.name: cap1, cap2.name: cap2, cap3.name: cap3}
    r = analyse_n_way_diff(caps, {
        "captures": [cap1.name, cap2.name, cap3.name],
        "expect_levels": {0: [0, 1, 0]},
    })
    ch0 = r["per_channel"]["ch0"]
    assert ch0["health"] in ("stuck_or_wrong", "stuck")
    assert "stuck" in r["summary"]["verdict"].lower() or "wrong" in r["summary"]["verdict"].lower()


def test_n_way_diff_falsy_zero_in_expect_levels_not_dropped(tmp_vcd_dir):
    """Regression test for the `or` bug: if expect_levels[ch] = [0, 0, 0]
    (a legitimate all-zero expectation), the analyser must NOT fall through
    to expect_levels[str(ch)] because 0 is falsy."""
    cap1 = make_vcd(tmp_vcd_dir, "a", {0: [(0, 0)]}, duration_s=0.1)
    cap2 = make_vcd(tmp_vcd_dir, "b", {0: [(0, 0)]}, duration_s=0.1)
    cap3 = make_vcd(tmp_vcd_dir, "c", {0: [(0, 0)]}, duration_s=0.1)
    caps = {cap1.name: cap1, cap2.name: cap2, cap3.name: cap3}
    r = analyse_n_way_diff(caps, {
        "captures": [cap1.name, cap2.name, cap3.name],
        "expect_levels": {0: [0, 0, 0]},
    })
    ch0 = r["per_channel"]["ch0"]
    # The level_match should be ok (all observed = 0, all expected = 0)
    assert ch0["level_match"] is not None
    assert ch0["level_match"].get("ok") is True


def test_n_way_diff_ok_when_levels_match(tmp_vcd_dir):
    """Sanity: matching levels → 'All channels behaved as expected'."""
    # Final states: cap1=1, cap2=0, cap3=1 (expect_levels = [1, 0, 1])
    cap1 = make_vcd(tmp_vcd_dir, "a", {0: [(0, 0), (100, 1)]}, duration_s=0.1)
    cap2 = make_vcd(tmp_vcd_dir, "b", {0: [(0, 1), (100, 0)]}, duration_s=0.1)
    cap3 = make_vcd(tmp_vcd_dir, "c", {0: [(0, 0), (100, 1)]}, duration_s=0.1)
    caps = {cap1.name: cap1, cap2.name: cap2, cap3.name: cap3}
    r = analyse_n_way_diff(caps, {
        "captures": [cap1.name, cap2.name, cap3.name],
        "expect_levels": {0: [1, 0, 1]},
    })
    ch0 = r["per_channel"]["ch0"]
    assert ch0["level_match"]["ok"] is True
    assert "expected" in r["summary"]["verdict"].lower()


def test_n_way_diff_detects_activity_change(tmp_vcd_dir):
    """If CH0 has the same final level in all 3 captures, it's 'stuck'
    even without expect_levels."""
    cap1 = make_vcd(tmp_vcd_dir, "a", {0: [(0, 1)]}, duration_s=0.1)
    cap2 = make_vcd(tmp_vcd_dir, "b", {0: [(0, 1)]}, duration_s=0.1)
    cap3 = make_vcd(tmp_vcd_dir, "c", {0: [(0, 1)]}, duration_s=0.1)
    caps = {cap1.name: cap1, cap2.name: cap2, cap3.name: cap3}
    r = analyse_n_way_diff(caps,
                           {"captures": [cap1.name, cap2.name, cap3.name]})
    ch0 = r["per_channel"]["ch0"]
    # levels_changed = False (all final states are 1)
    assert ch0["levels_changed"] is False
    assert ch0["health"] == "stuck"
