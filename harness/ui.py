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
import time
from pathlib import Path
from typing import Any, Optional

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
