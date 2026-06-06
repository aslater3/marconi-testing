"""Entry point for the Marconi 2019A test harness.

Usage:
    python3 run.py                       # interactive menu
    python3 run.py --list                # list available tests
    python3 run.py --test <name>         # run a specific test
    python3 run.py --test <name> --dry-run
    python3 run.py --test <name> --simulate
    python3 run.py --test <name> --simulate-pattern shark_fin
"""
from __future__ import annotations
import argparse
import queue
import sys
import threading
import time
from pathlib import Path

from harness import ui
from harness.tests import list_tests, get_test
from harness.capture import capture as do_capture, _check_hardware
from harness.report import Report
from harness.analysis import (analyse_bus_census, analyse_contention, analyse_diff,
                              analyse_n_way_diff, analyse_analogue_vs_code,
                              analyse_bus_e2e, analyse_clock_health)


HARNESS_DIR = Path(__file__).parent.resolve()
CAPTURES_DIR = HARNESS_DIR / "captures"
REPORTS_DIR = HARNESS_DIR / "reports"


# -----------------------------------------------------------------------------
# Step execution
# -----------------------------------------------------------------------------

def _execute_step(step: dict, step_num: int, total: int, report: Report,
                  captures: dict, mode: str) -> None:
    """Execute one test step. Mutates report and captures in place."""
    step_id = step.get("id", f"step_{step_num}")
    step_type = step.get("type")

    if step_type == "prompt":
        text = step.get("text", "")
        wait_for = step.get("wait_for", "enter")
        if wait_for == "enter":
            ui.prompt_continue(text)
        else:
            ans = ui.prompt_text(text)
            report.add_event(step_id, "prompt", text=text, operator_input=ans)

    elif step_type == "clip":
        text = step.get("text", "Clip the channels:")
        probes = step.get("probes", {})
        ui.step_header(step_num, total, "Clip channels")
        print()
        for ch, label in probes.items():
            print(f"  {ui.BOLD}CH{ch}{ui.RESET}  →  {label}")
        print()
        ui.prompt_continue(text)
        report.add_event(step_id, "clip", channels=list(probes.keys()),
                         probes=probes, text=text)

    elif step_type == "press":
        text = step.get("text", "")
        ui.prompt_continue(text)
        report.add_event(step_id, "press", text=text)

    elif step_type == "capture":
        ui.step_header(step_num, total, "Capture")
        duration_s = float(step.get("duration_s", 2.0))
        sample_rate_hz = int(step.get("sample_rate_hz", 1_000_000))
        channels = list(step.get("channels", list(range(8))))
        trigger = step.get("trigger")

        print(f"  {ui.DIM}duration: {duration_s}s  rate: {sample_rate_hz:,} Hz  "
              f"channels: {channels}  mode: {mode}{ui.RESET}")
        print()
        if mode in ("dry_run", "simulate"):
            ui.info(f"{mode}: skipping live dashboard")
            cap = do_capture(step, CAPTURES_DIR, mode=mode)
            captures[cap.name] = cap
            report.add_capture(cap.to_dict())
            report.add_event(step_id, "capture", capture_id=cap.name, mode=cap.mode,
                             duration_s=cap.duration_s, sample_rate_hz=cap.sample_rate_hz,
                             n_samples=cap.n_samples, vcd_path=str(cap.vcd_path) if cap.vcd_path else None)
            ui.success(f"captured {cap.n_samples:,} samples to {cap.vcd_path or 'stub'}")
            return

        # Probe labels for the live dashboard. Best-effort: pull from any
        # preceding "clip" step's probe map if we have one in the test, else
        # fall back to "D<n>". (We don't currently track prior steps, so we
        # use generic names — the test author can pass them in via a future
        # 'probes' field on the capture step.)
        probes = step.get("probes", {ch: f"D{ch}" for ch in channels})

        # In-memory capture path: stream sigrok's stdout into a LiveBuffer
        # instead of writing to a file during capture. capture.py dumps the
        # buffer to disk in one shot after sigrok exits, so the post-capture
        # analyser still has a file to read. The advantage is no disk I/O
        # competing with the USB bus while the LA is streaming — relevant on
        # macOS where fx2lafw is sensitive to I/O back-pressure.
        live_buffer = ui.LiveBuffer()
        dashboard_result: "queue.Queue[dict]" = queue.Queue()
        dashboard_exc: "queue.Queue[BaseException]" = queue.Queue()

        def _dashboard_worker(_vcd_path_unused, should_stop):
            try:
                result = ui.live_capture_progress(
                    duration_s, probes=probes,
                    should_stop=should_stop,
                    live_buffer=live_buffer,
                )
                dashboard_result.put(result)
            except BaseException as e:  # noqa: BLE001
                dashboard_exc.put(e)

        def _on_progress(vcd_path, should_stop):
            t = threading.Thread(
                target=_dashboard_worker, args=(vcd_path, should_stop),
                daemon=True, name="live-dashboard",
            )
            t.start()

        try:
            cap = do_capture(step, CAPTURES_DIR, mode=mode,
                             on_progress=_on_progress,
                             live_buffer=live_buffer)
            captures[cap.name] = cap
            report.add_capture(cap.to_dict())
            report.add_event(step_id, "capture", capture_id=cap.name, mode=cap.mode,
                             duration_s=cap.duration_s, sample_rate_hz=cap.sample_rate_hz,
                             n_samples=cap.n_samples, vcd_path=str(cap.vcd_path) if cap.vcd_path else None)
            ui.success(f"captured {cap.n_samples:,} samples to {cap.vcd_path or 'stub'}")
            # Surface what the dashboard saw, in case the operator missed it
            try:
                res = dashboard_result.get_nowait()
                edges = sum(res.get("rising", {}).values()) + sum(res.get("falling", {}).values())
                if edges:
                    ui.info(f"live view: {edges} edges observed during capture "
                            f"({dict(res.get('rising', {}))} rising, "
                            f"{dict(res.get('falling', {}))} falling)")
                else:
                    ui.info("live view: no edges observed during capture")
            except queue.Empty:
                pass
            try:
                exc = dashboard_exc.get_nowait()
                ui.warn(f"live dashboard error: {exc!r}")
            except queue.Empty:
                pass
        except Exception as e:
            ui.error(f"capture failed: {e}")
            report.add_event(step_id, "capture_error", error=str(e))

    elif step_type == "analyse":
        ui.step_header(step_num, total, "Analyse")
        kind = step.get("kind")
        params = step.get("params", {})
        result = None
        try:
            if kind == "bus_census":
                result = analyse_bus_census(captures, params)
            elif kind == "contention":
                result = analyse_contention(captures, params)
            elif kind == "diff":
                result = analyse_diff(captures, params)
            elif kind == "n_way_diff":
                result = analyse_n_way_diff(captures, params)
            elif kind == "analogue_vs_code":
                # Inject the report's sticky_state so the analyser can read measurements
                params_with_state = dict(params)
                params_with_state["state"] = report._state
                result = analyse_analogue_vs_code(captures, params_with_state)
            elif kind == "bus_e2e":
                # Inject the report's sticky_state so the analyser can read TP2 measurements
                params_with_state = dict(params)
                params_with_state["state"] = report._state
                result = analyse_bus_e2e(captures, params_with_state)
            elif kind == "clock_health":
                result = analyse_clock_health(captures, params)
            else:
                ui.warn(f"unknown analysis kind: {kind}")
                return
            report.add_event(step_id, "analysis", kind=kind, params=params, result=result)
            ui.success(f"analysis complete: {kind}")
            # Render a one-line-per-channel summary for the dashboard
            if kind == "bus_census":
                if result.get("skipped"):
                    print(f"    {ui.YELLOW}⊘ skipped: {result.get('reason', '?')}{ui.RESET}")
                elif "channels" in result:
                    print()
                    for ch_name, info in result["channels"].items():
                        verdict = info.get("health", "?")
                        color = ui.GREEN if verdict == "ok" else (ui.RED if verdict in ("degraded", "suspicious", "constant") else ui.YELLOW)
                        print(f"    {ch_name}: {color}{verdict}{ui.RESET}  "
                              f"edges={info.get('n_edges', 0)}  "
                              f"rising/falling={info.get('n_rising', 0)}/{info.get('n_falling', 0)}  "
                              f"{ui.DIM}{info.get('notes', '')}{ui.RESET}")
            elif kind == "contention":
                if result.get("skipped"):
                    print(f"    {ui.YELLOW}⊘ skipped: {result.get('reason', '?')}{ui.RESET}")
                else:
                    s = result.get("summary", {})
                    print(f"    verdict: {ui.BOLD}{s.get('verdict', '?')}{ui.RESET}")
                    print(f"    {ui.DIM}{s}{ui.RESET}")
            elif kind == "clock_health":
                v = result.get("verdict", "?")
                mhz = (result.get("measured_hz") or 0) / 1e6
                exp_mhz = (result.get("expected_hz") or 0) / 1e6
                ch = result.get("channel") or 0
                n_r = result.get("n_rising_edges", 0)
                verdict_color = ui.GREEN if "within tolerance" in v else (ui.RED if "OUT" in v else ui.YELLOW)
                print(f"    LA CH{ch+1}: {verdict_color}{v}{ui.RESET}")
                print(f"    {ui.DIM}measured: {mhz:.4f} MHz  expected: {exp_mhz:.4f} MHz  "
                      f"n_rising_edges: {n_r:,}{ui.RESET}")
            elif kind == "diff":
                fd = result.get("first_divergence")
                if fd:
                    if "time_ns" in fd:
                        print(f"    {ui.RED}first divergence:{ui.RESET} channel {fd['channel']} "
                              f"at t={fd['time_ns']}ns  (a={fd['a_value']}, b={fd['b_value']}) "
                              f"[{fd.get('kind', '?')}]")
                    else:
                        print(f"    {ui.RED}first divergence:{ui.RESET} channel {fd['channel']} "
                              f"[{fd.get('kind', '?')}]  "
                              f"a_edges={fd.get('n_transitions_a', '?')}  "
                              f"b_edges={fd.get('n_transitions_b', '?')}")
                else:
                    print(f"    {ui.GREEN}no divergence in first 50 transitions on any channel{ui.RESET}")
            elif kind == "n_way_diff":
                s = result.get("summary", {})
                print(f"    verdict: {ui.BOLD}{s.get('verdict', '?')}{ui.RESET}")
                if s.get("truly_stuck_channels"):
                    print(f"    {ui.RED}truly stuck: {s['truly_stuck_channels']}{ui.RESET}")
                if s.get("wrong_level_channels"):
                    print(f"    {ui.RED}wrong level: {s['wrong_level_channels']}{ui.RESET}")
                # Show per-channel level sequence for channels of interest
                for ch_name, info in result.get("per_channel", {}).items():
                    if info["health"] == "stuck_or_wrong":
                        seq = info.get("final_state_sequence", [])
                        match = info.get("level_match", {})
                        exp_str = ""
                        if match and "mismatches" in match:
                            ms = match["mismatches"]
                            exp_str = f"  mismatches: {ms}"
                        print(f"    {ui.RED}{ch_name}{ui.RESET}: levels={seq}  "
                              f"{ui.DIM}{info.get('notes', '')}{exp_str}{ui.RESET}")
            elif kind == "analogue_vs_code":
                s = result.get("summary", {})
                print(f"    verdict: {ui.BOLD}{s.get('verdict', '?')}{ui.RESET}")
                for m in result.get("measurements", []):
                    within = m.get("within_tolerance")
                    tag = (f"{ui.GREEN}✓{ui.RESET}" if within
                           else (f"{ui.RED}✗{ui.RESET}" if within is False
                                 else "?"))
                    code = m.get("code", "?")
                    val = m.get("value_v", "?")
                    exp = m.get("expected_v", "?")
                    diff = m.get("diff_v")
                    diff_pct = m.get("diff_pct")
                    if diff is not None and diff_pct is not None:
                        diff_str = f"  Δ={diff:+.4f} ({diff_pct:+.2f}%)"
                    else:
                        diff_str = ""
                    print(f"    code={code:>5}  measured={val} V  expected={exp} V  {tag}{diff_str}")
            elif kind == "bus_e2e":
                s = result.get("summary", {})
                print(f"    verdict: {ui.BOLD}{s.get('verdict', '?')}{ui.RESET}")
                print()
                # Stage-by-stage table
                print(f"    {ui.BOLD}Stage  Name                                    Result{ui.RESET}")
                print(f"    {'─'*70}")
                for sname, stage in result.get("stages", {}).items():
                    n_pass = sum(1 for c in stage.get("checks", []) if c.get("pass"))
                    n_total = len(stage.get("checks", []))
                    tag = (f"{ui.GREEN}PASS{ui.RESET}" if stage.get("pass")
                           else f"{ui.RED}FAIL{ui.RESET}")
                    print(f"    {sname:<7s} {stage.get('name', '?'):<40s} {tag}  "
                          f"({n_pass}/{n_total} checks)")
                    # Show failed checks
                    for c in stage.get("checks", []):
                        if not c.get("pass"):
                            diag = c.get("diagnosis", "")
                            print(f"           {ui.RED}✗ CH{c.get('channel')} {c.get('role')}: "
                                  f"{c.get('description')}{ui.RESET}")
                            if diag:
                                print(f"             {ui.DIM}→ {diag}{ui.RESET}")
                    # Show stage notes (e.g. shark-fin detection)
                    for note in stage.get("notes", []):
                        print(f"           {ui.YELLOW}! {note}{ui.RESET}")
                # Analog cross-check
                analog = result.get("analog", {})
                if analog.get("checked"):
                    print()
                    tag = (f"{ui.GREEN}within tolerance{ui.RESET}" if analog.get("within_tolerance")
                           else f"{ui.RED}OUT OF TOLERANCE{ui.RESET}")
                    print(f"    TP2 cross-check (code={analog.get('code')}): "
                          f"measured={analog.get('measured_v')}V  "
                          f"expected={analog.get('expected_v')}V  "
                          f"Δ={analog.get('diff_v'):.4f}V  {tag}")
        except Exception as e:
            ui.error(f"analysis failed: {e}")
            report.add_event(step_id, "analysis_error", kind=kind, error=str(e))

    elif step_type == "note":
        ui.step_header(step_num, total, "Operator note")
        prompt = step.get("prompt", "Record observation:")
        multiline = step.get("multiline", False)
        text = ui.prompt_text(prompt, multiline=multiline)
        report.add_event(step_id, "note", operator_input=text)

    elif step_type == "set_state":
        # Structured measurement: {channel, value, expected, tolerance}
        # Handle BEFORE the key check, since measurement steps don't need a key
        if step.get("measurement"):
            m_def = step["measurement"]
            step_id = step.get("id", "?")
            prompt = step.get("prompt", f"DMM reading for {m_def.get('channel', step_id)}:")
            if mode in ("simulate", "dry_run") and "_default_value" in m_def:
                value = m_def["_default_value"]
            else:
                value = ui.prompt_text(prompt)
            try:
                value_f = float(value)
            except (ValueError, TypeError):
                ui.warn(f"could not parse {value!r} as float; storing as-is")
                value_f = value
            m = report.add_measurement(
                step_id=step_id,
                channel=m_def.get("channel", step_id),
                value_v=value_f,
                expected_v=m_def.get("expected"),
                code=m_def.get("code"),
                tolerance_pct=m_def.get("tolerance_pct", 2.0),
                unit=m_def.get("unit", "V"),
                notes=m_def.get("notes", ""),
            )
            tag = (f"  within={ui.GREEN}✓{ui.RESET}" if m.get("within_tolerance")
                   else (f"  within={ui.RED}✗{ui.RESET}" if m.get("within_tolerance") is False
                         else "  (no expected)"))
            ui.info(f"measurement {m['channel']}: {m['value_v']} {m['unit']} "
                    f"(expected {m.get('expected_v')}, code={m.get('code')}){tag}")
            # Don't pass step_id again (it's already in m)
            m_event = {k: v for k, v in m.items() if k != "step_id"}
            report.add_event(step_id, "measurement", **m_event)
            return

        # Plain key/value (requires 'key' field)
        key = step.get("key")
        if not key:
            ui.warn("set_state step missing 'key'")
            return
        prompt = step.get("prompt", f"Value for {key}:")
        default = step.get("_default_simulate", None)

        if mode in ("simulate", "dry_run") and default is not None:
            value = default
            ui.info(f"[{mode}] set {key} = {value}")
        else:
            value = ui.prompt_text(prompt)
            report.add_event(step_id, "set_state", key=key, value=value)
        report.set_state(key, value)
        report.add_event(step_id, "set_state", key=key, value=value)

    else:
        ui.warn(f"unknown step type: {step_type}")


# -----------------------------------------------------------------------------
# Test runner
# -----------------------------------------------------------------------------

def run_test(test_key: str, mode: str) -> int:
    test = get_test(test_key)
    ui.banner(f"Marconi 2019A Test Harness — {test.name}",
              subtitle=test.description)
    ui.info(f"operator: {__import__('os').environ.get('USER', 'unknown')}")
    ui.info(f"mode: {mode}  hardware detected: {_check_hardware()}")
    print()

    report = Report(test_name=test.name, test_description=test.description,
                    hardware="fx2lafw 24MHz 8ch" if mode == "hardware" else mode)
    captures: dict = {}

    total = len(test.steps)
    for i, step in enumerate(test.steps, 1):
        try:
            _execute_step(step, i, total, report, captures, mode)
        except KeyboardInterrupt:
            ui.warn("\n\nInterrupted by operator (Ctrl-C).")
            break
        except EOFError:
            ui.warn("\n\nEOF on stdin. Exiting.")
            break

    report.finish()
    out = report.write(REPORTS_DIR)
    print()
    ui.success(f"Test complete. Report written to: {out}")
    print()
    return 0


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Marconi 2019A test harness")
    ap.add_argument("--list", action="store_true", help="list available tests")
    ap.add_argument("--test", help="run a specific test by key")
    ap.add_argument("--dry-run", action="store_true", help="walk through prompts without hardware")
    ap.add_argument("--simulate", action="store_true", help="use synthetic captures instead of hardware")
    ap.add_argument("--simulate-pattern", default="clean_strobe",
                    help="synthetic capture pattern (clean_strobe|shark_fin|stuck_high|stuck_low|idle)")
    args = ap.parse_args()

    if args.list:
        tests = list_tests()
        ui.banner("Marconi 2019A Test Harness")
        for t in tests:
            print(f"  {ui.BOLD}{t['key']}{ui.RESET}  ({t['n_steps']} steps)")
            print(f"     {ui.DIM}{t['description']}{ui.RESET}")
        return 0

    if args.test:
        # Optionally override the simulate pattern globally. Don't clobber
        # per-capture patterns that the test def already specifies — the
        # user can still set --simulate-pattern to force one pattern for all
        # captures (e.g. for debugging).
        if args.simulate and args.simulate_pattern != "clean_strobe":
            test = get_test(args.test)
            for s in test.steps:
                if s.get("type") == "capture" and "_simulate_pattern" not in s:
                    s["_simulate_pattern"] = args.simulate_pattern
        mode = "hardware"
        if args.dry_run:
            mode = "dry_run"
        elif args.simulate:
            mode = "simulate"
        return run_test(args.test, mode)

    # Interactive menu
    ui.banner("Marconi 2019A Test Harness")
    ui.info("Mode selection:")
    print(f"  {ui.BOLD}1{ui.RESET}) hardware (real LA, needs sigrok-cli + connected device)")
    print(f"  {ui.BOLD}2{ui.RESET}) dry-run (no hardware, walk through prompts)")
    print(f"  {ui.BOLD}3{ui.RESET}) simulate (synthetic captures, end-to-end test)")
    print()
    mode_choice = ui.prompt_choice("Mode:", ["hardware", "dry-run", "simulate"])
    mode = ["hardware", "dry_run", "simulate"][mode_choice]

    tests = list_tests()
    key = ui.list_tests_menu(tests)
    if not key:
        return 0

    return run_test(key, mode)


if __name__ == "__main__":
    sys.exit(main())
