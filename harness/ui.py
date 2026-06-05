"""Terminal UI for the harness.

Two responsibilities:
  1. Walk through a test's prompts (clip, press, capture, etc.) and gather operator input
  2. Render a live dashboard: channel states + ASCII waveform bars + last-capture summary

The dashboard is rendered in-place using ANSI cursor escapes, so it updates smoothly
in any modern terminal (and over SSH).
"""
from __future__ import annotations
import os
import shutil
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional, Union

# Detect terminal capabilities
TERM = os.environ.get("TERM", "")
HAS_COLOR = sys.stdout.isatty() and ("xterm" in TERM or "color" in TERM or TERM == "screen" or TERM == "tmux" or "256" in TERM)
TERM_WIDTH = shutil.get_terminal_size((100, 30)).columns


# ANSI helpers
def _ansi(code: str) -> str:
    return f"\033[{code}m" if HAS_COLOR else ""

RESET = _ansi("0")
BOLD = _ansi("1")
DIM = _ansi("2")
RED = _ansi("31")
GREEN = _ansi("32")
YELLOW = _ansi("33")
BLUE = _ansi("34")
MAGENTA = _ansi("35")
CYAN = _ansi("36")
WHITE = _ansi("37")
BG_BLUE = _ansi("44")
BG_GREY = _ansi("100")
CLEAR_LINE = "\033[2K"
CURSOR_UP = lambda n: f"\033[{n}A" if n else ""
CURSOR_COL = lambda n: f"\033[{n}G" if n else ""


# -----------------------------------------------------------------------------
# Box drawing / banners
# -----------------------------------------------------------------------------

def banner(title: str, subtitle: str = "") -> None:
    width = min(TERM_WIDTH, 90)
    print()
    print(BG_BLUE + WHITE + BOLD + " " * width + RESET)
    print(BG_BLUE + WHITE + BOLD + f"  {title}".ljust(width) + RESET)
    if subtitle:
        print(BG_BLUE + WHITE + f"  {subtitle}".ljust(width) + RESET)
    print(BG_BLUE + WHITE + " " * width + RESET)
    print()


def info(msg: str) -> None:
    print(f"{CYAN}ℹ{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠{RESET}  {msg}")


def error(msg: str) -> None:
    print(f"{RED}✗{RESET}  {msg}")


def success(msg: str) -> None:
    print(f"{GREEN}✓{RESET}  {msg}")


def step_header(n: int, total: int, title: str) -> None:
    print()
    print(BOLD + BLUE + f"──── Step {n}/{total}: {title} " + "─" * max(0, 60 - len(title)) + RESET)


# -----------------------------------------------------------------------------
# Prompts
# -----------------------------------------------------------------------------

def prompt_text(text: str, default: str = "", multiline: bool = False) -> str:
    """Print a block of text, then prompt for input. Returns the input string."""
    print(text)
    print()
    if multiline:
        print(f"{DIM}(Enter a blank line to finish){RESET}")
        lines = []
        while True:
            try:
                line = input(f"{MAGENTA}│{RESET} ")
            except EOFError:
                break
            if not line:
                break
            lines.append(line)
        return "\n".join(lines) if lines else default
    else:
        prompt = f"{MAGENTA}❯{RESET} "
        if default:
            prompt += f"{DIM}[{default}]{RESET} "
        try:
            return input(prompt).strip() or default
        except EOFError:
            return default


def prompt_continue(text: str) -> None:
    """Press-enter-to-continue prompt."""
    print(text)
    print()
    try:
        input(f"{DIM}Press ENTER to continue...{RESET}")
    except EOFError:
        pass


def prompt_yesno(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"{MAGENTA}❯{RESET} {question} {DIM}{suffix}{RESET} ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def prompt_choice(question: str, choices: list[str]) -> int:
    for i, c in enumerate(choices, 1):
        print(f"  {BOLD}{i}{RESET}) {c}")
    while True:
        ans = input(f"{MAGENTA}❯{RESET} {question} {DIM}[1-{len(choices)}]{RESET} ").strip()
        if ans.isdigit() and 1 <= int(ans) <= len(choices):
            return int(ans) - 1
        warn("Invalid choice")


# -----------------------------------------------------------------------------
# Live dashboard
# -----------------------------------------------------------------------------

def _waveform_char(state: bool, char: str = "█") -> str:
    return f"{GREEN}{char}{RESET}" if state else f"{DIM}·{RESET}"


def render_dashboard(channels: dict[int, bool], labels: dict[int, str],
                     bar_width: int = 60, last_edge_ns: Optional[int] = None,
                     n_transitions: int = 0) -> None:
    """Render a one-line-per-channel dashboard. No animation — caller manages redraws."""
    for ch in sorted(channels):
        state = channels[ch]
        label = labels.get(ch, f"ch{ch}")
        # Truncate label
        if len(label) > 28:
            label = label[:25] + "..."
        bar = _waveform_char(state) * bar_width
        state_str = f"{GREEN}HIGH{RESET}" if state else f"{DIM}LOW {RESET}"
        print(f"  ch{ch} {DIM}│{RESET} {label:<28s} {DIM}│{RESET} {state_str}  {bar}")


def channel_label(probes: dict[int, str], ch: int) -> str:
    return probes.get(ch, f"ch{ch}")


# -----------------------------------------------------------------------------
# Menu / list
# -----------------------------------------------------------------------------

def list_tests_menu(tests: list[dict]) -> Optional[str]:
    print(f"{BOLD}Available tests:{RESET}")
    print()
    for i, t in enumerate(tests, 1):
        print(f"  {BOLD}{i}{RESET}) {CYAN}{t['key']}{RESET}  ({t['n_steps']} steps)")
        print(f"     {DIM}{t['description'][:90]}{RESET}")
    print(f"  {BOLD}q{RESET}) quit")
    print()
    while True:
        ans = input(f"{MAGENTA}❯{RESET} Pick a test {DIM}[1-{len(tests)} or q]{RESET}: ").strip()
        if ans in ("q", "quit", "exit"):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(tests):
            return tests[int(ans) - 1]["key"]
        warn("Invalid choice")


# -----------------------------------------------------------------------------
# Simple progress bar for captures
# -----------------------------------------------------------------------------

def progress_bar(current: int, total: int, width: int = 50, prefix: str = "") -> None:
    pct = current / total if total else 0
    fill = int(width * pct)
    bar = "█" * fill + "·" * (width - fill)
    sys.stdout.write(f"\r{CLEAR_LINE}{prefix} {bar} {pct*100:5.1f}%")
    sys.stdout.flush()


def countdown_progress(duration_s: float, prefix: str = "Capturing") -> None:
    """Show a live countdown. Used while sigrok captures."""
    start = time.time()
    width = 40
    while True:
        elapsed = time.time() - start
        remaining = max(0, duration_s - elapsed)
        pct = min(1.0, elapsed / duration_s)
        fill = int(width * pct)
        bar = "█" * fill + "·" * (width - fill)
        sys.stdout.write(f"\r{CLEAR_LINE}{prefix} {bar} {elapsed:5.1f}s / {duration_s:5.1f}s")
        sys.stdout.flush()
        if remaining <= 0:
            break
        time.sleep(0.1)
    print()


# -----------------------------------------------------------------------------
# Live waveform dashboard during capture
# -----------------------------------------------------------------------------

# VCD line format produced by sigrok-cli (libsigrok 0.5.x):
#   $date ... $end
#   $version libsigrok 0.5.2 $end
#   $comment ... $end
#   $timescale 100 ps $end
#   $scope module libsigrok $end
#   $var wire 1 ! D0 $end          <-- one byte per var id, prefixed "1" for 1-bit
#   $var wire 1 " D1 $end
#   ...
#   $upscope $end
#   $enddefinitions $end
#   #0 1! 1" 1# 1$ 1% 1& 1' 1(     <-- initial values, "1!" means 1-bit value 1 on var "!"
#   #1024831250 0!                  <-- timestamp (in 100ps ticks), then value changes
#   ...
#
# We tail this file while sigrok writes it. Each poll we read whatever's new
# since the last byte offset, parse it incrementally, update per-channel state
# and edge counts, and redraw the dashboard on top of the previous frame.

# -----------------------------------------------------------------------------
# LiveBuffer: thread-safe in-memory byte sink for the streaming capture path
# -----------------------------------------------------------------------------

class LiveBuffer:
    """Thread-safe append-only byte buffer.

    Use case: a reader thread (e.g. draining sigrok-cli's stdout) calls
    .write(bytes) to append data; a polling thread (e.g. the live dashboard)
    calls .read_since(offset) to fetch all new bytes since the last poll.

    Internally backed by a bytearray + a threading.Lock. read_since() returns
    a (data, new_offset) tuple where new_offset is the total length after the
    read, so callers can track progress monotonically.

    The rationale for an in-memory buffer (vs writing VCD to disk while
    capturing): sigrok's stdout pipe back-pressures naturally when the
    consumer falls behind, and macOS's USB bulk transfer for fx2lafw is
    sensitive to disk I/O competing for the USB bus. Streaming to memory
    keeps the capture off the storage path entirely until capture ends,
    at which point a single write_bytes() flushes the buffer to disk so the
    post-capture analyser still has a file.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def read_since(self, offset: int) -> tuple[bytes, int]:
        """Return all bytes from `offset` to the current end, plus the new total length."""
        with self._lock:
            new_len = len(self._buf)
            if offset >= new_len:
                return b"", new_len
            # bytes(bytearray[...]) copies; the dashboard's parser doesn't mutate.
            return bytes(self._buf[offset:]), new_len

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

def _parse_vcd_header_for_dashboard(path: Path, max_wait_s: float = 5.0) -> tuple[dict[str, tuple[int, str]], str, int]:
    """Wait for sigrok to write the $var definitions, then return:
       - var_id_map: {"!": (0, "D0"), '"': (1, "D1"), ...}
       - timescale_str: e.g. "100 ps"
       - timescale_factor_ns: multiplier to convert raw VCD timestamps to ns

       File-based variant — used when no LiveBuffer is available. The caller
       (live_capture_progress) uses _parse_vcd_header_from_buffer instead when
       the streaming path is in play.
    """
    start = time.time()
    text = ""
    var_re = __import__("re").compile(r'\$var\s+\w+\s+(\d+)\s+(\S+)\s+(\S+)\s+\$end')
    timescale_re = __import__("re").compile(r'\$timescale\s+(\d+)\s*(\S+)\s+\$end')
    while time.time() - start < max_wait_s:
        try:
            text = path.read_text(errors="replace")
        except (FileNotFoundError, PermissionError):
            time.sleep(0.05)
            continue
        if "$enddefinitions $end" in text:
            break
        time.sleep(0.05)
    else:
        # Timed out — return what we have, the caller will deal with empty data
        return {}, "1 ns", 1
    return _parse_vcd_header_text(text)


def _parse_vcd_header_from_buffer(buf: "LiveBuffer", max_wait_s: float = 5.0
                                   ) -> tuple[dict[str, tuple[int, str]], str, int]:
    """Buffer-based variant of _parse_vcd_header_for_dashboard. Polls the
    buffer until sigrok has written the $enddefinitions $end marker."""
    start = time.time()
    text = ""
    while time.time() - start < max_wait_s:
        data, _total = buf.read_since(0)
        if data:
            text = data.decode("utf-8", errors="replace")
        if "$enddefinitions $end" in text:
            break
        time.sleep(0.05)
    else:
        return {}, "1 ns", 1
    return _parse_vcd_header_text(text)


def _parse_vcd_header_text(text: str) -> tuple[dict[str, tuple[int, str]], str, int]:
    """Shared body of the header parsers — extract var_id_map, timescale_str,
       and timescale_factor_ns from already-loaded VCD header text."""
    import re
    var_re = re.compile(r'\$var\s+\w+\s+(\d+)\s+(\S+)\s+(\S+)\s+\$end')
    timescale_re = re.compile(r'\$timescale\s+(\d+)\s*(\S+)\s+\$end')

    var_id_map: dict[str, tuple[int, str]] = {}
    for m in var_re.finditer(text):
        width, vid, name = m.group(1), m.group(2), m.group(3)
        if width != "1":
            continue  # we only handle 1-bit wires for the dashboard
        # sigrok names channels D0, D1, ... in the order they appear in the $var block
        # We map by parsing the trailing integer from "D<n>"
        if name.startswith("D") and name[1:].isdigit():
            ch = int(name[1:])
            var_id_map[vid] = (ch, name)

    ts_match = timescale_re.search(text)
    if ts_match:
        factor, unit = int(ts_match.group(1)), ts_match.group(2)
        # Convert to nanoseconds
        unit_ns = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000,
                   "ps": 0.001, "fs": 0.000001}.get(unit, 1)
        timescale_factor_ns = factor * unit_ns
    else:
        timescale_factor_ns = 1  # assume ns

    return var_id_map, (ts_match.group(0) if ts_match else "1 ns"), timescale_factor_ns


def _consume_vcd_chunk(text: str, state: dict[int, bool],
                       rising: dict[int, int], falling: dict[int, int],
                       var_id_map: dict[str, tuple[int, str]]) -> None:
    """Parse every '#<ts> <val><id>' line in `text` and mutate state/edge dicts.
       The '#0 ...' line (initial values at t=0) sets the baseline but does
       NOT count as an edge. All other '#<ts> ...' lines are real transitions."""
    # Lines we care about look like: "#1024831250 0!"  or  "#0 1!"
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("#"):
            continue
        # Tokenise: "#<ts> <val><id> <val><id> ..."
        parts = line.split()
        if len(parts) < 2:
            continue
        ts_str = parts[0][1:]  # strip the leading '#'
        is_initial = (ts_str == "0")
        # parts[0] is the timestamp; parts[1:] are value changes
        for tok in parts[1:]:
            if len(tok) < 2:
                continue
            val_char, vid = tok[0], tok[1:]
            if vid not in var_id_map:
                continue
            ch, _ = var_id_map[vid]
            new_state = (val_char == "1")
            old_state = state.get(ch, False)
            state[ch] = new_state
            if is_initial:
                continue  # t=0 baseline — don't count as an edge
            if new_state and not old_state:
                rising[ch] = rising.get(ch, 0) + 1
            elif not new_state and old_state:
                falling[ch] = falling.get(ch, 0) + 1


def live_capture_progress(duration_s: float,
                          vcd_path: Optional[Path] = None,
                          probes: Optional[dict[int, str]] = None,
                          poll_interval_s: float = 0.2,
                          should_stop: Optional[Callable[[], bool]] = None,
                          live_buffer: Optional["LiveBuffer"] = None) -> dict:
    """Show a live per-channel dashboard while sigrok streams VCD data.

    Two data sources are supported (use exactly one):
      - vcd_path:     Path to a file being written by sigrok-cli (legacy mode,
                      where sigrok writes directly to disk via -o).
      - live_buffer:  a harness.ui.LiveBuffer that's being fed by a reader
                      thread pumping sigrok-cli's stdout. No disk I/O during
                      capture — preferred mode for hardware runs on macOS.

    Polls every poll_interval_s, parses new VCD lines, and redraws a
    one-line-per-channel state display in place using ANSI cursor escapes.

    Exits when EITHER:
      - the wall-clock duration has elapsed (duration_s), OR
      - should_stop() returns True (caller can use this to detect "sigrok done"
        before the wall clock hits duration_s)

    Returns a dict with the final state and edge counts:
       {"state": {ch: bool}, "rising": {ch: int}, "falling": {ch: int},
        "var_id_map": {vid: (ch, name)}, "timescale_factor_ns": float}
    """
    if (vcd_path is None) == (live_buffer is None):
        raise ValueError("live_capture_progress: pass exactly one of "
                         "vcd_path (legacy file mode) or live_buffer (streaming mode)")

    # Parse the $var header (waits up to 5s for sigrok to start streaming)
    if live_buffer is not None:
        var_id_map, _ts_str, ts_factor = _parse_vcd_header_from_buffer(
            live_buffer, max_wait_s=5.0)
    else:
        var_id_map, _ts_str, ts_factor = _parse_vcd_header_for_dashboard(
            vcd_path, max_wait_s=5.0)  # type: ignore[arg-type]
    if not var_id_map:
        # No header arrived. Fall back to a simple countdown so the operator
        # at least sees something happening.
        warn("live dashboard: VCD header not found, falling back to countdown")
        countdown_progress(duration_s, prefix="Capturing")
        return {"state": {}, "rising": {}, "falling": {}, "var_id_map": {},
                "timescale_factor_ns": 1.0}

    state: dict[int, bool] = {}
    rising: dict[int, int] = {}
    falling: dict[int, int] = {}
    probes = probes or {}

    # Reserve a probe label for every var we found, falling back to "D<n>"
    labels: dict[int, str] = {}
    for vid, (ch, name) in var_id_map.items():
        labels[ch] = probes.get(ch, name)

    # Cap bar width to terminal width minus the chrome
    bar_width = max(20, min(60, TERM_WIDTH - 50))
    n_channels = len(var_id_map)
    # Layout: 1 header line + n_channels dashboard lines
    dashboard_height = 1 + n_channels

    start = time.time()
    last_offset = 0
    residual = ""  # partial line from the previous chunk
    first_draw = True
    while True:
        elapsed = time.time() - start
        remaining = max(0.0, duration_s - elapsed)

        # Read whatever has been streamed/flushed since the last poll
        if live_buffer is not None:
            data, last_offset = live_buffer.read_since(last_offset)
            chunk = data.decode("utf-8", errors="replace")
        else:
            try:
                with open(vcd_path, "rb") as f:  # type: ignore[arg-type]
                    f.seek(last_offset)
                    chunk = f.read().decode("utf-8", errors="replace")
                    last_offset = f.tell()
            except (FileNotFoundError, PermissionError):
                chunk = ""

        if chunk:
            # Re-attach any residual partial line from the previous poll so
            # we don't mis-parse a "#<ts> 0" that's actually "#<ts> 0!"
            combined = residual + chunk
            # Keep the last (possibly partial) line as new residual
            if "\n" in combined:
                *complete, residual = combined.split("\n")
                text_to_parse = "\n".join(complete)
            else:
                text_to_parse = ""
                residual = combined
            if text_to_parse:
                _consume_vcd_chunk(text_to_parse, state, rising, falling,
                                   var_id_map)

        # Render
        if first_draw:
            print()  # start on a fresh line below any prior output
            first_draw = False
        else:
            # Move cursor up to overwrite the previous dashboard frame
            sys.stdout.write(CURSOR_UP(dashboard_height))
            sys.stdout.flush()

        # Header line: elapsed/remaining + edge totals
        total_edges = sum(rising.values()) + sum(falling.values())
        header = (f"  {BOLD}Capturing{RESET}  "
                  f"{DIM}{elapsed:5.1f}s / {duration_s:5.1f}s{RESET}  "
                  f"{DIM}edges: {total_edges}{RESET}")
        print(f"{CLEAR_LINE}{header}")

        for ch in sorted(var_id_map.values(), key=lambda x: x[0]):
            ch_num, _ = ch
            s = state.get(ch_num, False)
            label = labels.get(ch_num, f"ch{ch_num}")
            if len(label) > 28:
                label = label[:25] + "..."
            n_r = rising.get(ch_num, 0)
            n_f = falling.get(ch_num, 0)
            state_str = f"{GREEN}HIGH{RESET}" if s else f"{DIM}LOW {RESET}"
            bar = _waveform_char(s) * bar_width
            edge_str = (f"  {DIM}↑{n_r} ↓{n_f}{RESET}"
                        if (n_r or n_f) else f"  {DIM}(no edges){RESET}")
            print(f"{CLEAR_LINE}  ch{ch_num} {DIM}│{RESET} "
                  f"{label:<28s} {DIM}│{RESET} {state_str}  {bar}{edge_str}")

        if remaining <= 0:
            break
        if should_stop is not None and should_stop():
            break
        time.sleep(poll_interval_s)

    print()  # newline after final frame
    return {"state": dict(state), "rising": dict(rising), "falling": dict(falling),
            "var_id_map": dict(var_id_map), "timescale_factor_ns": ts_factor}
