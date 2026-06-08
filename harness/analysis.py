"""Post-capture analysis.

Each analyser takes a list of Captures (by id) plus the full event log, and returns
a result dict that gets embedded in the JSON report.

Analysers:
  - bus_census: per-channel health summary, compare to reference capture
  - contention: detect transitions on a suspect line outside expected windows
  - diff:       sample-by-sample diff of two captures, find first divergence
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .capture import Capture, parse_vcd_transitions


def _resolve_capture(captures: dict, ref: str):
    """Resolve a capture reference. Exact match first, then prefix match."""
    if ref in captures:
        return captures[ref]
    matches = [k for k in captures if k.startswith(ref + "_") or k == ref]
    if matches:
        return captures[matches[0]]
    return None


# -----------------------------------------------------------------------------
# n_way_diff
# -----------------------------------------------------------------------------

def analyse_n_way_diff(captures: dict[str, Capture], params: dict) -> dict:
    """Compare N captures pairwise. For each channel, report whether the transition
    count varies (activity) and whether the final value changes across captures
    (level).

    The '274 / 273 sequence test' uses this to ask: "across N successive DAC
    writes, did channel X actually change value, or did it get stuck?"

    params:
      captures: ordered list of capture ids
      expect_levels: optional dict of {channel: [v0, v1, v2, ...]} — expected
                     final-state value of each channel at each capture. If provided,
                     the analyser flags mismatches. If absent, it just reports
                     observed level sequence.
    """
    cap_ids = params.get("captures", [])
    expect_levels = params.get("expect_levels", {})

    # Resolve each id
    resolved = []
    for cid in cap_ids:
        cap = _resolve_capture(captures, cid)
        if cap is None:
            return {"kind": "n_way_diff", "error": f"missing capture: {cid}"}
        resolved.append(cap)

    if not resolved:
        return {"kind": "n_way_diff", "error": "no captures provided"}

    channels = resolved[0].channels

    out: dict[str, Any] = {
        "kind": "n_way_diff",
        "n_captures": len(resolved),
        "capture_names": [c.name for c in resolved],
        "per_channel": {},
    }

    for ch in channels:
        ch_key = f"ch{ch}"
        per_capture = []
        for i, cap in enumerate(resolved):
            trans = parse_vcd_transitions(cap.vcd_path, ch) if cap.vcd_path else []
            # Final state: scan transitions to find the last value
            final = trans[-1][1] if trans else 0
            per_capture.append({
                "capture_index": i,
                "capture_name": cap.name,
                "n_transitions": len(trans),
                "final_state": final,
            })

        # Check level sequence
        final_states = [p["final_state"] for p in per_capture]
        levels_changed = len(set(final_states)) > 1

        # Check expected levels (if provided)
        # Use explicit None check rather than `or` so a legitimate expected
        # value of 0 or [] is not silently dropped.
        exp = expect_levels.get(str(ch))
        if exp is None:
            exp = expect_levels.get(ch)
        level_match = None
        if exp is not None:
            if len(exp) != len(final_states):
                level_match = {"error": f"expected {len(exp)} levels, got {len(final_states)}"}
            else:
                mismatches = []
                for i, (a, b) in enumerate(zip(exp, final_states)):
                    if a != b:
                        mismatches.append({"index": i, "expected": a, "actual": b})
                level_match = {
                    "ok": len(mismatches) == 0,
                    "mismatches": mismatches,
                }

        # Health verdict
        if exp is not None and level_match and not level_match.get("ok", True):
            health = "stuck_or_wrong"
            notes = f"expected levels {exp}, got {final_states}"
        elif not levels_changed:
            health = "stuck"  # channel never changed value across all captures
            notes = f"final_state constant at {final_states[0]} across {len(final_states)} captures"
        else:
            health = "ok"
            notes = f"final_state sequence: {final_states}"

        out["per_channel"][ch_key] = {
            "channel": ch,
            "health": health,
            "notes": notes,
            "levels_changed": levels_changed,
            "final_state_sequence": final_states,
            "level_match": level_match,
            "per_capture": per_capture,
        }

    # Summary: count of stuck vs ok
    # Only flag as "stuck" if the channel SHOULD have changed (expect_levels has
    # at least one non-constant value for it) but didn't.
    truly_stuck = []
    wrong = []
    for ch, info in out["per_channel"].items():
        if info["health"] == "stuck_or_wrong":
            wrong.append(ch)
        elif info["health"] == "stuck":
            # Check if it was expected to change
            exp = expect_levels.get(str(ch))
            if exp is None:
                exp = expect_levels.get(ch)
            if exp is not None and len(set(exp)) > 1:
                # Expected to change but didn't — truly stuck
                truly_stuck.append(ch)
    out["summary"] = {
        "n_ok": len(out["per_channel"]) - len(truly_stuck) - len(wrong),
        "n_truly_stuck": len(truly_stuck),
        "n_wrong_level": len(wrong),
        "truly_stuck_channels": truly_stuck,
        "wrong_level_channels": wrong,
    }
    if wrong:
        out["summary"]["verdict"] = f"WRONG LEVEL on channels: {wrong}"
    elif truly_stuck:
        out["summary"]["verdict"] = f"STUCK (should have changed but didn't): {truly_stuck}"
    else:
        out["summary"]["verdict"] = "All channels behaved as expected"

    return out


# -----------------------------------------------------------------------------
# analogue_vs_code
# -----------------------------------------------------------------------------

def analyse_analogue_vs_code(captures: dict[str, Capture], params: dict) -> dict:
    """Cross-check analog measurements (DMM readings) recorded in sticky_state
    against expected voltage for the corresponding DAC code.

    params:
      state: the report's sticky_state dict (passed in by run.py)
      full_scale_v: what the full-scale DAC output corresponds to (default 10.0 V
                    for an AD7522 with +/-10V reference, or whatever the
                    service manual says)
      code_max: maximum DAC code (default 4095 for 12-bit)
      measurements_key: which sticky_state key holds the measurements
                        (default 'analog_measurements')
    """
    state = params.get("state", {})
    full_scale_v = float(params.get("full_scale_v", 10.0))
    code_max = int(params.get("code_max", 4095))
    m_key = params.get("measurements_key", "analog_measurements")
    measurements = state.get(m_key, [])

    out: dict[str, Any] = {
        "kind": "analogue_vs_code",
        "full_scale_v": full_scale_v,
        "code_max": code_max,
        "n_measurements": len(measurements),
        "measurements": [],
        "summary": {},
    }

    n_pass = 0
    n_fail = 0
    n_no_expected = 0

    for m in measurements:
        code = m.get("code")
        value = m.get("value_v")
        # Compute expected voltage from code if not given
        expected = m.get("expected_v")
        if expected is None and code is not None and code_max > 0:
            # For a unipolar DAC: v_out = (code / code_max) * full_scale_v
            # For a bipolar DAC: v_out = ((code - code_max/2) / (code_max/2)) * (full_scale_v/2)
            # The Marconi uses a unipolar arrangement with the I-to-V converter
            # after, so we'll treat as unipolar unless told otherwise.
            if params.get("bipolar"):
                expected = ((code - code_max / 2) / (code_max / 2)) * (full_scale_v / 2)
            else:
                expected = (code / code_max) * full_scale_v
            m["expected_v"] = expected

        # Compare
        if value is not None and expected is not None:
            diff = abs(value - expected)
            tol_pct = m.get("tolerance_pct", 2.0)
            within = diff <= abs(expected) * (tol_pct / 100.0) if expected else False
            m["diff_v"] = diff
            m["diff_pct"] = (diff / abs(expected) * 100) if expected else None
            m["within_tolerance"] = within
            if within:
                n_pass += 1
            else:
                n_fail += 1
        else:
            n_no_expected += 1
        out["measurements"].append(m)

    out["summary"] = {
        "n_pass": n_pass,
        "n_fail": n_fail,
        "n_no_expected": n_no_expected,
        "tolerance_pct": 2.0,
    }
    if n_fail == 0 and n_no_expected == 0:
        out["summary"]["verdict"] = f"All {n_pass} measurements within tolerance"
    elif n_fail == 0:
        out["summary"]["verdict"] = f"{n_pass} pass, {n_no_expected} no expected value"
    else:
        out["summary"]["verdict"] = f"{n_fail} FAIL, {n_pass} pass — see measurements"
    return out


# -----------------------------------------------------------------------------
# bus_e2e — end-to-end CPU→AC4 stage-by-stage verification
# -----------------------------------------------------------------------------

# Channel roles for the e2e test.  Matches the harness probe map.
# These channel numbers are the LA channel the operator should clip.
E2E_CHANNELS: dict[str, int] = {
    "ale":      0,  # IC5.30  ALE
    "wr_cpu":   1,  # IC5.31  /WR (from the CPU)
    "clk":      2,  # IC5.37  CLK (3.072 MHz)
    "a0":       3,  # IC21.Q  A0 latched
    "addr_c":   4,  # IC11.18 'C' address line
    "a7l_y":    5,  # AC3/ACL3 IC1.Y?  A7L? (the Y-output we're targeting)
    "lb":       6,  # AC4 IC6.24  LBS  (Low Byte Strobe — wiki verified 2026-06-07)
    "ldac":     7,  # AC4 IC6.22  LDAC (Load DAC — wiki verified 2026-06-07; IC6.21 is SPC, NOT LDAC)
}

# Stage definitions: each stage knows which channel(s) to check, what the
# healthy pattern looks like, and what to do with the result.
#
# stage_label, role, channel_key, healthy_test, fault_description
E2E_STAGES: list[dict] = [
    {
        "stage": 1,
        "name": "CPU (IC5 P8085A)",
        "channel_keys": ["clk", "ale", "wr_cpu"],
        "min_edges": {"clk": 2, "ale": 1, "wr_cpu": 1},
        "checks": [
            ("clk",     "IC5.37 produces a clock pulse (3.072 MHz)"),
            ("ale",     "IC5.30 ALE pulses HIGH (demuxes address)"),
            ("wr_cpu",  "IC5.31 /WR pulses LOW (write cycle)"),
        ],
    },
    {
        "stage": 2,
        "name": "Buffer (IC10 74LS245 + IC11 74LS244)",
        "channel_keys": ["addr_c", "a0"],
        "min_edges": {"addr_c": 2, "a0": 1},
        "checks": [
            ("a0",      "IC21 latched the A0 address bit onto the bus"),
            ("addr_c",  "IC11.18 'C' line toggles cleanly (no shark fin)"),
        ],
    },
    {
        "stage": 3,
        "name": "Decoder (AC3/ACL3 IC1 74LS138)",
        "channel_keys": ["a7l_y"],
        "min_edges": {"a7l_y": 1},
        "checks": [
            ("a7l_y",   "AC3/ACL3 IC1 selected the right Y-output (A7L?)"),
        ],
    },
    {
        "stage": 4,
        "name": "DAC (AC4 IC6 AD7522)",
        "channel_keys": ["lb", "ldac"],
        "min_edges": {"lb": 1, "ldac": 1},
        "checks": [
            ("lb",      "AD7522 LBS strobe pulsed (Pin 24)"),
            ("ldac",    "AD7522 LDAC strobe fired after LBS settled (Pin 22; Pin 21 is SPC, NOT LDAC)"),
        ],
    },
]


def analyse_bus_e2e(captures: dict[str, Capture], params: dict) -> dict:
    """End-to-end CPU→AC4 verification.

    For each stage in the 4-stage pipeline, look up the corresponding
    capture by id, count edges on the stage's required channels, and
    compare to the expected minimum.  A stage passes if every required
    channel has at least `min_edges` transitions.  A channel with no
    transitions at all is flagged as `stuck_low`.

    Optionally cross-references the analog measurement (TP2) recorded in
    sticky_state: if `expected_tp2_v` is provided and the measurement
    is more than 50% off, the test's final verdict becomes `analogue_mismatch`
    even if all four digital stages pass.

    params:
        stage_captures: dict of {stage_label: capture_id}
                        e.g. {"stage1": "cap_cpu", "stage2": "cap_buffer", ...}
        state: report's sticky_state (for TP2 cross-check)
        expected_tp2_v: optional expected TP2 voltage for the
                        specific code sent. If provided, the test
                        cross-checks the measured TP2 voltage.
        tp2_tolerance_pct: tolerance for the TP2 cross-check
                           (default 50% — generous because the
                           operator may have entered the code
                           manually with some drift)
    """
    state = params.get("state", {})
    stage_captures = params.get("stage_captures", {})
    expected_tp2 = params.get("expected_tp2_v", None)
    tp2_tol_pct = float(params.get("tp2_tolerance_pct", 50.0))

    out: dict[str, Any] = {
        "kind": "bus_e2e",
        "stages": {},
        "first_failing_stage": None,
        "summary": {},
    }

    any_fail = False

    for stage_def in E2E_STAGES:
        stage_num = stage_def["stage"]
        cap_id = stage_captures.get(f"stage{stage_num}")
        stage_result: dict[str, Any] = {
            "stage": stage_num,
            "name": stage_def["name"],
            "capture_id": cap_id,
            "checks": [],
            "pass": True,
            "notes": [],
        }

        if cap_id is None:
            stage_result["pass"] = False
            stage_result["notes"].append(f"no capture id provided for stage{stage_num}")
            out["stages"][f"stage{stage_num}"] = stage_result
            any_fail = True
            if out["first_failing_stage"] is None:
                out["first_failing_stage"] = stage_num
            continue

        cap = _resolve_capture(captures, cap_id)
        if cap is None:
            stage_result["pass"] = False
            stage_result["notes"].append(f"capture '{cap_id}' not found in captures dict")
            out["stages"][f"stage{stage_num}"] = stage_result
            any_fail = True
            if out["first_failing_stage"] is None:
                out["first_failing_stage"] = stage_num
            continue

        # Per-channel edge count
        for role, ch_num, desc in [(c[0], E2E_CHANNELS[c[0]], c[1])
                                    for c in stage_def["checks"]]:
            trans = parse_vcd_transitions(cap.vcd_path, ch_num) if cap.vcd_path else []
            n_edges = len(trans)
            min_required = stage_def["min_edges"].get(role, 1)
            check = {
                "role": role,
                "channel": ch_num,
                "description": desc,
                "n_edges": n_edges,
                "min_required": min_required,
                "pass": n_edges >= min_required,
            }
            if not check["pass"]:
                # Diagnose: zero edges is 'stuck', partial is 'degraded'
                if n_edges == 0:
                    check["diagnosis"] = f"stuck_low (no transitions on CH{ch_num})"
                else:
                    check["diagnosis"] = f"degraded (only {n_edges}/{min_required} edges)"
                stage_result["pass"] = False
            stage_result["checks"].append(check)

        # Special diagnostic for the 'C' line in stage 2: detect shark-fin
        if stage_num == 2:
            c_trans = parse_vcd_transitions(cap.vcd_path, E2E_CHANNELS["addr_c"]) if cap.vcd_path else []
            if c_trans:
                # Shark-fin / bus contention produces MANY rapid transitions
                # in a short time window. A clean address-line toggle in
                # a normal bus cycle shows 2 transitions. Contention shows
                # 5+ transitions clustered together (the two drivers
                # fighting each other on the same line).
                if len(c_trans) >= 5:
                    # Compute the span of the transitions (first to last)
                    span_ns = c_trans[-1][0] - c_trans[0][0] if c_trans else 0
                    # Cluster check: contention edges are within 1 µs
                    if span_ns < 1000:
                        stage_result["pass"] = False
                        stage_result["notes"].append(
                            f"CH{E2E_CHANNELS['addr_c']} 'C' line has {len(c_trans)} "
                            f"transitions in {span_ns} ns — possible shark-fin / bus "
                            f"contention (clean expected: ≤2)"
                        )

        # Special diagnostic for the LB strobe in stage 4: too-slow rise
        if stage_num == 4:
            lb_trans = parse_vcd_transitions(cap.vcd_path, E2E_CHANNELS["lb"]) if cap.vcd_path else []
            # A slow rise on a single channel shows up as many sub-edges
            # within a short window (10-100 ns). With a clean 50 ns pulse
            # we get exactly 2 transitions. With a 200-ns rise we get 4+.
            if len(lb_trans) >= 4:
                stage_result["pass"] = False
                stage_result["notes"].append(
                    f"CH{E2E_CHANNELS['lb']} LB has {len(lb_trans)} transitions — "
                    f"possible slow-rise / RC-filtered edge"
                )

        if not stage_result["pass"]:
            any_fail = True
            if out["first_failing_stage"] is None:
                out["first_failing_stage"] = stage_num
        out["stages"][f"stage{stage_num}"] = stage_result

    # Optional analog cross-check on TP2
    analog_result: dict[str, Any] = {"checked": False}
    if expected_tp2 is not None:
        measurements = state.get("analog_measurements", [])
        # The most recent measurement with a measured value (its own
        # expected_v may be None — that's fine, we have our own
        # expected_tp2 from the analyser params)
        relevant = [m for m in measurements if m.get("value_v") is not None]
        if relevant:
            m = relevant[-1]  # most recent
            diff = abs(m["value_v"] - expected_tp2)
            within = diff <= abs(expected_tp2) * (tp2_tol_pct / 100.0) if expected_tp2 else False
            analog_result = {
                "checked": True,
                "measured_v": m["value_v"],
                "expected_v": expected_tp2,
                "diff_v": diff,
                "within_tolerance": within,
                "code": m.get("code"),
                "tolerance_pct": tp2_tol_pct,
            }
            if not within:
                any_fail = True

    n_pass = sum(1 for s in out["stages"].values() if s["pass"])
    n_total = len(out["stages"])

    out["analog"] = analog_result
    out["summary"] = {
        "n_stages": n_total,
        "n_pass": n_pass,
        "n_fail": n_total - n_pass,
        "all_stages_pass": (n_pass == n_total) and not any_fail,
    }
    if out["summary"]["all_stages_pass"]:
        out["summary"]["verdict"] = "ALL 4 STAGES PASS — bus is healthy end-to-end"
    else:
        first = out["first_failing_stage"]
        first_name = out["stages"][f"stage{first}"]["name"] if first is not None else "unknown"
        out["summary"]["verdict"] = (
            f"FAIL at stage {first} ({first_name}) — "
            f"{n_pass}/{n_total} digital stages pass"
        )
        if analog_result.get("checked") and not analog_result.get("within_tolerance"):
            out["summary"]["verdict"] += (
                f"; TP2 analog mismatch ({analog_result['measured_v']}V vs "
                f"expected {analog_result['expected_v']}V)"
            )
    return out


# -----------------------------------------------------------------------------
# bus_census
# -----------------------------------------------------------------------------

def analyse_bus_census(captures: dict[str, Capture], params: dict) -> dict:
    """Per-channel health summary.

    params:
      reference: capture id to compare against (or 'self' to use a quiescent capture)
      expect_quiescent: optional capture id of a known-quiescent state (e.g. idle power-on)
    """
    out: dict[str, Any] = {"kind": "bus_census", "channels": {}}

    # If 'reference' is a known capture id, compare to it. Otherwise compare to
    # the most recent capture (this is the running-bus-census pattern: each
    # capture is diffed against the previous one).
    target_id = params.get("reference", "self")
    expect_quiescent = params.get("expect_quiescent")

    # Guard: a prior capture step may have failed. Don't crash with
    # IndexError on the empty captures dict — return a clear "skipped"
    # verdict so the report JSON is still well-formed.
    if not captures:
        out["skipped"] = True
        out["reason"] = "no capture available — prior capture step failed"
        return out

    if expect_quiescent and expect_quiescent != "self":
        ref_cap = _resolve_capture(captures, expect_quiescent)
        target_cap = list(captures.values())[-1]  # most recent
    elif target_id != "self":
        ref_cap = _resolve_capture(captures, target_id)
        target_cap = list(captures.values())[-1]
    else:
        ref_cap = None
        target_cap = list(captures.values())[-1]

    for ch in target_cap.channels:
        ch_key = f"ch{ch}"
        trans = parse_vcd_transitions(target_cap.vcd_path, ch) if target_cap.vcd_path else []
        # Health heuristics
        n_edges = len(trans)
        n_rising = sum(1 for t, v in trans if v == 1)
        n_falling = n_edges - n_rising
        last_state = trans[-1][1] if trans else 0
        first_state = trans[0][1] if trans else 0
        final_state = last_state

        # Health verdicts
        health = "ok"
        notes = []
        if n_edges == 0:
            # Constant. Could be OK if all channels are quiet (idle) — context-dependent.
            if expect_quiescent is not None and expect_quiescent == target_cap.name:
                health = "ok"  # this is the quiet reference itself
            else:
                health = "constant"
                notes.append(f"no transitions (stuck at {final_state})")
        elif n_edges == 1 and expect_quiescent is not None:
            # Single transition is suspicious for an idle capture
            if expect_quiescent == target_cap.name:
                health = "ok"  # this is the quiet reference itself
            else:
                # In a real bus capture, one edge is low activity but not necessarily wrong.
                # Flag it for review but not as a fault.
                health = "ok"
                notes.append(f"low activity (1 edge in {target_cap.duration_s}s)")

        out["channels"][ch_key] = {
            "channel": ch,
            "n_edges": n_edges,
            "n_rising": n_rising,
            "n_falling": n_falling,
            "first_state": first_state,
            "last_state": final_state,
            "health": health,
            "notes": "; ".join(notes) if notes else "",
        }

    # If we have a reference, diff against it
    if ref_cap is not None:
        for ch in ref_cap.channels:
            ref_trans = parse_vcd_transitions(ref_cap.vcd_path, ch) if ref_cap.vcd_path else []
            tgt_trans = parse_vcd_transitions(target_cap.vcd_path, ch) if target_cap.vcd_path else []
            ref_n = len(ref_trans)
            tgt_n = len(tgt_trans)
            ch_key = f"ch{ch}"
            if ch_key in out["channels"]:
                # Avoid producing literal Infinity (not valid in standard JSON).
                if ref_n == 0 and tgt_n > 0:
                    ratio = "infinite"  # sentinel string for "ref had 0, target had some"
                elif ref_n == 0 and tgt_n == 0:
                    ratio = 1.0
                else:
                    ratio = tgt_n / ref_n
                out["channels"][ch_key]["edge_ratio_vs_ref"] = ratio
                if isinstance(ratio, (int, float)) and ratio < 0.5:
                    out["channels"][ch_key]["health"] = "degraded"
                    out["channels"][ch_key]["notes"] += f" ({ratio:.1%} of reference edge count)"
                elif ratio == "infinite":
                    # Only flag as anomalous if it's a meaningful amount of activity.
                    # A single transition on a strobe line is normal, not contention.
                    if tgt_n >= 3:
                        out["channels"][ch_key]["health"] = "anomalous_activity"
                        out["channels"][ch_key]["notes"] += f" ({tgt_n} edges vs 0 in reference — possible contention source)"
                    # else: don't downgrade healthy strobe behaviour

    return out


# -----------------------------------------------------------------------------
# signal_integrity
# -----------------------------------------------------------------------------

def analyse_signal_integrity(captures: dict, params: dict) -> dict:
    """Per-channel signal-integrity check based on inter-edge interval distribution.

    The bus_census analyser counts edges but doesn't look at the *timing* of
    those edges. A 24 MHz logic analyser samples at 41.67 ns intervals, so
    a line that's oscillating faster than 24 MHz shows up as a stream of
    back-to-back 0/1 transitions with 41 ns gaps — the "sub-sample-period
    gap" aliasing signature. A healthy bus has a smooth distribution of
    inter-edge intervals in the 100-1000 ns range.

    This analyser reports per-channel:
      - total edges, total intervals
      - count and percentage of sub-100ns intervals (the aliasing tell)
      - min/median/max gap in ns
      - health verdict: ok / degraded / suspect

    Verdicts:
      ok        <5% sub-100ns intervals — clean signal
      degraded  5-20% sub-100ns             — worth a scope check
      suspect   >20% sub-100ns              — line is oscillating at >24 MHz,
                                              almost certainly bus contention
                                              or a failed output stage. Probe
                                              with the scope for shark fin.

    params:
      (none currently — the analyser auto-iterates all channels of the
      most recent capture, the same convention as bus_census)
    """
    out: dict[str, Any] = {
        "kind": "signal_integrity",
        "channels": {},
        "summary": {"verdict": "ok", "n_channels": 0, "n_ok": 0,
                    "n_degraded": 0, "n_suspect": 0},
    }

    # Guard: if no capture is available, return a clean skip rather than
    # crashing on list(captures.values())[-1].
    if not captures:
        out["skipped"] = True
        out["reason"] = "no capture available — prior capture step failed"
        return out

    cap = list(captures.values())[-1]
    if not cap.vcd_path:
        return out

    n_sub100_total = 0
    n_intervals_total = 0
    n_ok = n_degraded = n_suspect = 0

    for ch in cap.channels:
        ch_key = f"ch{ch}"
        trans = parse_vcd_transitions(cap.vcd_path, ch)
        if len(trans) < 2:
            out["channels"][ch_key] = {
                "channel": ch,
                "n_edges": len(trans),
                "n_intervals": 0,
                "n_sub100ns": 0,
                "pct_sub100ns": 0.0,
                "min_gap_ns": None,
                "median_gap_ns": None,
                "max_gap_ns": None,
                "health": "ok",
                "notes": "fewer than 2 edges — no intervals to analyse",
            }
            n_ok += 1
            continue

        ts = [t for t, _v in trans]
        gaps = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
        n_intervals = len(gaps)
        n_sub100 = sum(1 for g in gaps if g < 100)
        n_intervals_total += n_intervals
        n_sub100_total += n_sub100
        pct = (n_sub100 / n_intervals) * 100.0 if n_intervals else 0.0
        min_gap = gaps[0]
        max_gap = gaps[-1]
        median_gap = gaps[n_intervals // 2]

        if pct >= 20.0:
            health = "suspect"
            notes = (f"{pct:.1f}% of intervals are sub-100ns — line is "
                     f"oscillating at >24 MHz (LA sample rate). Probe with "
                     f"the scope for shark fin shape.")
            n_suspect += 1
        elif pct >= 5.0:
            health = "degraded"
            notes = (f"{pct:.1f}% of intervals are sub-100ns — mild "
                     f"aliasing. Worth a scope check on this signal.")
            n_degraded += 1
        else:
            health = "ok"
            notes = ""
            n_ok += 1

        out["channels"][ch_key] = {
            "channel": ch,
            "n_edges": len(trans),
            "n_intervals": n_intervals,
            "n_sub100ns": n_sub100,
            "pct_sub100ns": round(pct, 2),
            "min_gap_ns": min_gap,
            "median_gap_ns": median_gap,
            "max_gap_ns": max_gap,
            "health": health,
            "notes": notes,
        }

    n_channels = len(out["channels"])
    if n_suspect > 0:
        verdict = f"SUSPECT — {n_suspect} channel(s) oscillating at >24 MHz (bus contention signature)"
    elif n_degraded > 0:
        verdict = f"DEGRADED — {n_degraded} channel(s) showing mild aliasing"
    else:
        verdict = f"OK — all {n_channels} channel(s) clean"

    out["summary"] = {
        "verdict": verdict,
        "n_channels": n_channels,
        "n_ok": n_ok,
        "n_degraded": n_degraded,
        "n_suspect": n_suspect,
        "pct_sub100ns_overall": round(
            (n_sub100_total / n_intervals_total) * 100.0, 2
        ) if n_intervals_total else 0.0,
    }
    return out

# -----------------------------------------------------------------------------
# contention
# -----------------------------------------------------------------------------

def analyse_contention(captures: dict[str, Capture], params: dict) -> dict:
    """Detect suspicious transitions on a suspect line.

    Heuristic: if the suspect line transitions within 100ns of another channel
    transitioning (and they don't normally move together), flag it.

    params:
      suspect_channel: which channel is the suspect
      cs_channels:     which channels are chip-selects
    """
    out: dict[str, Any] = {"kind": "contention", "events": [], "summary": {}}
    suspect_ch = int(params.get("suspect_channel", 0))
    cs_channels = [int(c) for c in params.get("cs_channels", [])]

    # Guard: a prior capture step may have failed. Don't crash with
    # IndexError on the empty captures dict — return a clear "skipped"
    # verdict so the report JSON is still well-formed.
    if not captures:
        out["skipped"] = True
        out["reason"] = "no capture available — prior capture step failed"
        return out

    cap = list(captures.values())[-1]
    suspect_trans = parse_vcd_transitions(cap.vcd_path, suspect_ch) if cap.vcd_path else []
    cs_trans = {c: parse_vcd_transitions(cap.vcd_path, c) if cap.vcd_path else []
                for c in cs_channels}

    # Build a sorted list of all transitions across all CS channels
    all_cs_events: list[tuple[int, int, int]] = []  # (t, ch, val)
    for c, trans in cs_trans.items():
        for t, v in trans:
            all_cs_events.append((t, c, v))
    all_cs_events.sort()

    # For each suspect transition, find the nearest CS event
    for t, v in suspect_trans:
        # Find CS events within +/- 1000 ns
        nearby = [e for e in all_cs_events if abs(e[0] - t) <= 1000]
        if nearby:
            for cs_t, cs_ch, cs_v in nearby:
                out["events"].append({
                    "suspect_time_ns": t,
                    "suspect_value": v,
                    "nearest_cs_channel": cs_ch,
                    "cs_time_ns": cs_t,
                    "cs_value": cs_v,
                    "delta_ns": cs_t - t,
                })

    # Summarise
    out["summary"] = {
        "n_suspect_transitions": len(suspect_trans),
        "n_cs_transitions": len(all_cs_events),
        "n_correlated_events": len(out["events"]),
    }
    if out["summary"]["n_correlated_events"] > 10:
        out["summary"]["verdict"] = "HIGH contention activity"
    elif out["summary"]["n_correlated_events"] > 0:
        out["summary"]["verdict"] = "Some contention activity — review events"
    else:
        out["summary"]["verdict"] = "No clear contention — line looks clean"
    return out


# -----------------------------------------------------------------------------
# diff
# -----------------------------------------------------------------------------

def analyse_diff(captures: dict[str, Capture], params: dict) -> dict:
    """Sample-by-sample diff of two captures.

    params:
      a: capture id (the 'good' one)
      b: capture id (the 'bad' one)
    """
    out: dict[str, Any] = {"kind": "diff", "first_divergence": None, "per_channel": {}}

    a_id = params.get("a")
    b_id = params.get("b")
    # The test runner timestamps capture filenames, so 'cap_good' becomes 'cap_good_20260604_...'
    # Resolve by prefix match.
    a = _resolve_capture(captures, a_id)
    b = _resolve_capture(captures, b_id)
    if a is None or b is None:
        out["error"] = f"missing capture(s): a={a_id} b={b_id}"
        return out

    for ch in a.channels:
        if ch not in b.channels:
            continue
        a_trans = parse_vcd_transitions(a.vcd_path, ch) if a.vcd_path else []
        b_trans = parse_vcd_transitions(b.vcd_path, ch) if b.vcd_path else []
        # Compare the first 50 transitions
        n = min(50, len(a_trans), len(b_trans))
        first_diff = None
        for i in range(n):
            if a_trans[i] != b_trans[i]:
                first_diff = {
                    "transition_index": i,
                    "a": {"time_ns": a_trans[i][0], "value": a_trans[i][1]},
                    "b": {"time_ns": b_trans[i][0], "value": b_trans[i][1]},
                    "time_delta_ns": b_trans[i][0] - a_trans[i][0],
                }
                break
        # If the count of transitions itself differs, that's a smoking gun
        count_diff = None
        if len(a_trans) != len(b_trans):
            if len(a_trans) == 0:
                ratio = "infinite"
            else:
                ratio = len(b_trans) / len(a_trans)
            count_diff = {
                "n_transitions_a": len(a_trans),
                "n_transitions_b": len(b_trans),
                "ratio": ratio,
            }
        out["per_channel"][f"ch{ch}"] = {
            "channel": ch,
            "n_transitions_a": len(a_trans),
            "n_transitions_b": len(b_trans),
            "first_divergence": first_diff,
            "count_diff": count_diff,
        }
        # Track the earliest first-divergence across all channels,
        # OR the channel with the most dramatic count difference
        if first_diff:
            t = first_diff["a"]["time_ns"]
            cur = out["first_divergence"]
            # Only compare time if both have it
            if cur is None or "time_ns" not in cur or t < cur["time_ns"]:
                out["first_divergence"] = {
                    "channel": ch,
                    "kind": "transition_mismatch",
                    "time_ns": t,
                    "a_value": first_diff["a"]["value"],
                    "b_value": first_diff["b"]["value"],
                }
        elif count_diff and count_diff["ratio"] != "infinite":
            # Activity changed by more than 2x (in either direction)
            # ratio = b/a. We flag if ratio > 2 (b much more active) or
            # ratio < 0.5 (a much more active, i.e. b quieter).
            if count_diff["ratio"] > 2 or count_diff["ratio"] < 0.5:
                if (out["first_divergence"] is None
                        or abs(count_diff["n_transitions_b"] - count_diff["n_transitions_a"])
                        > abs(out["first_divergence"].get("n_transitions_b", 0)
                              - out["first_divergence"].get("n_transitions_a", 0))):
                    out["first_divergence"] = {
                        "channel": ch,
                        "kind": "activity_mismatch",
                        "n_transitions_a": count_diff["n_transitions_a"],
                        "n_transitions_b": count_diff["n_transitions_b"],
                        "ratio": count_diff["ratio"],
                    }
        elif count_diff and count_diff["ratio"] == "infinite" and count_diff["n_transitions_b"] > 0:
            if out["first_divergence"] is None or count_diff["n_transitions_b"] > out["first_divergence"].get("n_transitions_b", 0):
                out["first_divergence"] = {
                    "channel": ch,
                    "kind": "silent_in_good",
                    "n_transitions_a": 0,
                    "n_transitions_b": count_diff["n_transitions_b"],
                }

    return out


# -----------------------------------------------------------------------------
# clock_health
# -----------------------------------------------------------------------------

def analyse_clock_health(captures: dict[str, Capture], params: dict) -> dict:
    """Measure the frequency of a designated clock channel and report whether
    it's within tolerance of an expected value.

    params:
      channel:         which channel (0..7) the clock is on
      expected_hz:     nominal clock frequency in Hz
      tolerance_pct:   acceptable deviation (default 5.0)

    Returns measured_hz, n_rising_edges, n_falling_edges, period_ns,
    and a verdict: 'within tolerance', 'out of tolerance', or 'no edges seen'.
    """
    out: dict[str, Any] = {
        "kind": "clock_health",
        "channel": None,
        "expected_hz": None,
        "measured_hz": None,
        "n_rising_edges": 0,
        "n_falling_edges": 0,
        "period_ns": None,
        "deviation_pct": None,
        "verdict": "no edges seen",
    }

    channel = int(params.get("channel", 0))
    expected_hz = float(params.get("expected_hz", 1_000_000))
    tolerance_pct = float(params.get("tolerance_pct", 5.0))
    out["channel"] = channel
    out["expected_hz"] = expected_hz

    # Guard: a prior capture step may have failed (timeout, no hardware,
    # sigrok crashed). Don't crash with IndexError on the empty captures
    # dict — return a clear "skipped" verdict so the report JSON is still
    # well-formed and the operator can see *why* analysis is missing.
    if not captures:
        out["skipped"] = True
        out["reason"] = "no capture available — prior capture step failed"
        out["verdict"] = "skipped (no capture)"
        return out

    cap = list(captures.values())[-1]
    if not cap.vcd_path:
        return out
    trans = parse_vcd_transitions(cap.vcd_path, channel)
    n_rising = sum(1 for t, v in trans if v == 1)
    n_falling = sum(1 for t, v in trans if v == 0)
    out["n_rising_edges"] = n_rising
    out["n_falling_edges"] = n_falling

    if n_rising < 2 or cap.duration_s <= 0:
        return out

    # Use the time between the first and last rising edge to compute period
    first_rising = next(t for t, v in trans if v == 1)
    last_rising = next(t for t, v in reversed(trans) if v == 1)
    elapsed_ns = last_rising - first_rising
    if elapsed_ns <= 0:
        return out
    period_ns = elapsed_ns / max(1, n_rising - 1)
    out["period_ns"] = period_ns
    measured_hz = 1e9 / period_ns
    out["measured_hz"] = measured_hz

    deviation_pct = abs(measured_hz - expected_hz) / expected_hz * 100.0
    out["deviation_pct"] = deviation_pct
    if deviation_pct <= tolerance_pct:
        out["verdict"] = f"within tolerance ({deviation_pct:.2f}% off, expected {expected_hz:,} Hz)"
    else:
        out["verdict"] = (f"OUT OF TOLERANCE ({deviation_pct:.2f}% off, "
                          f"expected {expected_hz:,} Hz, tolerance {tolerance_pct:.1f}%)")
    return out


# -----------------------------------------------------------------------------
# protocol_decode — bus-protocol decoder (the missing piece)
# -----------------------------------------------------------------------------
# This analyser turns a raw LA capture into a per-event **decoded value
# table** — hex / binary / decimal / signed — for whatever protocol the
# operator is debugging. It's deliberately generic: the operator passes
# a channel layout in `params`, and the analyser reports whatever values
# it sees on those channels at each edge of the strobe/clock.
#
# Why this exists: every other analyser in the harness (bus_census,
# contention, signal_integrity) tells you *something is wrong on this
# line* but not *what value was being sent*. For the level-sweep test
# you specifically asked for "decode what's being requested into binary
# at each step or decode some true value" — this is the analyser that
# does that. Without it, a fault on a single data line is invisible
# unless you manually walk the VCD sample by sample.
#
# Three sample-rate / edge scenarios this handles:
#  1. Synchronous bus, clock on a known channel — sample data on each
#     rising clock edge, decode the bus.
#  2. Asynchronous strobe (e.g. AD7522 LB/HB pulses) — sample data
#     just before the strobe's rising edge, decode the bus.
#  3. Edge-driven events (e.g. 74LS273 latch CLK) — sample data
#     just before the CLK rising edge, decode the bus.
#
# params (passed in via the `analyse` step's `params` dict):
#   mode:                  "clock_edge" | "strobe_rising" | "any_edge"
#                          Default: "clock_edge"
#   clock_channel:         which LA channel carries the clock/strobe (int)
#   data_channels:         {bit_position: channel} — bit 0 = LSB
#                          e.g. {0: 3, 1: 4, 2: 5, 3: 6, 4: 7, 5: 0, 6: 1, 7: 2}
#                          for an 8-bit bus on LA CH4..CH8 + CH1..CH3
#   enable_channel:        (optional) channel that must be LOW (or HIGH) for
#                          the event to be counted. e.g. /CS, /WR, /OE.
#                          Value: "low" means event counted when this is LOW.
#                          Value: "high" means event counted when this is HIGH.
#                          Default: no enable filter (count all clock edges).
#   invert:                {channel: True} for active-low lines (default: {})
#   signed:                bool — interpret MSB as sign (default: False)
#   bit_positions:         (optional) explicit bit→channel map; if absent,
#                          defaults to {0: data_channels[0], 1: data_channels[1], ...}
#                          Use this when channels aren't in LSB-first order.
#   level_markers:         (optional) list of VCD timestamps (in ns) the
#                          operator logged via set_state("level_markers",
#                          [...]) — used to bin decoded values by front-panel
#                          level. The analyser walks level_markers in order
#                          and reports a per-level table.
#   expected:              (optional) per-level expected integer value (in
#                          bus units) for the standard 1 dB sweep pattern.
#                          e.g. for the fine attenuator: 0 at max level,
#                          increasing as level decreases. The analyser
#                          flags mismatches.
#
# Output:
#   {
#     "kind": "protocol_decode",
#     "mode": "clock_edge",
#     "n_events": <int>,        # number of clock/strobe edges captured
#     "events": [
#       {"idx": 0, "t_ns": 12345, "value": 0x55, "binary": "01010101",
#        "decimal": 85, "signed": 85, "hex": "0x55"},
#       ...
#     ],
#     "summary": {
#       "n_unique_values": <int>,
#       "min": <int>, "max": <int>,
#       "monotonic": True|False,
#       "duplicate_count": <int>
#     },
#     "per_level": [           # present only if level_markers were set
#       {"level_idx": 0, "t_start_ns": ..., "t_end_ns": ..., "values": [...],
#        "min": ..., "max": ..., "expected": ..., "match": True|False},
#       ...
#     ]
#   }

def _bits_to_value(channel_states_at_time: dict[int, int],
                   bit_positions: dict[int, int],
                   invert: dict[int, bool]) -> tuple[int, int]:
    """Given a snapshot of {channel: 0|1} and a bit→channel map, return
    (unsigned_value, signed_value) for a small fixed-width bus (≤16 bits).
    """
    unsigned = 0
    for bit_pos, ch in bit_positions.items():
        bit = channel_states_at_time.get(ch, 0)
        if invert.get(ch, False):
            bit ^= 1
        if bit:
            unsigned |= (1 << bit_pos)
    # Signed: for ≤16 bit values, MSB is bit (max(bit_positions))
    max_bit = max(bit_positions.keys()) if bit_positions else 0
    if max_bit > 0 and (unsigned & (1 << max_bit)):
        signed = unsigned - (1 << (max_bit + 1))
    else:
        signed = unsigned
    return unsigned, signed


def _state_at(transitions_by_ch: dict[int, list[tuple[int, int]]],
              t_ns: int) -> dict[int, int]:
    """For each channel, return the value it holds at time t_ns.
    Walks the transition list once per channel; O(N) total per call.
    """
    state = {}
    for ch, trans in transitions_by_ch.items():
        # transitions are sorted by (t_ns, value); find the last entry
        # with t_ns <= t_ns
        v = 0
        for t, val in trans:
            if t <= t_ns:
                v = val
            else:
                break
        state[ch] = v
    return state


def analyse_protocol_decode(captures: dict, params: dict) -> dict:
    """Decode a bus protocol from a LA capture. See module docstring for full
    params documentation.
    """
    out: dict[str, Any] = {"kind": "protocol_decode", "events": []}

    if not captures:
        out["skipped"] = True
        out["reason"] = "no capture available"
        return out

    cap = list(captures.values())[-1]
    if not cap.vcd_path:
        out["skipped"] = True
        out["reason"] = "most recent capture has no VCD"
        return out

    mode = params.get("mode", "clock_edge")
    clock_ch = params.get("clock_channel")
    data_chs = params.get("data_channels", {})  # {bit_pos: channel}
    enable_ch = params.get("enable_channel")
    enable_polarity = params.get("enable_polarity", "low")  # "low" or "high"
    invert = params.get("invert", {})
    signed_flag = params.get("signed", False)
    level_markers = params.get("level_markers", [])  # list of t_ns, sorted
    expected = params.get("expected", [])           # list of ints, per level

    # JSON round-tripping turns int dict keys into str ("0" not 0). The
    # bit-shift math below does 1 << bit_pos and will TypeError on a
    # string. Coerce keys to int here so stored reports survive reload.
    if data_chs:
        data_chs = {int(k): int(v) for k, v in data_chs.items()}
    if invert:
        invert = {int(k): bool(v) for k, v in invert.items()}
    if clock_ch is not None:
        clock_ch = int(clock_ch)
    if enable_ch is not None:
        enable_ch = int(enable_ch)

    out["mode"] = mode
    out["clock_channel"] = clock_ch
    out["data_channels"] = data_chs
    out["enable_channel"] = enable_ch

    if not data_chs:
        out["error"] = "no data_channels specified"
        return out

    # Load all relevant channels
    chs_needed = set(data_chs.values())
    if clock_ch is not None:
        chs_needed.add(clock_ch)
    if enable_ch is not None:
        chs_needed.add(enable_ch)

    trans_by_ch: dict[int, list[tuple[int, int]]] = {}
    for ch in chs_needed:
        trans_by_ch[ch] = parse_vcd_transitions(cap.vcd_path, ch)

    # Determine which edges are "events" to decode
    if mode in ("clock_edge", "strobe_rising"):
        if clock_ch is None:
            out["error"] = f"mode={mode} requires clock_channel"
            return out
        # Use rising edges of the clock channel as the event times
        events = [(t, 1) for t, v in trans_by_ch[clock_ch] if v == 1]
    elif mode == "any_edge":
        # Use every rising edge of every data channel (loose)
        events = []
        for ch in data_chs.values():
            for t, v in trans_by_ch[ch]:
                if v == 1:
                    events.append((t, ch))
        events.sort()
    else:
        out["error"] = f"unknown mode: {mode}"
        return out

    # Filter by enable
    if enable_ch is not None and mode != "any_edge":
        target_val = 0 if enable_polarity == "low" else 1
        en_trans = trans_by_ch[enable_ch]
        filtered = []
        for t, _ in events:
            en_state = 0
            for et, ev in en_trans:
                if et <= t:
                    en_state = ev
                else:
                    break
            if en_state == target_val:
                filtered.append((t, _))
        events = filtered

    # Walk the events and decode the bus at each
    decoded_events = []
    # Where to sample relative to the clock/strobe edge:
    #   "before" (default) — 1 ns before the rising edge. Right for
    #     synchronous buses where the source clocks data out before
    #     asserting the strobe.
    #   "after" — N ns after the rising edge. Right for the Marconi
    #     2019A where the data bus is shared with other address-decoded
    #     writes and doesn't settle until after the strobe rises.
    #   "stable" — sample at the midpoint of the strobe high window.
    #
    # IMPORTANT: for any "clocked-latch" protocol (e.g. AD7522 LBS, 74LS273
    # CLK), the data MUST be sampled BEFORE the rising edge. The latch
    # captures on the rising edge, and the CPU typically releases the
    # data bus 50-100 ns after the strobe rises, which means an "after"
    # sample lands on the bus idle state (pull-ups to logic 1) and
    # reports every byte as 0xFF. If you see "all 0xFF" decoded values
    # on every event, the sample_point is wrong.
    sample_point = params.get("sample_point", "before")  # before/after/stable
    sample_offset_ns = int(params.get("sample_offset_ns", 1))
    if sample_point == "after" and sample_offset_ns >= 20 and "sample_point" in params:
        # Heuristic guard: warn the operator that "after 20+ ns" on a
        # typical CPU bus is almost always wrong. This catches the
        # classic "I read 0xFF on every strobe" bug without forcing a
        # hard error.
        out.setdefault("_warnings", []).append(
            f"sample_point='after' with sample_offset_ns={sample_offset_ns} — "
            "for a clocked latch (AD7522 LBS, 74LS273 CLK), the data is "
            "latched on the rising edge and the CPU releases the bus shortly "
            "after. Sampling >20 ns after the edge often lands on the bus "
            "idle state (all 1s) and reports 0xFF for every event. "
            "Consider sample_point='before' with sample_offset_ns=50."
        )
    for idx, (t, _src) in enumerate(events):
        if sample_point == "before":
            sample_t = max(0, t - sample_offset_ns)
        elif sample_point == "after":
            sample_t = t + sample_offset_ns
        elif sample_point == "stable":
            # Midpoint between this event and the next (or +100 ns for last)
            # Will be set per-event below.
            sample_t = t  # placeholder; overridden in stable loop
            # Find next event of the same kind
            for j in range(idx + 1, len(events)):
                if events[j][1] == _src:
                    sample_t = (t + events[j][0]) // 2
                    break
            else:
                sample_t = t + 100  # 10 ns default for last event
        else:
            sample_t = max(0, t - sample_offset_ns)
        state = _state_at(trans_by_ch, sample_t)
        unsigned, signed_v = _bits_to_value(state, data_chs, invert)
        n_bits = (max(data_chs.keys()) + 1) if data_chs else 0
        decoded_events.append({
            "idx": idx,
            "t_ns": t,
            "value": unsigned,
            "binary": format(unsigned, f"0{n_bits}b") if n_bits else "0",
            "hex": f"0x{unsigned:0{(n_bits + 3) // 4}X}",
            "decimal": unsigned,
            "signed": signed_v,
        })

    out["n_events"] = len(decoded_events)
    out["events"] = decoded_events

    # Summary stats
    if decoded_events:
        values = [e["value"] for e in decoded_events]
        out["summary"] = {
            "n_unique_values": len(set(values)),
            "min": min(values),
            "max": max(values),
            "monotonic": all(values[i] <= values[i + 1] for i in range(len(values) - 1))
                        or all(values[i] >= values[i + 1] for i in range(len(values) - 1)),
            "duplicate_count": len(values) - len(set(values)),
        }
    else:
        out["summary"] = {"n_unique_values": 0, "min": None, "max": None,
                          "monotonic": None, "duplicate_count": 0}

    # Per-level binning (if level_markers supplied)
    if level_markers:
        per_level = []
        # Build level windows: [marker[i], marker[i+1]) — last extends to end
        n_levels = len(level_markers) + 1
        for i in range(n_levels):
            t_start = level_markers[i] if i < len(level_markers) else None
            t_end = level_markers[i + 1] if (i + 1) < len(level_markers) else None
            # Filter decoded_events to this window
            window = []
            for ev in decoded_events:
                if t_start is not None and ev["t_ns"] < t_start:
                    continue
                if t_end is not None and ev["t_ns"] >= t_end:
                    continue
                window.append(ev)
            vals = [w["value"] for w in window]
            entry = {
                "level_idx": i,
                "t_start_ns": t_start,
                "t_end_ns": t_end,
                "n_events": len(window),
                "values": [w["value"] for w in window],
                "binaries": [w["binary"] for w in window],
                "hexes": [w["hex"] for w in window],
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
                "first": window[0]["value"] if window else None,
                "last": window[-1]["value"] if window else None,
            }
            # Match against expected
            if i < len(expected):
                exp = expected[i]
                if vals:
                    entry["expected"] = exp
                    entry["match_first"] = (window[0]["value"] == exp)
                    entry["match_last"] = (window[-1]["value"] == exp)
                    entry["match_min"] = (min(vals) == exp)
                    entry["match_max"] = (max(vals) == exp)
                else:
                    entry["expected"] = exp
                    entry["match_first"] = False
                    entry["match_last"] = False
            per_level.append(entry)
        out["per_level"] = per_level

        # Overall verdict
        if expected and per_level:
            matches = sum(1 for pl in per_level
                          if pl.get("match_first") is True)
            out["verdict"] = (f"{matches}/{len(per_level)} levels matched "
                              f"expected on first decoded value")
        else:
            out["verdict"] = "no expected values provided"

    return out
