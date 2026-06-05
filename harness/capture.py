"""sigrok capture wrapper.

Three modes:
  - hardware:    runs sigrok-cli against the connected LA
  - dry_run:     returns a stub capture (no sigrok call) — for walking through prompts
  - simulate:    synthesises a plausible capture for end-to-end testing

All modes return a Capture dataclass with the raw bytes + metadata, so the rest
of the harness doesn't care which mode produced it.
"""
from __future__ import annotations
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class Capture:
    name: str
    sample_rate_hz: int
    n_samples: int
    duration_s: float
    channels: list[int]
    trigger: Optional[dict]
    raw_path: Path
    vcd_path: Optional[Path]
    captured_at: str
    mode: str  # 'hardware' | 'dry_run' | 'simulate'
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["raw_path"] = str(self.raw_path)
        if self.vcd_path:
            d["vcd_path"] = str(self.vcd_path)
        return d


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _check_sigrok() -> Optional[str]:
    """Return path to sigrok-cli, or None if missing."""
    return shutil.which("sigrok-cli")


def _check_hardware() -> bool:
    """Return True if a sigrok-compatible LA is connected."""
    sr = _check_sigrok()
    if not sr:
        return False
    try:
        out = subprocess.run([sr, "-L"], capture_output=True, text=True, timeout=5)
        # The output lists drivers. Look for fx2lafw (24 MHz 8ch clones) or any driver with
        # a connected device.
        for line in out.stdout.splitlines():
            if "fx2lafw" in line.lower() or "saleae" in line.lower():
                if "with" in line.lower() or "connected" in line.lower():
                    return True
        # Also try detecting any device
        out2 = subprocess.run([sr, "--scan"], capture_output=True, text=True, timeout=5)
        return bool(out2.stdout.strip())
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Synthetic capture (for simulate mode and tests)
# -----------------------------------------------------------------------------

def _vcd_header(channels: list[int], timescale: str = "1 ns") -> str:
    """Build a minimal VCD header."""
    lines = ["$date", _timestamp(), "$end",
             "$version hermes-sigrok-bridge 0.1 $end",
             f"$timescale {timescale} $end",
             "$scope module logic $end"]
    for ch in channels:
        lines.append(f"$var wire 1 {chr(ord('a') + ch)} ch{ch} $end")
    lines.append("$upscope $end")
    lines.append("$enddefinitions $end")
    return "\n".join(lines)


def _synthesise_vcd(channels: list[int], n_samples: int, sample_rate_hz: int,
                    pattern: str = "idle", duration_s: float = 2.0) -> str:
    """Generate a synthetic VCD for testing. Only emits value-change lines (real VCD form)."""
    sample_period_ns = int(1e9 / sample_rate_hz)
    lines = [_vcd_header(channels)]

    # Compute the value of each channel at every sample, then emit only on changes.
    # State is a list of (channel_int) and we track previous value per channel.
    prev = {ch: 0 for ch in channels}

    def emit(t: int, ch: int, v: int) -> str:
        var = chr(ord('a') + ch)
        return f"#{t}\n{v}{var}"

    if pattern.startswith("seq_code_"):
        # Each capture should show a different 8-bit code on the data channels.
        # The code is encoded in the pattern itself: 'seq_code_5' = code 5 = 0b00000101
        try:
            seq_code = int(pattern.split("_")[-1])
        except ValueError:
            seq_code = 0
        lines.append(f"#0")
        # Settle all data channels to their bit value at t=0
        for ch in range(8):
            bit_value = (seq_code >> ch) & 1
            if bit_value != prev.get(ch, 0):
                lines.append(f"#0")
                lines.append(f"{bit_value}{chr(ord('a') + ch)}")
                prev[ch] = bit_value
        # Single strobe pulse on ch7 at t=50..200 (within capture window)
        pulse_start = 50
        pulse_end = 200
        if prev.get(7, 0) != 1:
            lines.append(f"#{pulse_start}")
            lines.append(f"1{chr(ord('a') + 7)}")
            prev[7] = 1
        if prev.get(7, 0) != 0:
            lines.append(f"#{pulse_end}")
            lines.append(f"0{chr(ord('a') + 7)}")
            prev[7] = 0

    elif pattern == "shark_fin":
        # Simulate the contested 'C' line: rises sharply, then droops linearly
        # Other channels do normal idle/strobe
        lines.append(f"#0")
        for i in range(n_samples):
            t = i * sample_period_ns
            for ch in channels:
                if ch == 0:
                    # 'C' line contention burst every ~100 samples
                    burst = (i // 100) % 2 == 0
                    if burst:
                        burst_pos = i % 100
                        if burst_pos < 10:
                            v = 1  # sharp rise
                        elif burst_pos < 50:
                            v = 1  # hold high briefly
                        elif burst_pos < 80:
                            v = 0  # linear droop
                        else:
                            v = 0  # settled low
                    else:
                        v = 0
                elif ch == 7:
                    # 10 kHz strobe — toggle every 1200 samples at 24 MHz
                    v = 1 if (i // 1200) % 2 == 0 else 0
                else:
                    v = 0
                if v != prev[ch]:
                    lines.append(f"#{t}")
                    lines.append(f"{v}{chr(ord('a') + ch)}")
                    prev[ch] = v

    elif pattern == "clean_strobe":
        lines.append(f"#0")
        for i in range(n_samples):
            t = i * sample_period_ns
            for ch in channels:
                if ch == 7:
                    v = 1 if (i // 1200) % 2 == 0 else 0
                else:
                    v = 0
                if v != prev[ch]:
                    lines.append(f"#{t}")
                    lines.append(f"{v}{chr(ord('a') + ch)}")
                    prev[ch] = v

    elif pattern == "stuck_high":
        lines.append(f"#0")
        for ch in channels:
            v = 1
            if v != prev[ch]:
                lines.append(f"#0")
                lines.append(f"{v}{chr(ord('a') + ch)}")
                prev[ch] = v

    elif pattern == "stuck_low":
        # All zeros, no transitions to emit
        pass

    elif pattern.startswith("e2e_cpu_to_ac4"):
        # -------------------------------------------------------------------
        # Synthetic capture for the bus_e2e_cpu_to_ac4 end-to-end test.
        # -------------------------------------------------------------------
        # Models a single Second-Function-3 byte write to A7L2 = 0b00000001
        # (DB0 = 1, DB1..DB7 = 0), traced through the canonical 4-stage
        # pipeline:
        #
        #   Stage 1 (CPU):    IC5 P8085A — clock, ALE, /WR
        #   Stage 2 (Buffer): IC10 74LS245 + IC11 74LS244 — bus out
        #   Stage 3 (Decode): AC3/ACL3 IC1 74LS138 — A7L0..A7L6
        #   Stage 4 (DAC):    AC4 IC6 AD7522 — /CS, /WR, LB, HB, LDAC, Iout
        #
        # The harness maps stages to 8 LA channels:
        #   CH0  IC5.30     ALE
        #   CH1  IC5.31     /WR
        #   CH2  IC5.37     CLK (3.072 MHz) — decimated to a strobe in sim
        #   CH3  IC21.Q     A0 latched
        #   CH4  IC11.18    'C' address line (the shark fin target)
        #   CH5  AC3/ACL3 IC1.Y1  A7L1 (active-LOW when A7L1 selected)
        #   CH6  AC4 IC6.24 LB strobe (active-LOW pulse)
        #   CH7  AC4 IC6.21 LDAC strobe (active-LOW pulse)
        #
        # Parse an optional fault tag: e.g. "e2e_cpu_to_ac4:contention_c"
        #   - "clean"               (default) — every stage fires cleanly
        #   - "no_clock"            — Stage 1: CH2 stays LOW
        #   - "missing_wr"          — Stage 1: CH1 never goes LOW
        #   - "contention_c"        — Stage 2: CH4 has shark-fin droop
        #   - "decoder_no_y"        — Stage 3: CH5 never goes LOW
        #   - "missing_lb"          — Stage 4: CH6 never goes LOW
        #   - "lb_too_slow"         — Stage 4: CH6 has 200-ns rise
        #   - "missing_ldac"        — Stage 4: CH7 never goes LOW
        # -------------------------------------------------------------------
        if ":" in pattern:
            _, fault = pattern.split(":", 1)
        else:
            fault = "clean"

        lines.append("#0")
        # Initial states: everything LOW except where the fault demands
        for ch in channels:
            v = 0
            if v != prev[ch]:
                lines.append(f"0{chr(ord('a') + ch)}")
                prev[ch] = v

        # Stage 1 — CPU activity. 12 clock pulses (CH2), 2 ALE pulses (CH0),
        # 2 /WR pulses (CH1).  /WR happens AFTER ALE — write cycle.
        if fault != "no_clock":
            t = 100
            for cycle in range(2):
                # CLK tick: CH2 high briefly
                lines.append(f"#{t}")
                lines.append(f"1{chr(ord('a') + 2)}")
                prev[2] = 1
                t += 50
                lines.append(f"#{t}")
                lines.append(f"0{chr(ord('a') + 2)}")
                prev[2] = 0
                t += 50
                # ALE HIGH: CH0 (demuxes address from AD0-AD7)
                lines.append(f"#{t}")
                lines.append(f"1{chr(ord('a') + 0)}")
                prev[0] = 1
                t += 50
                lines.append(f"#{t}")
                lines.append(f"0{chr(ord('a') + 0)}")
                prev[0] = 0
                t += 50
                # A0 latched: CH3 stays HIGH for the duration of the cycle
                lines.append(f"#{t}")
                lines.append(f"1{chr(ord('a') + 3)}")
                prev[3] = 1
                t += 50
                if fault != "missing_wr":
                    # /WR LOW pulse: CH1
                    lines.append(f"#{t}")
                    lines.append(f"0{chr(ord('a') + 1)}")
                    prev[1] = 0
                    t += 100
                    lines.append(f"#{t}")
                    lines.append(f"1{chr(ord('a') + 1)}")
                    prev[1] = 1
                    t += 50
                else:
                    t += 150
                # End of cycle: A0 goes LOW
                lines.append(f"#{t}")
                lines.append(f"0{chr(ord('a') + 3)}")
                prev[3] = 0
                t += 100

        # Stage 2 — Buffer. The 'C' address line (CH4) tracks the decoded
        # address onto the bus. In the clean case it's a clean square wave.
        if fault == "contention_c":
            # Shark-fin on the scope = many rapid sub-ns transitions
            # on the LA. Two TTL drivers fighting on the same line
            # produce a glitchy waveform as the dominant driver
            # toggles back and forth with the sinking one.
            # Emit a burst of 16 high-frequency toggles.
            lines.append(f"#{t}")
            lines.append(f"1{chr(ord('a') + 4)}")
            prev[4] = 1
            t += 20
            for glitch in range(16):
                # 60-ns-wide pulse-train (within 24-MHz LA sample window)
                v = 0 if glitch % 2 == 0 else 1
                lines.append(f"#{t}")
                lines.append(f"{v}{chr(ord('a') + 4)}")
                prev[4] = v
                t += 30
        else:
            # Clean square wave on the 'C' line: 1 cycle (2 transitions)
            # A normal bus cycle has 1-2 toggles on the address line
            lines.append(f"#{t}")
            lines.append(f"1{chr(ord('a') + 4)}")
            prev[4] = 1
            t += 100
            lines.append(f"#{t}")
            lines.append(f"0{chr(ord('a') + 4)}")
            prev[4] = 0
            t += 100

        # Stage 3 — Decoder. AC3/ACL3 IC1 (74LS138) asserts one Y-output
        # when the address matches. CH5 = Y1 = A7L1 (the write target).
        if fault != "decoder_no_y":
            lines.append(f"#{t}")
            lines.append(f"0{chr(ord('a') + 5)}")
            prev[5] = 0
            t += 200
            lines.append(f"#{t}")
            lines.append(f"1{chr(ord('a') + 5)}")
            prev[5] = 1
            t += 100
        else:
            t += 300

        # Stage 4 — DAC. /CS propagates (CH5 → CH6 strobe at AD7522).
        # In a real AD7522 the LB and HB strobes are derived from the
        # decoder output. CH6 = LB (Pin 24). CH7 = LDAC (Pin 21).
        if fault != "missing_lb":
            if fault == "lb_too_slow":
                # 200-ns rise time: step 4 times through intermediate
                # voltages to simulate a slow edge (sigrok samples as
                # transitions, so we emit 4 sub-edges instead of 1)
                lines.append(f"#{t}")
                lines.append(f"0{chr(ord('a') + 6)}")
                prev[6] = 0
                t += 50
                for i in range(4):
                    lines.append(f"#{t}")
                    if i % 2 == 0:
                        lines.append(f"1{chr(ord('a') + 6)}")
                        prev[6] = 1
                    else:
                        lines.append(f"0{chr(ord('a') + 6)}")
                        prev[6] = 0
                    t += 50
                # Final settled state
                lines.append(f"#{t}")
                lines.append(f"1{chr(ord('a') + 6)}")
                prev[6] = 1
                t += 50
            else:
                # Clean LB pulse: 50 ns low, then back high
                lines.append(f"#{t}")
                lines.append(f"0{chr(ord('a') + 6)}")
                prev[6] = 0
                t += 100
                lines.append(f"#{t}")
                lines.append(f"1{chr(ord('a') + 6)}")
                prev[6] = 1
                t += 50

        if fault != "missing_ldac":
            # LDAC fires 200 ns after LB rises (data-settle window)
            lines.append(f"#{t}")
            lines.append(f"0{chr(ord('a') + 7)}")
            prev[7] = 0
            t += 100
            lines.append(f"#{t}")
            lines.append(f"1{chr(ord('a') + 7)}")
            prev[7] = 1
            t += 50

    else:  # idle
        # All zeros, no transitions
        pass

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def capture(step: dict, out_dir: Path, mode: str = "auto",
            on_progress: Optional[Callable[[Path, Callable[[], bool]], Any]] = None,
            live_buffer: Optional[Any] = None
            ) -> Capture:
    """Run a capture step.

    mode: 'hardware' | 'dry_run' | 'simulate' | 'auto'
          'auto' picks hardware if available, else simulate

    on_progress: optional callable for hardware mode, signature
                 on_progress(vcd_path: Path, should_stop: Callable[[], bool]) -> None
                 Called once, just after the sigrok subprocess has been started
                 and is writing the VCD to vcd_path. The callable is expected
                 to return quickly after launching any background UI (e.g. a
                 live dashboard thread) and may use should_stop() to poll
                 whether the capture subprocess has finished. Only invoked in
                 'hardware' mode; other modes ignore it.

    live_buffer: optional thread-safe byte sink (anything with a
                 .write(bytes) method and a .read_since(offset) -> (bytes, new_offset)
                 method — see harness.ui.LiveBuffer). When provided AND mode is
                 'hardware', the VCD is streamed from sigrok's stdout directly
                 into the buffer (no disk I/O during capture). Once the
                 subprocess exits, the buffer's contents are written to vcd_path
                 in a single shot so the post-capture analyser still has a file.
                 When None, falls back to the legacy file-based capture where
                 sigrok writes directly to vcd_path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    name = step.get("id", "capture")
    duration_s = float(step.get("duration_s", 2.0))
    sample_rate_hz = int(step.get("sample_rate_hz", 1_000_000))
    channels = list(step.get("channels", list(range(8))))
    trigger = step.get("trigger")

    if mode == "auto":
        mode = "hardware" if _check_hardware() else "simulate"

    ts = _timestamp()
    base = f"{name}_{ts}"
    raw_path = out_dir / f"{base}.raw"
    vcd_path = out_dir / f"{base}.vcd"

    n_samples = int(duration_s * sample_rate_hz)

    if mode == "hardware":
        sr = _check_sigrok()
        if not sr:
            raise RuntimeError("sigrok-cli not installed (apt install sigrok-cli)")
        # sigrok-cli names the LA's digital channels D0, D1, ... not 0, 1, ...
        # The harness's test definitions use integer channel numbers; convert
        # them to sigrok's D-prefixed names here. ("all" is also accepted.)
        ch_spec = ",".join(f"D{c}" for c in channels)
        # sigrok-cli accepts integer seconds ("1s") or integer ms ("1000ms"),
        # but NOT floats ("1.0s" fails with "Invalid time"). If the duration
        # is a whole number of seconds, use "Ns"; otherwise convert to ms.
        if duration_s == int(duration_s):
            time_spec = f"{int(duration_s)}s"
        else:
            time_spec = f"{int(round(duration_s * 1000))}ms"
        # Build the sigrok-cli command. Two flavours:
        #   - With live_buffer:    stdout=PIPE  → reader thread → buffer
        #   - Without live_buffer: -o <file>    → sigrok writes directly
        # The in-memory path is preferred for hardware runs because it avoids
        # disk I/O competing with the USB bulk-transfer endpoint for the LA,
        # which on macOS can cause sigrok to stall or the device to drop off.
        use_pipe = (live_buffer is not None)
        cmd = [sr, "-d", "fx2lafw", "-C", ch_spec,
               "-c", f"samplerate={sample_rate_hz}",
               "--time", time_spec,
               "-O", "vcd"]
        if not use_pipe:
            # sigrok writes the chosen output format to the file given by
            # -o. The "vcd=filename=..." form is NOT supported; that's a
            # PulseView export extension. The VCD goes to the output file
            # directly.
            cmd += ["-o", str(vcd_path)]
        # TODO: trigger handling is more complex in sigrok-cli; for now, no trigger

        if use_pipe:
            proc = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        else:
            proc = subprocess.Popen(cmd,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)

        # Reader thread: pumps sigrok's stdout into live_buffer.
        # Always run it (even if use_pipe is False) so a future change can
        # consume the pipe without restructuring the wait logic.
        pump_done = threading.Event()

        def _pump_stdout():
            try:
                assert proc.stdout is not None
                stdout_buf = proc.stdout  # type: ignore[assignment]
                while True:
                    # read1 returns up to N bytes without blocking for more
                    # (vs read(N) which blocks until N bytes or EOF)
                    chunk = stdout_buf.read1(65536)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    if live_buffer is not None:
                        live_buffer.write(chunk)
            except Exception as e:  # noqa: BLE001
                print(f"warning: stdout pump raised {e!r}", file=sys.stderr)
            finally:
                pump_done.set()

        pump_thread = threading.Thread(target=_pump_stdout, daemon=True,
                                       name="vcd-pump")
        pump_thread.start()

        if on_progress is not None:
            try:
                on_progress(vcd_path, lambda: proc.poll() is not None)
            except Exception as e:  # noqa: BLE001 — never let a UI hook kill a capture
                print(f"warning: on_progress hook raised {e!r}", file=sys.stderr)
        try:
            proc.wait(timeout=duration_s + 10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pump_done.wait(timeout=2)
            raise RuntimeError(f"sigrok-cli timed out after {duration_s + 10}s")
        # sigrok has exited; let the reader drain any remaining bytes
        pump_thread.join(timeout=2)
        if proc.returncode != 0:
            stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8",
                                                                          errors="replace")
            raise RuntimeError(
                f"sigrok-cli exited with status {proc.returncode}: {stderr.strip()}"
            )

        # Flush the in-memory buffer to disk (single write — no streaming I/O)
        if use_pipe and live_buffer is not None:
            data, _ = live_buffer.read_since(0)
            vcd_path.write_bytes(data)

        # Also save raw (sigrok-cli can do -O raw too, but VCD is more portable)
        raw_path.write_bytes(b"")  # placeholder; raw is optional

        return Capture(name=name, sample_rate_hz=sample_rate_hz, n_samples=n_samples,
                       duration_s=duration_s, channels=channels, trigger=trigger,
                       raw_path=raw_path, vcd_path=vcd_path,
                       captured_at=ts, mode="hardware",
                       notes="captured via sigrok-cli fx2lafw (Saleae Logic 8ch/24MHz)")

    if mode == "dry_run":
        # No file, no capture. Just a stub.
        return Capture(name=name, sample_rate_hz=sample_rate_hz, n_samples=0,
                       duration_s=duration_s, channels=channels, trigger=trigger,
                       raw_path=raw_path, vcd_path=None,
                       captured_at=ts, mode="dry_run",
                       notes="dry-run: no capture performed")

    if mode == "simulate":
        # Pick a pattern that makes the test interesting
        pattern = step.get("_simulate_pattern", "clean_strobe")
        # In simulate mode, scale down sample count so it doesn't take 10s of wall time.
        # We use 1000 samples max for testing — plenty to see the pattern.
        n_samples_sim = min(n_samples, 1000)
        vcd = _synthesise_vcd(channels, n_samples_sim, sample_rate_hz, pattern=pattern,
                              duration_s=duration_s)
        vcd_path.write_text(vcd)
        raw_path.write_bytes(b"")
        return Capture(name=name, sample_rate_hz=sample_rate_hz, n_samples=n_samples_sim,
                       duration_s=duration_s, channels=channels, trigger=trigger,
                       raw_path=raw_path, vcd_path=vcd_path,
                       captured_at=ts, mode="simulate",
                       notes=f"synthetic VCD, pattern={pattern}, scaled to {n_samples_sim} samples for speed")

    raise ValueError(f"unknown mode: {mode}")


def _read_vcd_header(vcd_path: Path) -> tuple[dict[int, str], int, str]:
    """Read the $var definitions in a VCD and build:
       - var_id_by_channel:  {channel_int: var_id_string}  — based on the
         harness's $var convention "<id> ch<n>" (one-letter per channel).
         For sigrok-generated VCDs (no "ch<n>" suffix), the order in which
         D0..D7 are defined maps to channel 0..7.
       - timescale_factor:   multiplier to convert raw VCD timestamps to ns
         (e.g. "1 ns" -> 1, "100 ps" -> 0.1, "1 ps" -> 0.001).
       - timescale_unit:     the original unit string, for diagnostics.

    Note: This is lazy-friendly — the file is opened, header is read, file is
    closed. The body of the VCD is small enough that reading it twice is fine
    for a 24-MHz × N-second capture (up to ~5M lines).
    """
    var_id_by_channel: dict[int, str] = {}
    timescale_factor = 1.0
    timescale_unit = "1 ns"
    in_defs = True
    # Track the D-channel ordering if we don't find a ch<n> suffix
    d_order: list[str] = []
    with vcd_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("$enddefinitions"):
                break  # done with header
            if line.startswith("$timescale"):
                # "$timescale 1 ns $end"  or  "$timescale 100ps $end"
                parts = line.split()
                if len(parts) >= 2:
                    # second token is "<value> <unit>" e.g. "1 ns", "100ps", "1us"
                    val_unit = parts[1] + (parts[2] if len(parts) >= 3 else "")
                    val_unit = val_unit.replace("$end", "").strip()
                    timescale_unit = val_unit
                    timescale_factor = _vcd_timescale_to_ns(val_unit)
                continue
            if not line.startswith("$var"):
                continue
            # "$var wire 1 ! D0 $end"      (sigrok)
            # "$var wire 1 a  ch0 $end"    (harness simulator)
            parts = line.split()
            # parts: ['$var', 'wire', '1', '<id>', '<name>', '$end']
            if len(parts) < 5:
                continue
            var_id = parts[3]
            name = parts[4]
            # Harness convention: name == "ch<n>"
            if name.startswith("ch") and name[2:].isdigit():
                ch = int(name[2:])
                var_id_by_channel[ch] = var_id
            elif name.startswith("D") and name[1:].isdigit():
                # sigrok convention: D0, D1, ... — assign channel by order
                d_order.append(var_id)
    # Assign D-named vars to channels 0..7 in declaration order
    for idx, var_id in enumerate(d_order):
        if idx not in var_id_by_channel:
            var_id_by_channel[idx] = var_id
    return var_id_by_channel, timescale_factor, timescale_unit


_UNIT_TO_NS = {
    "s":  1_000_000_000.0,
    "ms": 1_000_000.0,
    "us": 1_000.0,
    "ns": 1.0,
    "ps": 0.001,
    "fs": 0.000001,
}


def _vcd_timescale_to_ns(spec: str) -> float:
    """Convert a VCD timescale spec like "1 ns", "100ps", "10 us" to ns."""
    spec = spec.strip()
    # split numeric prefix and unit suffix
    i = 0
    while i < len(spec) and (spec[i].isdigit() or spec[i] == "."):
        i += 1
    num_str = spec[:i] or "1"
    unit = spec[i:].strip().lower() or "ns"
    try:
        num = float(num_str)
    except ValueError:
        return 1.0
    return num * _UNIT_TO_NS.get(unit, 1.0)


# Cache parsed VCD headers in-process. VCDs from the same test are re-read
# many times by the analysers; this avoids re-parsing the header each call.
_VCD_HEADER_CACHE: dict[str, tuple[dict[int, str], int, str]] = {}


def _get_vcd_header(vcd_path: Path) -> tuple[dict[int, str], int, str]:
    key = str(vcd_path)
    if key not in _VCD_HEADER_CACHE:
        _VCD_HEADER_CACHE[key] = _read_vcd_header(vcd_path)
    return _VCD_HEADER_CACHE[key]


def parse_vcd_transitions(vcd_path: Path, channel: int) -> list[tuple[int, int]]:
    """Parse a VCD file and return [(timestamp_ns, new_value), ...] for one channel.

    Supports both VCD flavours the harness encounters:
      - Simulator output: $var lines are "ch<n>", var IDs are a..h, timescale
        is "1 ns".
      - sigrok-cli output: $var lines are "D<n>", var IDs are arbitrary
        (typically !"#$%&'() ), timescale is "100 ps" or similar.

    Timestamps are returned in **nanoseconds** (the analysers treat them as ns).
    """
    if not vcd_path.exists():
        return []
    var_id_by_channel, timescale_factor, _ = _get_vcd_header(vcd_path)
    var_id = var_id_by_channel.get(channel)
    if var_id is None:
        # Fallback: old harness behaviour — assume 'a' + channel.
        # Lets the parser keep working if a test ever uses a non-D0..D7
        # channel naming convention.
        var_id = chr(ord('a') + channel)

    transitions: list[tuple[int, int]] = []
    current_time_ns = 0
    in_defs = True
    with vcd_path.open() as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if in_defs:
                if line.startswith("$enddefinitions"):
                    in_defs = False
                continue
            if line.startswith("#"):
                # sigrok packs multiple value changes on the same line as
                # the timestamp: "#0 1! 0\" 0# 1$". Handle that.
                tokens = line.split()
                try:
                    raw_t = int(tokens[0][1:])
                    current_time_ns = int(round(raw_t * timescale_factor))
                except ValueError:
                    pass
                # The remaining tokens on this line are value changes
                # (sigrok emits initial-state transitions inlined like this).
                for token in tokens[1:]:
                    if len(token) < 2:
                        continue
                    if token[0] in "01xz" and token[1] == var_id:
                        v = int(token[0]) if token[0] in "01" else 0
                        transitions.append((current_time_ns, v))
                        break
                continue
            # Subsequent value changes (post-initial-state): one per line
            # in the harness simulator, or one per line in sigrok too. (sigrok
            # only packs the initial-state block on the first transition line.)
            for token in line.split():
                if len(token) < 2:
                    continue
                if token[0] in "01xz" and token[1] == var_id:
                    v = int(token[0]) if token[0] in "01" else 0
                    transitions.append((current_time_ns, v))
                    break  # one channel per line
        return transitions


def clear_vcd_cache() -> None:
    """Clear the parsed-VCD header cache. Useful between test runs."""
    _VCD_HEADER_CACHE.clear()


if __name__ == "__main__":
    import sys
    print("sigrok-cli:", _check_sigrok() or "NOT INSTALLED")
    print("hardware detected:", _check_hardware())
    cap = capture({"id": "test", "duration_s": 0.1, "sample_rate_hz": 1_000_000,
                   "channels": list(range(8))},
                  Path("/tmp"), mode="simulate")
    print("simulated capture:", cap)
    transitions = parse_vcd_transitions(cap.vcd_path, 0)
    print(f"channel 0 transitions: {len(transitions)}")
