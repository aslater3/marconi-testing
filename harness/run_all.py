"""Run every applicable analyser against every capture in the captures/ dir.

The harness's run.py walks the operator through prompts and runs a
specific subset of analysers per test. This script is the
"land-all-the-planes" version: dump every capture through every
analyser (where it makes sense) and produce a unified per-capture
summary table.

Usage:
    python3 -m harness.run_all [--out reports/run_all_<timestamp>.json]
                               [--captures-dir captures]
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
from harness.capture import Capture, parse_vcd_transitions, clear_vcd_cache


def _make_capture_from_vcd(vcd_path: Path) -> Capture | None:
    """Wrap a VCD in a Capture. We don't have the original metadata,
    so we synthesise reasonable defaults (24 MHz, 8 channels, generic
    duration inferred from the VCD)."""
    if not vcd_path.exists():
        return None
    # Infer a duration from the last timestamp in the VCD
    duration_s = 1.0
    try:
        # Quick scan for the last #<ts> line
        last_t = 0
        with open(vcd_path, "rb") as f:
            # Read last 64 KB to find the last timestamp (fast)
            f.seek(-min(65536, vcd_path.stat().st_size), 2)
            for line in f:
                line = line.strip()
                if line.startswith(b"#"):
                    try:
                        last_t = int(line[1:])
                    except ValueError:
                        pass
        # sigrok captures use 100 ps timestamps; harness uses 1 ns
        if last_t > 1_000_000_000:  # 100 ps scale
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


def _safe(fn, *args, **kwargs) -> dict:
    """Run an analyser, catching exceptions and returning them as
    an error result so the report JSON is well-formed."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return {"kind": fn.__name__, "error": f"{type(e).__name__}: {e}"}


def analyse_one_capture(cap: Capture) -> dict:
    """Run every analyser that's relevant for a single capture."""
    caps = {cap.name: cap}
    out = {
        "capture": cap.name,
        "vcd_path": str(cap.vcd_path),
        "duration_s": cap.duration_s,
        "n_samples": cap.n_samples,
        "analysers": {},
    }

    # 1. bus_census — always applicable
    out["analysers"]["bus_census"] = _safe(analyse_bus_census, caps, {})

    # 2. signal_integrity — always applicable
    out["analysers"]["signal_integrity"] = _safe(analyse_signal_integrity, caps, {})

    # 3. protocol_decode (DAC sweep pattern) — try the standard
    # level_sweep_dac params. If the capture has fewer than 5 channels
    # of data, the analyser will return 'no data_channels' and we skip.
    out["analysers"]["protocol_decode_dac_sweep"] = _safe(
        analyse_protocol_decode, caps, {
            "mode": "clock_edge",
            "clock_channel": 0,  # LBS
            "data_channels": {0: 3, 1: 4, 2: 5, 3: 6, 4: 7},  # DB0..DB4
            "sample_point": "before",
            "sample_offset_ns": 50,
            "signed": False,
        })

    # 4. protocol_decode (74LS273 latched) — try the latched-Q pattern
    out["analysers"]["protocol_decode_74ls273"] = _safe(
        analyse_protocol_decode, caps, {
            "mode": "clock_edge",
            "clock_channel": 0,  # CLK
            "data_channels": {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7},  # Q0..Q5
            "enable_channel": 1,  # /CLR
            "enable_polarity": "high",
            "signed": False,
        })

    # 5. protocol_decode (74LS138 /Y outputs) — walking /Y0.. /Y7
    # /Y0.. /Y5 on CH0..CH5, no enable
    out["analysers"]["protocol_decode_74ls138"] = _safe(
        analyse_protocol_decode, caps, {
            "mode": "any_edge",  # 138 fires edges on Y-outputs
            "data_channels": {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            "signed": False,
        })

    # 6. clock_health — measure CH0 as the candidate clock
    out["analysers"]["clock_health_ch0"] = _safe(
        analyse_clock_health, caps, {
            "channel": 0,
            "expected_hz": 3_072_000.0,  # the 8085 reference
            "tolerance_pct": 5.0,
        })

    # 7. contention — pick CH4 as the suspect (the 'C' line in our
    # bus_e2e mapping), CS lines on CH5 and CH6
    out["analysers"]["contention_c_vs_a7l2"] = _safe(
        analyse_contention, caps, {
            "suspect_channel": 4,  # 'C' address line
            "cs_channels": [5, 6],  # A7L? outputs
        })

    return out


def analyse_two_captures_diffs(captures: dict[str, Capture]) -> list[dict]:
    """For each pair of DAC-sweep captures, run the diff analyser
    to surface any per-channel differences."""
    results = []
    names = sorted(captures.keys())
    for i, a_name in enumerate(names):
        for b_name in names[i + 1:]:
            # Only diff captures that look like DAC sweeps (have at least
            # 8 channels and > 1000 transitions on CH0)
            a_trans = parse_vcd_transitions(captures[a_name].vcd_path, 0)
            b_trans = parse_vcd_transitions(captures[b_name].vcd_path, 0)
            if len(a_trans) < 10 and len(b_trans) < 10:
                continue  # tiny stub captures — skip
            res = _safe(analyse_diff, {a_name: captures[a_name],
                                        b_name: captures[b_name]},
                        {"a": a_name, "b": b_name})
            if res.get("first_divergence") is not None:
                results.append({
                    "a": a_name, "b": b_name,
                    "first_divergence": res["first_divergence"],
                })
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "captures",
                    help="where the .vcd.gz files live (default: ../captures)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output report path (default: reports/run_all_<ts>.json)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N captures (for smoke testing)")
    args = ap.parse_args()

    if not args.captures_dir.exists():
        print(f"captures dir {args.captures_dir} does not exist", file=sys.stderr)
        return 1

    vcd_files = sorted(args.captures_dir.glob("*.vcd.gz"))
    if args.limit:
        vcd_files = vcd_files[:args.limit]
    print(f"analysing {len(vcd_files)} capture(s) from {args.captures_dir}…")

    # Build a captures dict once for diff-pair work
    captures: dict[str, Capture] = {}
    per_capture: list[dict] = []
    for vcd in vcd_files:
        cap = _make_capture_from_vcd(vcd)
        if cap is None:
            print(f"  SKIP {vcd.name} (could not load)")
            continue
        captures[cap.name] = cap
        result = analyse_one_capture(cap)
        per_capture.append(result)
        # Quick console summary
        sig = result["analysers"].get("signal_integrity", {})
        sm = sig.get("summary", {}) if isinstance(sig, dict) else {}
        verdict = sm.get("verdict", "?") if isinstance(sm, dict) else "?"
        print(f"  {vcd.name}: {vcd.stat().st_size:>8d} bytes  "
              f"d={result['duration_s']:>6.3f}s  signal_integrity={verdict[:50]}")

    # Pair-wise diffs of DAC sweeps
    print(f"\nrunning pairwise diff on {len(captures)} captures…")
    diffs = analyse_two_captures_diffs(captures)
    print(f"  {len(diffs)} pair(s) with first divergence")

    # Write the report
    out_path = args.out or (
        Path(__file__).resolve().parent.parent / "reports"
        / f"run_all_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "schema_version": 2,
        "kind": "run_all_analysers",
        "created_at": _dt.datetime.now().isoformat(),
        "n_captures": len(per_capture),
        "per_capture": per_capture,
        "pairwise_diffs": diffs,
    }, indent=2, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
