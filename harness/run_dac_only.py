"""Focused analysis: run every applicable analyser against the REAL DAC
sweep captures only.

The captures/ dir has 46 files. Most are 200-byte synthetic test
stubs from the early sessions and a few are 10-20 MB ic21/ls138
captures from the AA2/1 sessions. The real DAC sweep captures are
the ~20 KB ones from the AC4 level-sweep tests — those are what we
care about for the LSB/HSB bus-fault analysis.

Usage:
    python3 -m harness.run_dac_only [--out reports/dac_only_<ts>.json]
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from harness.analysis import (
    analyse_bus_census, analyse_contention, analyse_diff,
    analyse_n_way_diff, analyse_analogue_vs_code,
    analyse_bus_e2e, analyse_clock_health,
    analyse_signal_integrity, analyse_protocol_decode,
)
from harness.capture import Capture, parse_vcd_transitions


def _make_capture(vcd_path):
    last_t = 0
    try:
        with open(vcd_path, "rb") as f:
            f.seek(-min(65536, vcd_path.stat().st_size), 2)
            for line in f:
                line = line.strip()
                if line.startswith(b"#"):
                    try:
                        last_t = int(line[1:])
                    except ValueError:
                        pass
        if last_t > 1_000_000_000:
            duration_s = last_t * 0.1 / 1e9
        else:
            duration_s = last_t / 1e9
        if duration_s < 0.001:
            duration_s = 1.0
    except Exception:
        duration_s = 1.0
    return Capture(
        name=vcd_path.stem.replace(".vcd", ""),
        sample_rate_hz=24_000_000,
        n_samples=int(duration_s * 24_000_000),
        duration_s=duration_s,
        channels=list(range(8)),
        trigger=None,
        raw_path=vcd_path.with_suffix(".raw") if vcd_path.with_suffix(".raw").exists() else Path("/dev/null"),
        vcd_path=vcd_path,
        captured_at="unknown",
        mode="hardware",
        notes=f"re-analysed from {vcd_path.name}",
    )


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return {"kind": fn.__name__, "error": f"{type(e).__name__}: {e}"}


def find_dac_captures(captures_dir):
    """Real DAC sweeps are 5-500 KB. Stubs are 200B, ic21/ls138 are >1MB."""
    return sorted([p for p in captures_dir.glob("*.vcd.gz")
                   if 5_000 < p.stat().st_size < 500_000])


def analyse_dac_capture(cap):
    """Channel map per tests.py::LEVEL_SWEEP_DAC:
      CH0 = AD7522.Pin24  LBS  (low byte strobe)
      CH1 = AD7522.Pin25  HBS  (high byte strobe)
      CH2 = AD7522.Pin22  LDAC (load to DAC register)
      CH3-7 = AD7522.Pin19..15  DB0..DB4 (LSB to MSB of probed slice)
    """
    caps = {cap.name: cap}
    out = {
        "capture": cap.name,
        "vcd_path": str(cap.vcd_path),
        "vcd_size_bytes": cap.vcd_path.stat().st_size,
        "duration_s": cap.duration_s,
        "n_samples": cap.n_samples,
    }

    ch_edges = {}
    for ch in range(8):
        trans = parse_vcd_transitions(cap.vcd_path, ch)
        n_rising = sum(1 for t, v in trans if v == 1)
        ch_edges[ch] = {
            "n_edges": len(trans),
            "n_rising": n_rising,
            "n_falling": len(trans) - n_rising,
            "first_state": trans[0][1] if trans else None,
            "last_state": trans[-1][1] if trans else None,
        }
    out["channel_activity"] = ch_edges

    out["bus_census"] = _safe(analyse_bus_census, caps, {})
    out["signal_integrity"] = _safe(analyse_signal_integrity, caps, {})

    # The canonical level_sweep_dac protocol_decode params (with our fix)
    pd_result = _safe(analyse_protocol_decode, caps, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 3, 1: 4, 2: 5, 3: 6, 4: 7},
        "sample_point": "before",
        "sample_offset_ns": 50,
        "signed": False,
    })
    out["protocol_decode"] = pd_result

    # Also try sampling AFTER to show the contrast (proves the fix is in effect)
    pd_after = _safe(analyse_protocol_decode, caps, {
        "mode": "clock_edge",
        "clock_channel": 0,
        "data_channels": {0: 3, 1: 4, 2: 5, 3: 6, 4: 7},
        "sample_point": "after",
        "sample_offset_ns": 100,
        "signed": False,
    })
    out["protocol_decode_after_old_buggy"] = pd_after

    out["clock_health_lbs"] = _safe(analyse_clock_health, caps, {
        "channel": 0, "expected_hz": 1.0, "tolerance_pct": 50.0,
    })
    out["clock_health_hbs"] = _safe(analyse_clock_health, caps, {
        "channel": 1, "expected_hz": 1.0, "tolerance_pct": 50.0,
    })

    return out


def analyse_pair_diff(cap_a, cap_b):
    return _safe(analyse_diff,
                 {cap_a.name: cap_a, cap_b.name: cap_b},
                 {"a": cap_a.name, "b": cap_b.name})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "captures")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    dac_files = find_dac_captures(args.captures_dir)
    print(f"found {len(dac_files)} DAC capture(s) (5-500 KB)")
    for p in dac_files:
        print(f"  {p.name}: {p.stat().st_size:>7d} bytes")

    per_capture = []
    captures = {}
    for vcd in dac_files:
        cap = _make_capture(vcd)
        captures[cap.name] = cap
        result = analyse_dac_capture(cap)
        per_capture.append(result)
        # Quick console summary
        ch = result["channel_activity"]
        print(f"\n  {cap.name}:")
        for c in range(8):
            print(f"    CH{c}: {ch[c]['n_edges']:>5} edges  "
                  f"({ch[c]['n_rising']:>4} rise / {ch[c]['n_falling']:>4} fall)  "
                  f"first={ch[c]['first_state']} last={ch[c]['last_state']}")
        si = result["signal_integrity"]
        if "summary" in si:
            print(f"    signal integrity: {si['summary'].get('verdict', '?')}")
        pd = result["protocol_decode"]
        if "summary" in pd:
            s = pd["summary"]
            print(f"    protocol_decode: n_events={pd.get('n_events')} "
                  f"unique={s.get('n_unique_values')} "
                  f"min={s.get('min')} max={s.get('max')} "
                  f"dupes={s.get('duplicate_count')}")
        pd_old = result["protocol_decode_after_old_buggy"]
        if "summary" in pd_old:
            s2 = pd_old["summary"]
            print(f"    protocol_decode (OLD after+100): n_events={pd_old.get('n_events')} "
                  f"unique={s2.get('n_unique_values')} "
                  f"min={s2.get('min')} max={s2.get('max')} "
                  f"dupes={s2.get('duplicate_count')}")

    # Pairwise diff of all DAC captures
    pairwise = []
    names = sorted(captures.keys())
    print(f"\npairwise diffs ({len(names)} captures → {len(names)*(len(names)-1)//2} pairs):")
    for i, a_name in enumerate(names):
        for b_name in names[i + 1:]:
            res = analyse_pair_diff(captures[a_name], captures[b_name])
            fd = res.get("first_divergence")
            if fd:
                pairwise.append({"a": a_name, "b": b_name,
                                 "kind": fd.get("kind"),
                                 "channel": fd.get("channel"),
                                 "time_ns": fd.get("time_ns")})
                print(f"  {a_name} vs {b_name}: {fd.get('kind')} on ch{fd.get('channel')}")
            else:
                print(f"  {a_name} vs {b_name}: identical")

    out_path = args.out or (
        Path(__file__).resolve().parent.parent / "reports"
        / f"dac_only_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "schema_version": 2,
        "kind": "dac_only_run",
        "created_at": _dt.datetime.now().isoformat(),
        "n_captures": len(per_capture),
        "per_capture": per_capture,
        "pairwise_diffs": pairwise,
    }, indent=2, default=str))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
