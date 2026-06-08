"""Test fixtures for the Marconi 2019A harness unit tests.

The analysers work on `Capture` objects (raw VCD files on disk). To make
tests fast and deterministic, we write synthetic VCDs to a tmp path and
wrap them in a Capture. A few helpers here build common shapes:

    make_vcd(ch_transitions, ...)        # generic — list of (t_ns, val) per ch
    make_clean_strobe(...)               # periodic 50% duty strobe
    make_dac_sweep(...)                  # 12 LBS pulses with incrementing 5-bit code
    make_idle_capture(...)               # all-zero
    make_stuck_high_capture(...)         # all-one
    make_8085_clock_capture(...)         # 3.072 MHz on CH2

All times in nanoseconds.
"""
from __future__ import annotations
from pathlib import Path
import sys

# Make the harness package importable from the tests/ dir
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from harness.capture import Capture  # noqa: E402


def _write_vcd(path: Path, ch_transitions: dict[int, list[tuple[int, int]]],
               timescale: str = "1 ns") -> Path:
    """Write a synthetic VCD. ch_transitions maps channel-int to a sorted
    list of (t_ns, value) for that channel.

    Writes plain .vcd (uncompressed) to keep tests simple. The analyser
    reads both plain and .gz transparently, so this works.
    """
    if path.suffix != ".vcd":
        path = path.with_suffix(".vcd")
    lines = [
        "$date", "test", "$end",
        "$version harness-test 1.0 $end",
        f"$timescale {timescale} $end",
        "$scope module logic $end",
    ]
    channels = sorted(ch_transitions.keys())
    for ch in channels:
        var_id = chr(ord('a') + ch)
        lines.append(f"$var wire 1 {var_id} ch{ch} $end")
    lines.append("$upscope $end")
    lines.append("$enddefinitions $end")

    # Initial state: the first transition's value, or 0 if no transitions
    initial = {ch: 0 for ch in channels}
    for ch, trans in ch_transitions.items():
        if trans:
            initial[ch] = trans[0][1]
    lines.append("#0")
    for ch, v in initial.items():
        var_id = chr(ord('a') + ch)
        lines.append(f"{v}{var_id}")

    # Walk all transitions across all channels in time order.
    flat: list[tuple[int, int, int]] = []
    for ch, trans in ch_transitions.items():
        for t, v in trans:
            flat.append((t, ch, v))
    flat.sort()

    # Group transitions at the same timestamp so they share a `#<ts>` line
    # (matches sigrok's packed-emission format the parser handles).
    # Skip the first transition for each channel if it's at t=0 with the
    # same value as the initial state we already emitted — otherwise the
    # parser sees a duplicate (0, v) for that channel.
    # Also dedupe consecutive same-value emissions on the same channel
    # at the same timestamp (a test helper that gives [(1000, 1)] * 5
    # shouldn't produce 5 redundant VCD lines).
    last_emitted: dict[int, int] = {ch: initial.get(ch, 0) for ch in channels}
    i = 0
    while i < len(flat):
        t = flat[i][0]
        lines.append(f"#{t}")
        while i < len(flat) and flat[i][0] == t:
            _, ch, v = flat[i]
            if v == last_emitted.get(ch):
                # Same value as last emission on this channel — skip
                i += 1
                continue
            var_id = chr(ord('a') + ch)
            lines.append(f"{v}{var_id}")
            last_emitted[ch] = v
            i += 1
    lines.append("")  # trailing newline
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def make_capture(vcd_path: Path, name: str = "test_cap",
                 duration_s: float = 1.0, sample_rate_hz: int = 24_000_000,
                 channels: list[int] | None = None) -> Capture:
    """Wrap a VCD in a Capture object the way `capture()` would."""
    if channels is None:
        channels = list(range(8))
    return Capture(
        name=name,
        sample_rate_hz=sample_rate_hz,
        n_samples=int(duration_s * sample_rate_hz),
        duration_s=duration_s,
        channels=channels,
        trigger=None,
        raw_path=Path("/dev/null"),
        vcd_path=vcd_path,
        captured_at="test",
        mode="simulate",
        notes="synthetic",
    )


def make_vcd(tmp_dir: Path, name: str,
             ch_transitions: dict[int, list[tuple[int, int]]],
             duration_s: float = 1.0,
             sample_rate_hz: int = 24_000_000,
             timescale: str = "1 ns") -> Capture:
    """Write a VCD with the given per-channel transitions, return a Capture."""
    p = _write_vcd(tmp_dir / name, ch_transitions, timescale=timescale)
    return make_capture(p, name=name, duration_s=duration_s,
                        sample_rate_hz=sample_rate_hz,
                        channels=sorted(ch_transitions.keys()) or list(range(8)))


def make_clean_strobe(tmp_dir: Path, name: str = "strobe",
                      strobe_ch: int = 0, freq_hz: float = 1000.0,
                      duration_s: float = 1.0) -> Capture:
    """Periodic 50% duty strobe on `strobe_ch`, all other channels idle."""
    period_ns = int(1e9 / freq_hz)
    half = period_ns // 2
    n_pulses = int(duration_s * freq_hz)
    trans = []
    for i in range(n_pulses):
        t_rise = i * period_ns
        t_fall = t_rise + half
        trans.append((t_rise, 1))
        trans.append((t_fall, 0))
    return make_vcd(tmp_dir, name, {strobe_ch: trans}, duration_s=duration_s)


def make_dac_sweep(tmp_dir: Path, name: str = "dac_sweep",
                   n_steps: int = 12, period_ms: int = 1000,
                   lbs_ch: int = 0, db_chs: tuple[int, ...] = (1, 2, 3, 4, 5),
                   start_code: int = 0) -> Capture:
    """Synthesise a +7 dBm → -5 dBm level-sweep pattern for `level_sweep_dac`.

    Per LBS pulse, the 5-bit data bus shows an incrementing code.
    `start_code` is the first code; the 5 bits (ch[0] = bit 0 LSB) are
    asserted 200 ns BEFORE the LBS rising edge (proper CPU write cycle).
    The LBS pulse is 200 ns wide. Data releases 50 ns after LBS falls.

    The first LBS is at t=0, so data setup is omitted for the first step
    (would require negative timestamps). This is realistic — the very
    first DAC write doesn't have a "previous state" to settle from.
    """
    transitions: dict[int, list[tuple[int, int]]] = {ch: [] for ch in (lbs_ch, *db_chs)}
    # Initial state: all data lines LOW (CPU driving 0x00 to start)
    # (initial state of all channels is 0, see _write_vcd)
    for step in range(n_steps):
        t_event = step * period_ms * 1_000_000  # ns
        code = (start_code + step) & 0x1F
        # Settle data 200 ns before LBS rises (skip for step 0 to avoid t<0)
        if step > 0:
            t_data_setup = t_event - 200
            for i, ch in enumerate(db_chs):
                bit = (code >> i) & 1
                transitions[ch].append((t_data_setup, bit))
        else:
            # For step 0, just set the data at t=0
            for i, ch in enumerate(db_chs):
                bit = (code >> i) & 1
                transitions[ch].append((0, bit))
        # LBS rising edge
        transitions[lbs_ch].append((t_event, 1))
        # LBS falling edge 200 ns later
        transitions[lbs_ch].append((t_event + 200, 0))
        # Data releases 50 ns after LBS falls (= 250 ns after LBS rising)
        t_data_release = t_event + 200 + 50
        for ch in db_chs:
            transitions[ch].append((t_data_release, 0))
    return make_vcd(tmp_dir, name, transitions,
                    duration_s=(n_steps * period_ms / 1000.0))


def make_8085_clock(tmp_dir: Path, name: str = "clk",
                    clk_ch: int = 2, freq_hz: float = 3_072_000.0,
                    duration_s: float = 0.01) -> Capture:
    """3.072 MHz continuous clock — the 8085's CLK OUT (page-083)."""
    return make_clean_strobe(tmp_dir, name, strobe_ch=clk_ch,
                             freq_hz=freq_hz, duration_s=duration_s)


def make_stuck_high(tmp_dir: Path, name: str = "stuck_high",
                    channels: list[int] | None = None) -> Capture:
    if channels is None:
        channels = list(range(8))
    transitions = {ch: [(0, 1)] for ch in channels}
    return make_vcd(tmp_dir, name, transitions, duration_s=1.0)


def make_stuck_low(tmp_dir: Path, name: str = "stuck_low",
                   channels: list[int] | None = None) -> Capture:
    """No transitions at all — every channel is stuck low."""
    if channels is None:
        channels = list(range(8))
    return make_vcd(tmp_dir, name, {ch: [] for ch in channels}, duration_s=1.0)


def make_shark_fin(tmp_dir: Path, name: str = "shark_fin",
                   target_ch: int = 4, period_us: int = 10) -> Capture:
    """The classic shark-fin on the 'C' line (IC11.18). Sharp rise, slow
    droop, settling, repeat. Other channels idle."""
    burst_period = period_us * 1000  # ns
    transitions: dict[int, list[tuple[int, int]]] = {target_ch: []}
    n_bursts = 100  # enough for any reasonable test
    for i in range(n_bursts):
        t = i * burst_period
        # sharp rise
        transitions[target_ch].append((t, 1))
        # slow droop in 5 small steps over 200 ns
        for k in range(1, 6):
            transitions[target_ch].append((t + k * 40, 0))
        # stay low for the rest of the burst
    return make_vcd(tmp_dir, name, transitions, duration_s=n_bursts * period_us / 1e6)


def make_ls138_walking(tmp_dir: Path, name: str = "ls138",
                       n_walks: int = 8, period_us: int = 100,
                       y_base_ch: int = 0) -> Capture:
    """Synthesise a 74LS138 /Y0.. /Y7 walking pattern — one /Yn active
    at a time, walking 0..7 and repeating. Tests the level_sweep_ls138
    protocol_decode pattern."""
    n_y = 8
    transitions: dict[int, list[tuple[int, int]]] = {y_base_ch + i: [] for i in range(n_y)}
    period_ns = period_us * 1000
    for cycle in range(n_walks):
        t = cycle * period_ns
        # Walk: one /Y LOW at a time, all others HIGH
        for yn in range(n_y):
            t0 = t + yn * (period_ns // n_y)
            t1 = t0 + (period_ns // n_y) - 100
            for yi in range(n_y):
                ch = y_base_ch + yi
                v = 0 if yi == yn else 1
                if transitions[ch] and transitions[ch][-1][1] != v:
                    transitions[ch].append((t0, v))
                elif not transitions[ch]:
                    transitions[ch].append((t0, v))
    return make_vcd(tmp_dir, name, transitions, duration_s=n_walks * period_us / 1e6)
