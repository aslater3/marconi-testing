"""Re-run all analyses from existing report JSONs against the existing VCDs.

The harness's run.py always re-executes the test (prompts, captures, analyses).
This script is for the case where the *analyser code* changed but the *captures*
didn't — re-run just the analyse steps with the current code, comparing the
result to the stored one.

Usage:
    python3 -m harness.reanalyse <report.json> [<report.json> ...]
    python3 -m harness.reanalyse --all       # re-analyse every reports/*.json

Output:
    For each report, writes reports/<name>_reanalyzed.json with the full
    reconstructed report (same captures + events, but every analysis event
    replaced with the fresh run). Also prints a side-by-side diff summary
    to stdout showing which verdict fields changed.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from harness.analysis import (
    analyse_bus_census, analyse_contention, analyse_diff,
    analyse_n_way_diff, analyse_analogue_vs_code,
    analyse_bus_e2e, analyse_clock_health,
    analyse_signal_integrity, analyse_protocol_decode,
)
from harness.capture import Capture

ANALYSERS = {
    "bus_census":      analyse_bus_census,
    "contention":      analyse_contention,
    "diff":            analyse_diff,
    "n_way_diff":      analyse_n_way_diff,
    "analogue_vs_code": analyse_analogue_vs_code,
    "bus_e2e":         analyse_bus_e2e,
    "clock_health":    analyse_clock_health,
    "signal_integrity": analyse_signal_integrity,
    "protocol_decode": analyse_protocol_decode,
}


class FakeReport:
    """Minimal Report-shaped object so bus_e2e and analogue_vs_code can
    read sticky_state the same way run.py injects it. The harness passes
    `state` into these two analysers via params; we replicate that.
    """
    def __init__(self, state: dict):
        self._state = state


def _resolve_vcd(vcd_path_str: str, captures_dir: Path) -> Path | None:
    """The vcd_path stored in old reports is the absolute path on the
    machine that produced them (a Mac, /Users/andy/...). We resolve by
    basename against the current captures/ directory.
    """
    if not vcd_path_str:
        return None
    p = Path(vcd_path_str)
    if p.exists():
        return p
    # Fallback: look in captures/ by basename
    basename = p.name
    # Try .vcd.gz, .vcd
    for ext in ("", ".gz", ".vcd", ".vcd.gz"):
        candidate = captures_dir / (basename + ext)
        if candidate.exists():
            return candidate
        # Also try with the original extension replaced
        stem = Path(basename).stem  # strips .vcd
        for test in (captures_dir / (stem + ".vcd.gz"),
                     captures_dir / (stem + ".vcd"),
                     captures_dir / basename):
            if test.exists():
                return test
    return None


def _build_captures(report: dict, captures_dir: Path) -> tuple[dict, list[str]]:
    """Recreate a captures dict {name: Capture} from a report, resolving
    the vcd paths against the current captures/ directory.
    """
    out: dict[str, Capture] = {}
    missing: list[str] = []
    for name, cap in report.get("captures", {}).items():
        vcd_path_str = cap.get("vcd_path", "")
        vcd_path = _resolve_vcd(vcd_path_str, captures_dir)
        if vcd_path is None:
            missing.append(name)
            continue
        # Build a Capture with the minimum fields the analysers touch.
        out[name] = Capture(
            name=name,
            sample_rate_hz=cap.get("sample_rate_hz", 24_000_000),
            n_samples=cap.get("n_samples", 0),
            duration_s=cap.get("duration_s", 0.0),
            channels=cap.get("channels", list(range(8))),
            trigger=cap.get("trigger"),
            raw_path=Path(cap["raw_path"]) if cap.get("raw_path") else Path("/dev/null"),
            vcd_path=vcd_path,
            captured_at=cap.get("captured_at", ""),
            mode=cap.get("mode", "hardware"),
            notes=cap.get("notes", ""),
        )
    return out, missing


def _build_state(report: dict) -> dict:
    """Reconstruct sticky_state from the report's measurement events, the
    way run.py does. The harness keeps measurements in
    report._state['analog_measurements']; we re-derive from the events list
    so the analyser sees the same measurements it saw originally.
    """
    state: dict = {}
    measurements = []
    for ev in report.get("events", []):
        if ev.get("type") == "measurement":
            measurements.append({
                "channel": ev.get("channel"),
                "value_v": ev.get("value_v"),
                "expected_v": ev.get("expected_v"),
                "code": ev.get("code"),
                "tolerance_pct": ev.get("tolerance_pct", 2.0),
                "unit": ev.get("unit", "V"),
            })
    if measurements:
        state["analog_measurements"] = measurements
    # Also pick up any set_state events
    for ev in report.get("events", []):
        if ev.get("type") == "set_state" and "key" in ev and "value" in ev:
            state[ev["key"]] = ev["value"]
    return state


def _diff_summary(old_result: dict, new_result: dict, kind: str = "") -> list[str]:
    """Compare two analyser result dicts and return a list of human-readable
    diff lines. Only top-level fields that are commonly 'verdict-like' are
    checked — deep diffs would be noisy.
    """
    diffs: list[str] = []
    keys_to_check = ("verdict", "n_stages", "n_pass", "n_fail",
                     "n_events", "n_unique_values", "min", "max",
                     "monotonic", "duplicate_count",
                     "all_stages_pass", "first_failing_stage",
                     "n_ok", "n_degraded", "n_suspect",
                     "pct_sub100ns_overall",
                     "n_truly_stuck", "n_wrong_level",
                     "truly_stuck_channels", "wrong_level_channels",
                     "n_pass", "n_fail", "n_no_expected")
    for k in keys_to_check:
        if k in old_result or k in new_result:
            ov = old_result.get(k)
            nv = new_result.get(k)
            if ov != nv:
                diffs.append(f"  {k}: {ov!r} -> {nv!r}")
    # Also check summary subdict
    if "summary" in old_result or "summary" in new_result:
        os = old_result.get("summary", {}) or {}
        ns = new_result.get("summary", {}) or {}
        for k in ("verdict", "n_channels", "n_ok", "n_degraded", "n_suspect",
                 "pct_sub100ns_overall"):
            ov = os.get(k)
            nv = ns.get(k)
            if ov != nv:
                diffs.append(f"  summary.{k}: {ov!r} -> {nv!r}")
    # For protocol_decode, the per-event values matter too.
    if kind == "protocol_decode" or old_result.get("kind") == "protocol_decode":
        oe = old_result.get("events", [])
        ne = new_result.get("events", [])
        if len(oe) != len(ne):
            diffs.append(f"  events length: {len(oe)} -> {len(ne)}")
        else:
            changed = []
            for i, (a, b) in enumerate(zip(oe, ne)):
                if a.get("hex") != b.get("hex") or a.get("decimal") != b.get("decimal"):
                    changed.append((i, a, b))
            if changed:
                diffs.append(f"  decoded value changes: {len(changed)} event(s)")
                for i, a, b in changed[:5]:
                    diffs.append(f"    evt[{i}]: {a.get('hex')} (t={a.get('t_ns')}ns) "
                                 f"-> {b.get('hex')} (t={b.get('t_ns')}ns)")
                if len(changed) > 5:
                    diffs.append(f"    ... and {len(changed) - 5} more")
    return diffs


def _reanalyse_one(report_path: Path, captures_dir: Path) -> int:
    """Re-run all analyses in one report. Returns 0 on success, 1 on error."""
    report = json.loads(report_path.read_text())
    captures, missing = _build_captures(report, captures_dir)
    if missing:
        print(f"  WARN: {len(missing)} capture(s) could not be resolved:")
        for m in missing[:5]:
            print(f"    - {m}")
        if len(missing) > 5:
            print(f"    ... and {len(missing) - 5} more")
    if not captures:
        print(f"  SKIP: no resolvable captures in {report_path.name}")
        return 1

    state = _build_state(report)
    fake_report = FakeReport(state)

    # Build index of original analysis results: {(step_id, kind): old_result}
    old_results: dict[tuple, dict] = {}
    for ev in report.get("events", []):
        if ev.get("type") == "analysis":
            key = (ev.get("step_id"), ev.get("kind"))
            old_results[key] = ev.get("result", {})

    # Walk events, re-run analysis events, build the new report.
    # IMPORTANT: pull the analyser params from the CURRENT test def, not
    # from the stored report. The whole point of re-analysis is to see
    # what changes when the *test code* is fixed; pulling stale params
    # out of the stored report would just reproduce the old (buggy) result.
    # For tests not in the current registry (renamed/removed), fall back
    # to the stored params so we still produce something.
    from harness.tests import REGISTRY
    current_test = REGISTRY.get(report.get("test", ""))
    current_ana_by_id: dict[str, dict] = {}
    if current_test is not None:
        for step in current_test.steps:
            if step.get("type") == "analyse":
                current_ana_by_id[step.get("id", "")] = step.get("params", {}) or {}

    new_events = []
    diff_count = 0
    for ev in report.get("events", []):
        if ev.get("type") != "analysis":
            new_events.append(ev)
            continue
        kind = ev.get("kind")
        step_id = ev.get("step_id", "")
        analyser = ANALYSERS.get(kind)
        if analyser is None:
            new_events.append(ev)
            continue
        # Prefer current test def's params, fall back to stored params.
        params = current_ana_by_id.get(step_id)
        if params is None:
            params = ev.get("params", {}) or {}
        # Inject state for the two that need it
        if kind in ("analogue_vs_code", "bus_e2e"):
            params = dict(params)
            params["state"] = fake_report._state
        try:
            new_result = analyser(captures, params)
        except Exception as e:
            print(f"  ERROR in {ev.get('step_id')}: {e!r}")
            new_events.append(ev)
            continue
        # Compare to old
        old_result = old_results.get((ev.get("step_id"), kind), {})
        diffs = _diff_summary(old_result, new_result, kind=kind)
        if diffs:
            diff_count += 1
            print(f"\n  [{ev.get('step_id')}] {kind} — CHANGED:")
            for d in diffs:
                print(d)
        # Build the new event
        new_ev = dict(ev)
        new_ev["result"] = new_result
        new_events.append(new_ev)

    # Write the new report
    new_report = dict(report)
    new_report["events"] = new_events
    new_report["_reanalyzed"] = True
    new_report["_reanalyzed_at"] = __import__("datetime").datetime.now().isoformat()
    out_path = report_path.with_name(report_path.stem + "_reanalyzed.json")
    out_path.write_text(json.dumps(new_report, indent=2, default=str))
    print(f"  wrote {out_path.name}  ({diff_count} analysis event(s) changed verdict)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reports", nargs="*", type=Path, help="report JSONs to re-analyse")
    ap.add_argument("--all", action="store_true",
                    help="re-analyse every reports/*.json")
    ap.add_argument("--captures-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "captures",
                    help="where to find the .vcd.gz files (default: ../captures)")
    args = ap.parse_args()

    if args.all:
        targets = sorted((Path(__file__).resolve().parent.parent / "reports").glob("*.json"))
        # Skip already-reanalyzed files
        targets = [t for t in targets if "_reanalyzed" not in t.name]
    else:
        targets = args.reports
    if not targets:
        print("no report files to process", file=sys.stderr)
        return 1

    print(f"re-analysing {len(targets)} report(s) against captures in {args.captures_dir}")
    n_fail = 0
    for r in targets:
        print(f"\n=== {r.name} ===")
        try:
            n_fail += _reanalyse_one(r, args.captures_dir)
        except Exception as e:
            print(f"  FATAL: {e!r}")
            n_fail += 1
    print(f"\ndone. {len(targets) - n_fail}/{len(targets)} succeeded.")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
