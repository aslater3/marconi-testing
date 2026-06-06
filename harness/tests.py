"""Test definitions for the Marconi 2019A harness.

Each test is a list of steps. A step is a dict with a 'type' field. Supported types:

    {"type": "prompt",   "id": "...", "text": "...", "wait_for": "enter"|"key"|None}
    {"type": "clip",     "id": "...", "channels": [0,1,2], "label": "...",
                         "probes": {0: "IC11.Pin18 'C' addr line", ...},
                         "wait_for": "enter"}
    {"type": "press",    "id": "...", "button": "STORE", "expected": "..."}
    {"type": "capture",  "id": "...", "duration_s": 2.0, "sample_rate_hz": 24000000,
                         "channels": [0,1,2,3,4,5,6,7], "trigger": "..."}
    {"type": "analyse",  "id": "...", "kind": "bus_census"|"contention"|"diff"|"n_way_diff"|"analogue_vs_code",
                         "params": {...}}
    {"type": "note",     "id": "...", "text": "..."}     # operator observation
    {"type": "set_state","id": "...", "key": "...", "value": "..."}  # pin a fact
    {"type": "set_state","id": "...", "key": "...", "measurement": {
        "channel": "AC4.TP2", "code": 1024, "expected": 2.5,
        "tolerance_pct": 2.0, "unit": "V", "_default_value": 0.620
    }}

Step IDs must be unique within a test. They are used as the 'event_id' in the JSON output.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import json
from pathlib import Path


@dataclass
class TestDef:
    name: str
    description: str
    steps: list[dict]

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "description": self.description, "steps": self.steps},
                          indent=2)


# -----------------------------------------------------------------------------
# Test 1: Bus Census (the marquee test — what the LA was bought for)
# -----------------------------------------------------------------------------
BUS_CENSUS = TestDef(
    name="bus_census",
    description=(
        "Per-channel health check on the AA2/1 address bus. For each recipient board, "
        "isolate the bus, capture, and compare to baseline. Identifies stuck-at, "
        "high-impedance, and contention lines."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== BUS CENSUS ===\n"
            "Goal: identify which address/data line is the contention source.\n"
            "We'll baseline AA2/1 alone, then add each recipient board one at a time "
            "and watch for the shark-fin return.\n\n"
            "PRE-REQUISITES:\n"
            "  - 2019A powered OFF and unplugged from mains\n"
            "  - Logic analyser plugged in, driver loaded (fx2lafw)\n"
            "  - Ground clip on AA2/1 GND (e.g. Pin 8 of any 74LSxx)\n"
            "  - 8 channel clips on the bus lines\n\n"
            "Press ENTER when ready to start."
        ), "wait_for": "enter"},

        # PROBE MAP NOTES (see probe-map-audit.md in the wiki for full audit):
        #  - IC11 (74LS244) buffers the ADDRESS bus; its Y outputs are on
        #    pins 2,4,6,8,12,14,16,18. The A/B/C <-> Y-output mapping is
        #    NOT documented in the wiki — to be confirmed from the 2019A
        #    schematic (page-014 Fig. 3) before relying on this map.
        #  - IC10 (74LS245) drives the DATA bus; A0-A7 are on pins
        #    2,5,7,10,12,15,17,20. This is the canonical data bus.
        #  - IC20 (74LS273) Pin 11 is CLK = data-valid strobe for external
        #    latches — useful as a CH7 trigger reference.
        #  - IC11.Pin9 is GND on a 20-pin DIP — DO NOT probe there.
        {"type": "clip", "id": "clip_baseline", "text": (
            "Clip the 8 LA channels to the AA2/1 bus:\n"
            "  - 8 channel clips as listed below\n"
            "  - 1 GND clip on AA2/1 GND (e.g. Pin 10 of any 74LSxx, or the AA2 TP_GND)\n\n"
            "NOTE on A/B/C labels: IC11's Y outputs carry the 3-bit address "
            "field, but which Y-output is A, B, or C is not in the wiki. "
            "Verify against page-014 Fig. 3 of the service manual before "
            "drawing diagnostic conclusions.\n"
        ),
         "channels": [0, 1, 2, 3, 4, 5, 6, 7],
         "probes": {
             0: "AA2/1 IC11.Pin18  (bus 'C' address line — the suspect; assumes Y7=C)",
             1: "AA2/1 IC11.Pin16  (bus 'B' address line — assumes Y6=B)",
             2: "AA2/1 IC11.Pin14  (bus 'A' address line — assumes Y5=A)",
             3: "AA2/1 IC10.Pin2   (data bus A0 = 74LS245 A-side bit 0)",
             4: "AA2/1 IC10.Pin5   (data bus A1 = 74LS245 A-side bit 1)",
             5: "AA2/1 IC10.Pin7   (data bus A2 = 74LS245 A-side bit 2)",
             6: "AA2/1 IC10.Pin10  (data bus A3 = 74LS245 A-side bit 3)",
             7: "AA2/1 IC20.Pin11  (74LS273 CLK = data-valid strobe for ext. latches)",
         },
         "wait_for": "enter"},

        {"type": "prompt", "id": "isolate_main", "text": (
            "STEP 1 of 5: BASELINE — mainboard alone.\n"
            "ACTION: Disconnect the PLAA ribbon cable at the AA2/1 end.\n"
            "         Power ON the 2019A. Do NOT press any front-panel buttons yet.\n"
            "         The CPU should be idling, no bus transactions.\n\n"
            "Press ENTER to capture 2 seconds of idle state."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_baseline_idle", "duration_s": 2.0,
         "sample_rate_hz": 1_000_000, "channels": list(range(8)),
         "trigger": None},

        {"type": "prompt", "id": "trigger_main", "text": (
            "Now we need bus activity. Press the front-panel button:\n"
            "  1) Press 'SECOND FUNCT' then '3' (enter Second Function 3 — DAC manual mode)\n"
            "  2) Press '7' '0' '2' (select A7L2 — fine attenuator low byte)\n"
            "  3) Press 'STORE' (this writes a DAC update and triggers a full bus transaction)\n\n"
            "Press ENTER immediately AFTER pressing STORE so we capture the transaction."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_baseline_active", "duration_s": 2.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 7, "edge": "rising"}},

        {"type": "analyse", "id": "ana_baseline", "kind": "bus_census",
         "params": {"reference": "self", "expect_quiescent": "cap_baseline_idle"}},

        {"type": "prompt", "id": "add_ac13", "text": (
            "STEP 2 of 5: RECONNECT AC13.\n"
            "ACTION: Power OFF, reconnect PLAA ribbon cable to AA2/1.\n"
            "         AC13 should be plugged in. ALL other recipient boards (AC14, AD2, AD4, etc.) "
            "         should be UNPLUGGED from the backplane.\n"
            "         Power ON.\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_with_ac13", "duration_s": 2.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 7, "edge": "rising"}},

        {"type": "analyse", "id": "ana_with_ac13", "kind": "bus_census",
         "params": {"reference": "cap_baseline_active"}},

        {"type": "prompt", "id": "result_ac13", "text": (
            "RESULT for AC13:\n"
            "  - If 'C' line shape is clean (square wave): AC13 is innocent.\n"
            "  - If 'C' line shows the shark-fin (sharp rise + linear droop): AC13 is the source.\n"
            "Look at the dashboard above for the verdict.\n"
            "Press ENTER to record your observation."
        ), "wait_for": "enter"},

        {"type": "note", "id": "obs_ac13", "prompt": "Record your observation about AC13 (one line):"},

        {"type": "prompt", "id": "add_ad4", "text": (
            "STEP 3 of 5: ADD AD4 (the keyboard assembly — the prime suspect per the service manual).\n"
            "ACTION: Power OFF. Plug in AD4 on top of AC13.\n"
            "         Power ON.\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_with_ad4", "duration_s": 2.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 7, "edge": "rising"}},

        {"type": "analyse", "id": "ana_with_ad4", "kind": "bus_census",
         "params": {"reference": "cap_with_ac13"}},

        {"type": "prompt", "id": "result_ad4", "text": (
            "RESULT for AD4:\n"
            "  - If 'C' line shape degenerated significantly vs. AC13-only: AD4 is the source.\n"
            "  - This is the prime suspect per the service manual (74LS138 on AD4 IC1 has its\n"
            "    Pin 3 connected to the 'C' address line).\n"
            "Press ENTER to record."
        ), "wait_for": "enter"},

        {"type": "note", "id": "obs_ad4", "prompt": "AD4 observation:"},

        {"type": "prompt", "id": "add_ad2", "text": (
            "STEP 4 of 5: ADD AD2 (display board — secondary suspect).\n"
            "ACTION: Plug in AD2.\n"
            "         Power ON.\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_with_ad2", "duration_s": 2.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 7, "edge": "rising"}},

        {"type": "analyse", "id": "ana_with_ad2", "kind": "bus_census",
         "params": {"reference": "cap_with_ad4"}},

        {"type": "note", "id": "obs_ad2", "prompt": "AD2 observation:"},

        {"type": "prompt", "id": "all_boards", "text": (
            "STEP 5 of 5: FULLY ASSEMBLED 2019A.\n"
            "ACTION: Plug in all remaining recipient boards.\n"
            "         This is the normal operating state.\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_all_boards", "duration_s": 2.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 7, "edge": "rising"}},

        {"type": "analyse", "id": "ana_all_boards", "kind": "bus_census",
         "params": {"reference": "cap_with_ad2"}},
        {"type": "analyse", "id": "ana_all_boards_si", "kind": "signal_integrity",
         "params": {}},

        {"type": "note", "id": "obs_all", "prompt": "Final observation — which board caused the most degradation?"},
    ],
)


# -----------------------------------------------------------------------------
# Test 2: Contention Detector (focused on the 'C' line)
# -----------------------------------------------------------------------------
CONTENTION = TestDef(
    name="contention_detector",
    description=(
        "Focus capture on the 'C' address line and look for transitions that occur "
        "OUTSIDE expected windows — these are the smoking gun for two chips driving "
        "the same line in opposite directions."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== CONTENTION DETECTOR ===\n"
            "Target: the 'C' address line (AA2/1 IC11 Pin 18).\n"
            "This is the line that showed the 'shark fin' on the scope. The LA will show "
            "us the exact moment a second driver is fighting IC11.\n\n"
            "PRE-REQUISITES:\n"
            "  - 2019A fully assembled, all boards in place\n"
            "  - LA CH1 (sigrok D0) on AA2/1 IC11.Pin18 (the 'C' line)\n"
            "  - LA CH2 (sigrok D1) on any chip-select (e.g. AD4 IC1 Pin 15, /Y0 output of 74LS138)\n"
            "  - LA CH3-CH8 (sigrok D2-D7) on other 74LS138 Y-outputs or 74LS273 outputs\n"
            "  - All GND clips on AA2/1 GND\n\n"
            "Note: the Saleae Logic 8 labels its physical inputs CH1..CH8, but sigrok\n"
            "names them D0..D7 in VCD output. The harness uses D0..D7 internally;\n"
            "the display shows LA-side CH1..CH8 to match the silkscreen.\n\n"
            "When the capture starts, you have 5 seconds to provoke the fault. Do this:\n"
            "  1) Press a few different front-panel keys (e.g. STEP UP, STEP DOWN, RANGE, MODULATION)\n"
            "  2) Watch the LA activity LEDs (if any)\n"
            "  3) Try to provoke the fault — toggle a 1 dB step a few times\n\n"
            "Read the instructions above, then press ENTER to begin the 5-second capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_contention", "duration_s": 5.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": None},

        {"type": "prompt", "id": "press_buttons", "text": (
            "The LA captured 5 seconds of activity. Press ENTER to analyse."
        ), "wait_for": "enter"},

        {"type": "analyse", "id": "ana_contention", "kind": "contention",
         "params": {"suspect_channel": 0, "cs_channels": [1, 2, 3]}},
        {"type": "analyse", "id": "ana_contention_si", "kind": "signal_integrity",
         "params": {}},

        {"type": "note", "id": "obs", "prompt": "What did you observe (any clicks, pops, display glitches)?",
         "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 3: Good-vs-Bad Diff (capture both a working and a suspect transaction)
# -----------------------------------------------------------------------------
GOOD_VS_BAD = TestDef(
    name="good_vs_bad_diff",
    description=(
        "Capture a known-good DAC write, then capture a suspect one, and let the "
        "harness diff them sample-by-sample. The first divergence is your answer."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== GOOD vs BAD DIFF ===\n"
            "Two captures:\n"
            "  (A) GOOD: a DAC write that produces the correct level change (verify with DMM at TP2)\n"
            "  (B) BAD:  a DAC write that produces no level change / wrong level\n\n"
            "The diff will show you the first sample where the two captures diverge, "
            "and on which channel. That's the smoking gun.\n\n"
            "Press ENTER to start."
        ), "wait_for": "enter"},

        {"type": "clip", "id": "clip", "text": "Clip 8 channels to the bus + strobes:",
         "channels": list(range(8)),
         "probes": {
             0: "AA2/1 IC11.Pin18 (bus 'C' address line)",
             1: "AA2/1 IC11.Pin16 (bus 'B' address line)",
             2: "AA2/1 IC11.Pin14 (bus 'A' address line)",
             3: "AC4 AD7522.Pin24 LB strobe (latch low byte)",
             4: "AC4 AD7522.Pin25 HB strobe (latch high byte)",
             5: "AC4 AD7522.Pin21 LDAC (load DAC)",
             6: "any CS line (e.g. AD4 IC1.Pin15 /Y0)",
             7: "any 10 dB step CS (e.g. AD2 IC2 7406 output)",
         },
         "wait_for": "enter"},

        {"type": "prompt", "id": "good_setup", "text": (
            "CAPTURE A (GOOD):\n"
            "  1) DMM on AC4 TP2, set to mV DC\n"
            "  2) Set level to 0 dBm (or any level that produces a stable TP2 reading)\n"
            "  3) Note the TP2 voltage (e.g. 2.500 V)\n"
            "  4) Press STORE to latch\n"
            "  5) Press ENTER to capture 1 second of the transaction\n"
            "  6) Verify TP2 is stable after the write\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_good", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 5, "edge": "rising"}},

        {"type": "set_state", "id": "good_voltage", "key": "good_tp2_voltage",
         "prompt": "What is the TP2 voltage now? (e.g. 2.500 V)"},

        {"type": "prompt", "id": "bad_setup", "text": (
            "CAPTURE B (BAD):\n"
            "  1) Change to a level that DOES NOT produce the expected TP2 change (e.g. any "
            "level where TP2 is wrong by >50 mV from the expected value, or doesn't change at all)\n"
            "  2) Press STORE to latch\n"
            "  3) Press ENTER to capture 1 second of the transaction\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_bad", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 5, "edge": "rising"}},

        {"type": "set_state", "id": "bad_voltage", "key": "bad_tp2_voltage",
         "prompt": "What is the TP2 voltage now? (e.g. 0.124 V — the wrong value)"},

        {"type": "analyse", "id": "ana_diff", "kind": "diff",
         "params": {"a": "cap_good", "b": "cap_bad"}},

        {"type": "note", "id": "obs", "prompt": "Notes (anything else you noticed):", "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 4: 74LS138 Isolation Test (confirm/rule out AD4 IC1)
# -----------------------------------------------------------------------------
LS138_ISOLATION = TestDef(
    name="ls138_isolation",
    description=(
        "Confirm or rule out AD4 IC1 (74LS138) as the source of the bus contention "
        "on the 'C' address line. Procedure: capture with 138 in circuit, then lift "
        "pin 3 of the 138 (electrically disconnecting it from the 'C' line), capture "
        "again, and diff. If contention disappears, the 138 is the source."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== 74LS138 ISOLATION TEST ===\n"
            "Hypothesis: AD4 IC1 (74LS138) is sinking current on its Pin 3 ('C' address line).\n\n"
            "Procedure:\n"
            "  1) Capture a DAC write with AD4 IC1 fully in circuit (CONTROL capture)\n"
            "  2) Power off, lift Pin 3 of AD4 IC1 with a fine tip\n"
            "  3) Power on, capture the same DAC write (TEST capture)\n"
            "  4) Harness diffs the two captures\n\n"
            "Verdict:\n"
            "  - 'C' line (LA CH1 / sigrok D0) goes from 'contention waveform' → 'clean square wave': 138 confirmed\n"
            "  - 'C' line unchanged: 138 is innocent, look elsewhere\n\n"
            "Press ENTER to begin."
        ), "wait_for": "enter"},

        {"type": "clip", "id": "clip", "text": "Clip 8 channels per the contention test map:",
         "channels": list(range(8)),
         "probes": {
             0: "AA2/1 IC11.Pin18 ('C' line — the contention victim)",
             1: "AA2/1 IC11.Pin16 ('B' line — control)",
             2: "AA2/1 IC11.Pin14 ('A' line — control)",
             3: "AC4 AD7522.Pin24 LB strobe",
             4: "AC4 AD7522.Pin25 HB strobe",
             5: "AC4 AD7522.Pin21 LDAC",
             6: "AD4 IC1.Pin15 /Y0 (74LS138 output A)",
             7: "AD4 IC1.Pin14 /Y1 (74LS138 output B)",
         },
         "wait_for": "enter"},

        {"type": "prompt", "id": "control_setup", "text": (
            "CONTROL CAPTURE: with AD4 IC1 in circuit.\n"
            "  1) Power on, enter Second Function 3\n"
            "  2) Set A7L2 = 0x00, press STORE\n"
            "  3) Verify TP2 shows the expected value (record it)\n"
            "  4) Press ENTER to capture 1 second of the bus transaction"
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_control", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 5, "edge": "rising"}},

        {"type": "set_state", "id": "control_tp2",
         "key": "control_tp2_voltage",
         "measurement": {
             "channel": "AC4.TP2",
             "code": 0,
             "expected": 0.0,
             "tolerance_pct": 2.0,
             "unit": "V",
             "_default_value": 0.124,
             "notes": "control: AD4 IC1 (138) fully in circuit"
         },
         "prompt": "DMM reading on TP2 (control, with 138 in circuit):"},

        {"type": "prompt", "id": "lift_pin", "text": (
            "LIFT PIN 3 OF AD4 IC1:\n"
            "  1) Power OFF\n"
            "  2) Locate AD4 IC1 (74LS138) — Pin 3 is the C address input\n"
            "  3) With a fine soldering tip, carefully lift Pin 3 off its pad\n"
            "     (just enough to break the electrical connection — don't break the pin!)\n"
            "  4) Power ON\n"
            "  5) Press ENTER when ready to test"
        ), "wait_for": "enter"},

        {"type": "prompt", "id": "test_setup", "text": (
            "TEST CAPTURE: with AD4 IC1 Pin 3 lifted.\n"
            "  1) Enter Second Function 3 (same as control)\n"
            "  2) Set A7L2 = 0x00, press STORE\n"
            "  3) Verify TP2 reading (it should change vs control if 138 was the source)\n"
            "  4) Press ENTER to capture 1 second of the bus transaction"
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_test", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 5, "edge": "rising"}},

        {"type": "set_state", "id": "test_tp2",
         "key": "test_tp2_voltage",
         "measurement": {
             "channel": "AC4.TP2",
             "code": 0,
             "expected": 0.0,
             "tolerance_pct": 2.0,
             "unit": "V",
             "_default_value": 7.0,
             "notes": "test: AD4 IC1 Pin 3 lifted"
         },
         "prompt": "DMM reading on TP2 (test, with 138 pin 3 lifted):"},

        {"type": "analyse", "id": "ana_diff", "kind": "diff",
         "params": {"a": "cap_control", "b": "cap_test"}},

        {"type": "prompt", "id": "verdict", "text": (
            "VERDICT — 74LS138 ISOLATION:\n"
            "  - If the diff shows ch0 (the 'C' line) as the FIRST divergence with substantially\n"
            "    fewer edges in the test capture: 74LS138 IS THE SOURCE. Replace it.\n"
            "  - If ch0 looks the same: 138 is innocent. Look at the 74LS273 (AD2 IC1, A6L10)\n"
            "    next, or the AD7522LN itself.\n"
            "Press ENTER to record the conclusion."
        ), "wait_for": "enter"},

        {"type": "note", "id": "verdict_note",
         "prompt": "Verdict — is AD4 IC1 (74LS138) the source? (yes / no / inconclusive):",
         "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 5: 74LS273 Stuck-Bit Sequence (detect a latch that doesn't update)
# -----------------------------------------------------------------------------
LS273_SEQUENCE = TestDef(
    name="ls273_sequence",
    description=(
        "Detect a stuck or non-updating 74LS273 output bit. The procedure writes three "
        "different single-bit codes (1, 2, 4) via Second Function 3 and captures the bus "
        "for each. An N-way diff checks that each output bit toggles when it should."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== 74LS273 STUCK-BIT SEQUENCE ===\n"
            "Hypothesis: a 74LS273 latch (likely AD2 IC1, the heat-stressed A6L10 latch) "
            "has an output that doesn't update on clock edges.\n\n"
            "Procedure:\n"
            "  1) Enter Second Function 3, select A7L2 (low byte)\n"
            "  2) Write code 1 (binary 00000001), STORE, capture bus\n"
            "  3) Write code 2 (binary 00000010), STORE, capture bus\n"
            "  4) Write code 4 (binary 00000100), STORE, capture bus\n"
            "  5) Harness does an N-way diff to verify each bit toggles correctly\n\n"
            "Press ENTER to start."
        ), "wait_for": "enter"},

        # PROBE MAP NOTES (see probe-map-audit.md in the wiki for full audit):
        #  - IC11 (74LS244) is the ADDRESS buffer, not the data buffer.
        #  - IC10 (74LS245) drives the DATA bus. A0-A4 are on pins
        #    2, 5, 7, 10, 12. The harness originally clipped CH0-CH4 to
        #    IC11 (wrong IC, and IC11.Pin9 is GND on a 20-pin DIP) — fixed.
        #  - "D0 data line — 273 output bit 0" labels in the original map
        #    were misleading: the 74LS245 drives data ONTO the bus, but the
        #    suspect 74LS273 is on AD2 (the A6L10 latch), not on AA2/1.
        #    The probe here is "data bus A0 bit, while a 273 write happens
        #    downstream on AD2". The harness watches for the bit toggling
        #    correctly as the test writes codes 1, 2, 4 through Second
        #    Function 3.
        {"type": "clip", "id": "clip", "text": (
            "Clip 8 channels to the data bus + strobes:\n"
            "  - 8 channel clips as listed below\n"
            "  - 1 GND clip on AA2/1 GND (e.g. Pin 10 of any 74LSxx, or AA2 TP_GND)\n"
        ),
         "channels": list(range(8)),
         "probes": {
             0: "AA2/1 IC10.Pin2   (data bus A0 = 74LS245 A-side bit 0)",
             1: "AA2/1 IC10.Pin5   (data bus A1 = 74LS245 A-side bit 1)",
             2: "AA2/1 IC10.Pin7   (data bus A2 = 74LS245 A-side bit 2)",
             3: "AA2/1 IC10.Pin10  (data bus A3 = 74LS245 A-side bit 3)",
             4: "AA2/1 IC10.Pin12  (data bus A4 = 74LS245 A-side bit 4)",
             5: "AC4 AD7522.Pin24  (LB strobe — latches A0-A7 on the DAC)",
             6: "AC4 AD7522.Pin21  (LDAC — loads the DAC from the latched byte)",
             7: "GND (reference — clip to AA2/1 GND)",
         },
         "wait_for": "enter"},

        {"type": "prompt", "id": "write_1", "text": (
            "WRITE CODE 1 (binary 00000001):\n"
            "  - In Second Function 3, select A7L2\n"
            "  - Enter 00000001\n"
            "  - Press STORE\n"
            "  - Press ENTER to capture 0.5 second"
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_code_1", "duration_s": 0.5,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 6, "edge": "rising"}},

        {"type": "prompt", "id": "write_2", "text": (
            "WRITE CODE 2 (binary 00000010):\n"
            "  - Enter 00000010\n"
            "  - Press STORE\n"
            "  - Press ENTER to capture 0.5 second"
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_code_2", "duration_s": 0.5,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 6, "edge": "rising"}},

        {"type": "prompt", "id": "write_4", "text": (
            "WRITE CODE 4 (binary 00000100):\n"
            "  - Enter 00000100\n"
            "  - Press STORE\n"
            "  - Press ENTER to capture 0.5 second"
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_code_4", "duration_s": 0.5,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 6, "edge": "rising"}},

        # The N-way diff analyser expects: {captures: [id1, id2, id3], expect_levels: {ch: [v0, v1, v2]}}
        {"type": "analyse", "id": "ana_nway", "kind": "n_way_diff",
         "params": {
             "captures": ["cap_code_1", "cap_code_2", "cap_code_4"],
             "expect_levels": {
                 # Code 1 sets D0, code 2 sets D1, code 4 sets D2.
                 # At idle (just after STORE), D0/D1/D2 should match the code bits.
                 # D3-D4 should be 0 throughout.
                 0: [1, 0, 0],   # D0: high for code 1, low otherwise
                 1: [0, 1, 0],   # D1: high for code 2
                 2: [0, 0, 1],   # D2: high for code 4
                 3: [0, 0, 0],   # D3: always low
                 4: [0, 0, 0],   # D4: always low
             }
         }},

        {"type": "note", "id": "verdict_note",
         "prompt": "Verdict — any stuck 74LS273 bits? (record final_state for each channel):",
         "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 6: DAC DMM Cross-Check (detect a faulty AD7522LN)
# -----------------------------------------------------------------------------
DAC_DMM_CROSSCHECK = TestDef(
    name="dac_dmm_crosscheck",
    description=(
        "Cross-check the AD7522LN DAC by writing known codes via Second Function 3 and "
        "measuring TP2 with a DMM. If digital inputs are clean but the analog output is "
        "wrong, the DAC itself is faulty."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== DAC DMM CROSS-CHECK ===\n"
            "Hypothesis: the AD7522LN DAC has an internal latch fault — digital inputs "
            "look clean, but analog output is wrong.\n\n"
            "Procedure:\n"
            "  1) DMM on AC4 TP2 (mV DC mode). DMM negative (COM) lead to AC4 TP_GND "
            "(any AC4 GND test point is fine, or Pin 7 of any AC4 74LSxx IC).\n"
            "  2) For each code {0, 1024, 2048, 4095} (12-bit):\n"
            "     - Enter the code, press STORE\n"
            "     - Wait ~100ms for settling\n"
            "     - Read DMM\n"
            "  3) Harness compares each reading to the expected voltage (within 2%)\n\n"
            "Expected output range (assuming 10V reference and R-2R ladder):\n"
            "  - Code 0:    0.000 V\n"
            "  - Code 1024: ~2.500 V (quarter scale)\n"
            "  - Code 2048: ~5.000 V (half scale)\n"
            "  - Code 4095: ~9.998 V (full scale)\n\n"
            "Press ENTER to start."
        ), "wait_for": "enter"},

        {"type": "set_state", "id": "tp2_code_0", "key": "tp2_code_0",
         "measurement": {
             "channel": "AC4.TP2", "code": 0,
             "expected": 0.0, "tolerance_pct": 2.0, "unit": "V",
             "_default_value": 0.000,
             "notes": "DAC code 0 — expect 0.000V"
         },
         "prompt": "DMM reading for code 0:"},

        {"type": "prompt", "id": "write_1024", "text": (
            "WRITE CODE 1024 (quarter scale, binary 0000010000000000):\n"
            "  - In Second Function 3, select A7L2 then A7L3\n"
            "  - Low byte  = 00000000\n"
            "  - High byte = 00000100\n"
            "  - Press STORE\n"
            "  - Wait 100ms\n"
            "  - Read DMM (record below)"
        ), "wait_for": "enter"},

        {"type": "set_state", "id": "tp2_code_1024", "key": "tp2_code_1024",
         "measurement": {
             "channel": "AC4.TP2", "code": 1024,
             "expected": 2.500, "tolerance_pct": 2.0, "unit": "V",
             "_default_value": 0.620,
             "notes": "DAC code 1024 — expect 2.500V"
         },
         "prompt": "DMM reading for code 1024:"},

        {"type": "prompt", "id": "write_2048", "text": (
            "WRITE CODE 2048 (half scale):\n"
            "  - Low byte  = 00000000\n"
            "  - High byte = 00001000\n"
            "  - Press STORE, wait 100ms, read DMM"
        ), "wait_for": "enter"},

        {"type": "set_state", "id": "tp2_code_2048", "key": "tp2_code_2048",
         "measurement": {
             "channel": "AC4.TP2", "code": 2048,
             "expected": 5.000, "tolerance_pct": 2.0, "unit": "V",
             "_default_value": 1.240,
             "notes": "DAC code 2048 — expect 5.000V"
         },
         "prompt": "DMM reading for code 2048:"},

        {"type": "prompt", "id": "write_4095", "text": (
            "WRITE CODE 4095 (full scale):\n"
            "  - Low byte  = 11111111\n"
            "  - High byte = 00001111\n"
            "  - Press STORE, wait 100ms, read DMM"
        ), "wait_for": "enter"},

        {"type": "set_state", "id": "tp2_code_4095", "key": "tp2_code_4095",
         "measurement": {
             "channel": "AC4.TP2", "code": 4095,
             "expected": 9.998, "tolerance_pct": 2.0, "unit": "V",
             "_default_value": 2.480,
             "notes": "DAC code 4095 — expect 9.998V"
         },
         "prompt": "DMM reading for code 4095:"},

        {"type": "analyse", "id": "ana_dac", "kind": "analogue_vs_code",
         "params": {
             "measurements_key": "tp2",
             # Map: code -> expected_v. The harness reads back the values from sticky_state.
             "expected_table": {
                 0:    0.0,
                 1024: 2.500,
                 2048: 5.000,
                 4095: 9.998,
             },
             "tolerance_pct": 2.0,
         }},

        {"type": "note", "id": "verdict_note",
         "prompt": "Verdict — is the AD7522LN DAC faulty? (yes / no / inconclusive):",
         "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 7: End-to-End CPU→AC4 Bus Verification
# -----------------------------------------------------------------------------
# Walks the operator through the FULL 4-stage pipeline that a Second Function 3
# byte write to A7L2 (or A7L3) traverses, from the 8085 CPU all the way to the
# AD7522LN DAC. Four captures, one per stage, plus a final DMM cross-check
# of the analog output. The harness produces a stage-by-stage verdict table
# that pinpoints which stage the bus transaction first goes wrong.
#
# Channel map (same for all 4 stages — operator clips once):
#   CH0  IC5.30  ALE       (CPU: address latch enable)
#   CH1  IC5.31  /WR       (CPU: write strobe)
#   CH2  IC5.37  CLK       (CPU: 3.072 MHz system clock)
#   CH3  IC21.Q  A0 latched (buffer: demuxed address)
#   CH4  IC11.18 'C' addr  (buffer: address line, the shark-fin target)
#   CH5  AC3/ACL3 IC1.Y1  A7L1 (decoder: 74LS138 output for the target latch)
#   CH6  AC4 IC6.24 LB     (DAC: low-byte strobe)
#   CH7  AC4 IC6.21 LDAC   (DAC: load-DAC strobe)
#
# Note: in simulate mode, the synthetic VCD emulates a clean path with
# a default fault tag, or the operator can set --simulate-pattern
# e2e_cpu_to_ac4:<fault> to inject a specific fault at a specific stage.
# -----------------------------------------------------------------------------


# Common 8-channel probe map (used in all 4 stage clip steps)
E2E_PROBE_MAP: dict = {
    0: "AA2/1 IC5.Pin30  ALE  (8085 address latch enable)",
    1: "AA2/1 IC5.Pin31  /WR  (8085 write strobe — active LOW)",
    2: "AA2/1 IC5.Pin37  CLK  (8085 system clock, 3.072 MHz)",
    3: "AA2/1 IC21.Q     A0   (latched lower address from demuxer)",
    4: "AA2/1 IC11.Pin18 'C'  (address line — the bus contention target)",
    5: "AC3/ACL3 IC1.Pin15 /Y1  A7L1  (74LS138 Y-output for the target latch)",
    6: "AC4 IC6.Pin24  LB   (AD7522 low-byte strobe)",
    7: "AC4 IC6.Pin21  LDAC (AD7522 load-DAC strobe)",
}


BUS_E2E_CPU_TO_AC4 = TestDef(
    name="bus_e2e_cpu_to_ac4",
    description=(
        "End-to-end CPU→AC4 bus verification. Walks the operator through 4 captures, "
        "one per stage of the A7L2/A7L3 write pipeline (CPU → buffer → decoder → DAC), "
        "and produces a stage-by-stage verdict table that pinpoints which stage the "
        "transaction first goes wrong. Optionally cross-checks the analog TP2 voltage."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== END-TO-END CPU→AC4 BUS VERIFICATION ===\n"
            "\n"
            "This test walks the FULL pipeline a Second Function 3 byte write to\n"
            "A7L2 (or A7L3) traverses, from the 8085 CPU all the way to the\n"
            "AD7522LN DAC. Four captures, one per stage:\n"
            "\n"
            "  Stage 1 — CPU (IC5 P8085A):\n"
            "    IC5.30 ALE | IC5.31 /WR | IC5.37 CLK\n"
            "\n"
            "  Stage 2 — Buffer (IC10 74LS245 + IC11 74LS244):\n"
            "    IC21.Q A0 latched | IC11.18 'C' address line\n"
            "\n"
            "  Stage 3 — Decoder (AC3/ACL3 IC1 74LS138):\n"
            "    IC1.Y? A7L? (the Y-output for the target latch)\n"
            "\n"
            "  Stage 4 — DAC (AC4 IC6 AD7522):\n"
            "    IC6.24 LB | IC6.21 LDAC\n"
            "\n"
            "Each capture is 1 second at 24 MHz, triggered on the /WR strobe.\n"
            "Same 8-channel probe map is used for all 4 stages — clip once,\n"
            "leave the clips on, and run the test.\n"
            "\n"
            "The harness produces a stage-by-stage verdict table. The first\n"
            "stage that fails tells you where in the pipeline the bus\n"
            "transaction is going wrong.\n"
            "\n"
            "PRE-REQUISITES:\n"
            "  - 2019A powered OFF, lid off, ESD strap on\n"
            "  - Logic analyser plugged in, driver loaded (fx2lafw)\n"
            "  - DMM on AC4 TP2 (mV DC mode)\n"
            "  - Service manual open to page-014 (Fig. 3) and page-097 (Table 24)\n"
            "\n"
            "Press ENTER when ready to start."
        ), "wait_for": "enter"},

        {"type": "clip", "id": "clip_all", "text": (
            "Clip the 8 LA channels ONCE — the same probe map is used for all 4 stages."
        ),
         "channels": list(range(8)),
         "probes": E2E_PROBE_MAP,
         "wait_for": "enter"},

        # ------------------------------------------------------------------
        # Stage 1 — CPU
        # ------------------------------------------------------------------
        {"type": "prompt", "id": "stage1_setup", "text": (
            "STAGE 1 of 4: CPU (IC5 P8085A)\n"
            "────────────────────────────────\n"
            "Trigger: rising edge of /WR (CH1).\n"
            "Goal: prove the CPU is alive and writing.\n"
            "\n"
            "Procedure:\n"
            "  1) Power on the 2019A\n"
            "  2) Enter Second Function 3 (press SECOND FUNCT, then 3)\n"
            "  3) Type 7, 0, 2 to select A7L2 (fine attenuator LSB)\n"
            "  4) Type the data byte — use 00000001 for this test\n"
            "  5) Press STORE\n"
            "  6) Press ENTER immediately after STORE to capture the bus\n"
            "\n"
            "Expected: you should see CLK pulses (CH2 ~3.072 MHz),\n"
            "ALE pulses (CH0), and a single /WR pulse (CH1 LOW)."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_stage1", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 1, "edge": "falling"},
         "_simulate_pattern": "e2e_cpu_to_ac4:clean"},

        # ------------------------------------------------------------------
        # Stage 2 — Buffer
        # ------------------------------------------------------------------
        {"type": "prompt", "id": "stage2_setup", "text": (
            "STAGE 2 of 4: BUFFER (IC10 74LS245 + IC11 74LS244)\n"
            "────────────────────────────────────────────────────\n"
            "Trigger: rising edge of /WR (CH1) — same as stage 1.\n"
            "Goal: prove the address has been demuxed and is on the bus.\n"
            "\n"
            "Procedure:\n"
            "  1) The probe is still on A0 (CH3) and 'C' line (CH4)\n"
            "  2) Press STORE again to repeat the same write\n"
            "  3) Press ENTER to capture\n"
            "\n"
            "Expected: A0 (CH3) should pulse HIGH as the latched address.\n"
            "The 'C' line (CH4) should toggle cleanly — this is the\n"
            "line that showed the 'shark fin' on the scope. A clean\n"
            "square wave means contention is absent in this capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_stage2", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 1, "edge": "falling"},
         "_simulate_pattern": "e2e_cpu_to_ac4:clean"},

        # ------------------------------------------------------------------
        # Stage 3 — Decoder
        # ------------------------------------------------------------------
        {"type": "prompt", "id": "stage3_setup", "text": (
            "STAGE 3 of 4: DECODER (AC3/ACL3 IC1 74LS138)\n"
            "──────────────────────────────────────────────\n"
            "Trigger: rising edge of the A7L? Y-output (CH5).\n"
            "Goal: prove the 74LS138 is decoding the address and\n"
            "asserting the correct Y-output (A7L1 for our test write).\n"
            "\n"
            "Procedure:\n"
            "  1) The probe is still on AC3/ACL3 IC1.Pin15 (Y1 = A7L1)\n"
            "  2) Press STORE again to repeat the same write\n"
            "  3) Press ENTER to capture\n"
            "\n"
            "Expected: A7L1 (CH5) should pulse LOW (active-LOW chip select)\n"
            "for the duration of the write cycle. If CH5 never goes LOW,\n"
            "the 74LS138 is dead or the address bits are not reaching it."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_stage3", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 5, "edge": "falling"},
         "_simulate_pattern": "e2e_cpu_to_ac4:clean"},

        # ------------------------------------------------------------------
        # Stage 4 — DAC
        # ------------------------------------------------------------------
        {"type": "prompt", "id": "stage4_setup", "text": (
            "STAGE 4 of 4: DAC (AC4 IC6 AD7522)\n"
            "────────────────────────────────────\n"
            "Trigger: falling edge of LB strobe (CH6).\n"
            "Goal: prove the data latched into the AD7522 input register\n"
            "and was transferred to the DAC output register via LDAC.\n"
            "\n"
            "Procedure:\n"
            "  1) The probes are still on LB (CH6) and LDAC (CH7)\n"
            "  2) Press STORE again to repeat the same write\n"
            "  3) Press ENTER to capture\n"
            "\n"
            "Expected: LB (CH6) should pulse LOW (the latching edge is\n"
            "the RISING edge of LB). LDAC (CH7) should pulse LOW AFTER\n"
            "LB has returned HIGH. The LB rise time should be <50 ns\n"
            "(if it's 200+ ns, that's the RC-filtered edge fault mode)."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_stage4", "duration_s": 1.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 6, "edge": "falling"},
         "_simulate_pattern": "e2e_cpu_to_ac4:clean"},

        # ------------------------------------------------------------------
        # Analog cross-check (TP2)
        # ------------------------------------------------------------------
        {"type": "set_state", "id": "tp2_after_write", "key": "tp2_after_write",
         "measurement": {
             "channel": "AC4.TP2",
             "code": 1,  # 0b00000001 = 1 LSB on the 10-bit field
             "expected": 0.010,  # 1 mV per LSB at the comparator (page-033)
             "tolerance_pct": 50.0,
             "unit": "V",
             "_default_value": 0.010,
             "notes": "After Second Function 3, A7L2 = 00000001, A7L3 = 00000000. "
                      "Per page-033: 1 LSB = 1 mV at the comparator (TP2)."
         },
         "prompt": "DMM reading on AC4 TP2 (after the 0b00000001 write):"},

        # ------------------------------------------------------------------
        # Analysis
        # ------------------------------------------------------------------
        {"type": "analyse", "id": "ana_e2e", "kind": "bus_e2e",
         "params": {
             "stage_captures": {
                 "stage1": "cap_stage1",
                 "stage2": "cap_stage2",
                 "stage3": "cap_stage3",
                 "stage4": "cap_stage4",
             },
             "expected_tp2_v": 0.010,
             "tp2_tolerance_pct": 50.0,
         }},

        {"type": "note", "id": "obs", "prompt": (
            "Free-form notes (first failing stage, work already done, next move):"
        ), "multiline": True},
    ],
)


# =============================================================================
# AA2/1 single-board tests
# =============================================================================
#
# These tests probe ONLY AA2/1 (the main CPU board). They are designed so the
# operator can run them with the unit in a single-board state — recipient
# boards (AC3, AC4, AC13, AD2, AD4, ...) need not be clipped. The probes
# listed in each test are all reachable on the AA2/1 PCB.
#
# Reference: wiki/projects/marconi-2019a/aa2-1-ic-inventory.md — the 6
# bus-relevant ICs (IC5 8085, IC10 74LS245, IC11 74LS244, IC13 74LS138,
# IC20 74LS273, IC21 74LS373) and their pin assignments.
#
# Common LA-side channel mapping (1-indexed, matches the Saleae silkscreen):
#   CH1 = sigrok D0   CH2 = D1   CH3 = D2   CH4 = D3
#   CH5 = D4   CH6 = D5   CH7 = D6   CH8 = D7

# -----------------------------------------------------------------------------
# Test 8: AA2/1 CPU Health (8085 clock + ALE + /WR)
# -----------------------------------------------------------------------------
# Probes IC5 (P8085A) only:
#   LA CH1 (D0) = IC5.37  CLK OUT   (3.072 MHz system clock)
#   LA CH2 (D1) = IC5.30  ALE       (address latch enable)
#   LA CH3 (D2) = IC5.31  /WR       (write strobe)
#   LA CH4 (D3) = IC5.32  /RD       (read strobe, for cross-check)
#   LA CH5 (D4) = IC5.34  IO/M      (IO vs memory cycle)
#   LA CH6 (D5) = IC5.21  S1        (bus status 1)
#   LA CH7 (D6) = IC5.22  S0        (bus status 0)
#   LA CH8 (D7) = GND      (reference — should be LOW throughout)
# Verdict: clock frequency within tolerance, ALE pulses occurring, /WR pulses
# occurring. If CLK is dead or wrong frequency, the 8085 is not running.
# -----------------------------------------------------------------------------
AA2_CPU_HEALTH = TestDef(
    name="aa2_cpu_health",
    description=(
        "AA2/1-only health check of the 8085 CPU (IC5). Probes CLK, ALE, /WR, "
        "/RD, S0, S1, and IO/M. Verifies the clock is at 3.072 MHz (within 5%) "
        "and that ALE and /WR are pulsing — proves the CPU is running and "
        "issuing bus cycles. No recipient-board access required."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== AA2/1 CPU HEALTH (IC5 P8085A) ===\n"
            "Goal: confirm the 8085 microprocessor on the mainboard is alive and\n"
            "running at the correct clock speed (3.072 MHz). All 8 probes are on\n"
            "AA2/1 IC5 — no other board needs to be touched.\n\n"
            "PROBE MAP (clip 8 LA channels to AA2/1 IC5 + GND):\n"
            "  LA CH1 (D0)  → AA2/1 IC5.Pin37  CLK OUT  (system clock, expect 3.072 MHz)\n"
            "  LA CH2 (D1)  → AA2/1 IC5.Pin30  ALE      (address latch enable)\n"
            "  LA CH3 (D2)  → AA2/1 IC5.Pin31  /WR      (write strobe — pulses LOW)\n"
            "  LA CH4 (D3)  → AA2/1 IC5.Pin32  /RD      (read strobe — mostly idle)\n"
            "  LA CH5 (D4)  → AA2/1 IC5.Pin34  IO/M     (IO vs memory cycle)\n"
            "  LA CH6 (D5)  → AA2/1 IC5.Pin21  S1       (bus status 1)\n"
            "  LA CH7 (D6)  → AA2/1 IC5.Pin22  S0       (bus status 0)\n"
            "  LA CH8 (D7)  → AA2/1 GND         GND      (reference, must stay LOW)\n\n"
            "ACTION: power ON the 2019A in a quiescent state (no key presses, no\n"
            "Second Function mode). Let the CPU free-run for 2 seconds while we\n"
            "capture the bus.\n\n"
            "Press ENTER to capture."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_cpu_health", "duration_s": 2.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": None},

        {"type": "analyse", "id": "ana_cpu_health", "kind": "clock_health",
         "params": {"channel": 0, "expected_hz": 3_072_000, "tolerance_pct": 5.0}},

        {"type": "analyse", "id": "ana_bus_health", "kind": "bus_census",
         "params": {"reference": "self"}},

        {"type": "note", "id": "obs", "prompt": (
            "Note the measured clock frequency and which lines are pulsing:\n"
            "  - Is CLK at ~3.072 MHz (within 5%)?     (verdict: see above)\n"
            "  - Is ALE pulsing regularly?\n"
            "  - Is /WR firing at all?\n"
            "  - Are any of the 8 channels stuck HIGH or LOW?"
        ), "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 9: AA2/1 Address Bus (IC21 74LS373 demuxer)
# -----------------------------------------------------------------------------
# Probes IC21 (74LS373) Q outputs + IC5 /WR (trigger):
#   LA CH1 (D0) = IC5.31  /WR          (trigger: falling edge)
#   LA CH2 (D1) = IC5.30  ALE          (qualifier for address phase)
#   LA CH3 (D2) = IC5.37  CLK          (clock reference)
#   LA CH4 (D3) = IC21.Pin19  Q0       (latched A0)
#   LA CH5 (D4) = IC21.Pin6   Q1       (latched A1)
#   LA CH6 (D5) = IC21.Pin4   Q2       (latched A2)
#   LA CH7 (D6) = IC21.Pin16  Q3       (latched A3)
#   LA CH8 (D7) = IC21.Pin14  Q4       (latched A4)
# ACTION: trigger on Second Function 3 button press (operator presses STORE
# after typing the data byte). Verify the latched address on Q0..Q4 matches
# the typed sub-address.
# -----------------------------------------------------------------------------
AA2_ADDRESS_BUS = TestDef(
    name="aa2_address_bus",
    description=(
        "AA2/1-only test of the address demuxer (IC21 74LS373). Probes Q0..Q4 "
        "outputs of IC21 alongside /WR, ALE, and CLK. Capture one Second Function "
        "3 byte write; verify the latched address is stable during /WR LOW and "
        "that Q0..Q4 transition when /WR is asserted. AA2/1 only — no recipient "
        "boards touched."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== AA2/1 ADDRESS BUS (IC21 74LS373) ===\n"
            "Goal: verify the address demuxer (IC21) is correctly latching the\n"
            "low-order address bits during every CPU bus cycle. We will capture\n"
            "a single Second Function 3 write and inspect the latched address.\n\n"
            "PROBE MAP:\n"
            "  LA CH1 (D0) → AA2/1 IC5.Pin31  /WR  (trigger — falling edge)\n"
            "  LA CH2 (D1) → AA2/1 IC5.Pin30  ALE  (address phase qualifier)\n"
            "  LA CH3 (D2) → AA2/1 IC5.Pin37  CLK  (3.072 MHz reference)\n"
            "  LA CH4 (D3) → AA2/1 IC21.Pin19  Q0  (latched address A0)\n"
            "  LA CH5 (D4) → AA2/1 IC21.Pin6   Q1  (latched A1)\n"
            "  LA CH6 (D5) → AA2/1 IC21.Pin4   Q2  (latched A2)\n"
            "  LA CH7 (D6) → AA2/1 IC21.Pin16  Q3  (latched A3)\n"
            "  LA CH8 (D7) → AA2/1 IC21.Pin14  Q4  (latched A4)\n\n"
            "ACTION (capture one Second Function 3 write to A7L2):\n"
            "  1) Set up the 2019A: power on, no Second Function yet\n"
            "  2) Press ENTER to begin the 5-second capture\n"
            "  3) When you see \"Capturing\" in the dashboard, press:\n"
            "       SECOND FUNCT → 3 → 7 → 0 → 0 → 0 → 0 → 0 → 0 → 0 → 0 → STORE\n"
            "     (this writes 0x00 to A7L2 — one byte)\n"
            "  4) Wait for the capture to end\n\n"
            "Press ENTER to begin."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_address_bus", "duration_s": 5.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 0, "edge": "falling"}},

        {"type": "analyse", "id": "ana_address_bus", "kind": "bus_census",
         "params": {"reference": "self"}},
        {"type": "analyse", "id": "ana_address_bus_si", "kind": "signal_integrity",
         "params": {}},

        {"type": "note", "id": "obs", "prompt": (
            "Examine the bus_census output:\n"
            "  - /WR (CH1) should have at least 2 transitions (one write cycle)\n"
            "  - ALE (CH2) should have at least 4 transitions (multiple bus cycles)\n"
            "  - Q0..Q4 (CH4..CH8) should each have at least 1 transition — if any\n"
            "    are stuck LOW or stuck HIGH, IC21 is suspect.\n"
            "  - Does the pattern of Q0..Q4 transitions correlate with /WR pulses?"
        ), "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 10: AA2/1 IC11 'C' line alone (the shark-fin localiser)
# -----------------------------------------------------------------------------
# Probes IC11 (74LS244) Y outputs — the address buffers. The 'C' line is on
# Pin 18. This is the AA2-only version of contention_detector: same target
# (the 'C' line), no cross-board probes.
#   LA CH1 (D0) = IC11.Pin18  Y7  ('C' line — the shark-fin target)
#   LA CH2 (D1) = IC11.Pin16  Y6  ('B' line — control)
#   LA CH3 (D2) = IC11.Pin14  Y5  ('A' line — control)
#   LA CH4 (D3) = IC11.Pin12  Y4
#   LA CH5 (D4) = IC11.Pin8   Y3
#   LA CH6 (D5) = IC11.Pin6   Y2
#   LA CH7 (D6) = IC11.Pin4   Y1
#   LA CH8 (D7) = IC11.Pin2   Y0
# All 8 Y outputs of IC11 — single IC, single board.
# -----------------------------------------------------------------------------
AA2_IC11_ALONE = TestDef(
    name="aa2_ic11_alone",
    description=(
        "AA2/1-only localiser for the 'C' line shark-fin fault. Probes all 8 Y "
        "outputs of IC11 (74LS244) — the address buffer that drives the bus "
        "downstream. The 'C' line is on Pin 18 (Y7). Cable-isolation test "
        "proved IC11 is healthy when its load is removed; this test re-checks "
        "with all boards in place, on AA2-side only, so you can decide whether "
        "the fault is on the AA2 side of IC11 or downstream. No cross-board "
        "clipping required."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== AA2/1 IC11 ALONE (74LS244 address buffer) ===\n"
            "Goal: observe all 8 buffered address lines on IC11 in isolation\n"
            "(no recipient boards need to be clipped). The 'C' line is on\n"
            "Pin 18 (Y7) — this is the line that showed the shark-fin on the\n"
            "scope. The cable-isolation test proved IC11 is healthy when its\n"
            "load is removed; this test re-checks with everything connected.\n\n"
            "PROBE MAP — all on AA2/1 IC11:\n"
            "  LA CH1 (D0) → AA2/1 IC11.Pin18  Y7  ('C' line — the target)\n"
            "  LA CH2 (D1) → AA2/1 IC11.Pin16  Y6  ('B' line — control)\n"
            "  LA CH3 (D2) → AA2/1 IC11.Pin14  Y5  ('A' line — control)\n"
            "  LA CH4 (D3) → AA2/1 IC11.Pin12  Y4\n"
            "  LA CH5 (D4) → AA2/1 IC11.Pin8   Y3\n"
            "  LA CH6 (D5) → AA2/1 IC11.Pin6   Y2\n"
            "  LA CH7 (D6) → AA2/1 IC11.Pin4   Y1\n"
            "  LA CH8 (D7) → AA2/1 IC11.Pin2   Y0\n\n"
            "ACTION (5 seconds of normal front-panel activity):\n"
            "  1) Press ENTER to begin the 5-second capture\n"
            "  2) During capture, press a few front-panel keys to provoke bus\n"
            "     activity: STEP UP, STEP DOWN, RANGE, MODULATION, frequency\n"
            "     select — anything that causes the CPU to write a latch\n"
            "  3) If the fault is intermittent, try toggling a 1 dB step a few\n"
            "     times — the A7L2 fine attenuator write will exercise the 'C' line\n\n"
            "Press ENTER to begin."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_ic11_alone", "duration_s": 5.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": None},

        {"type": "analyse", "id": "ana_ic11_alone", "kind": "bus_census",
         "params": {"reference": "self"}},
        {"type": "analyse", "id": "ana_ic11_alone_si", "kind": "signal_integrity",
         "params": {}},

        {"type": "note", "id": "obs", "prompt": (
            "Interpret the bus_census output:\n"
            "  - The 'C' line (CH1, Y7) should toggle during bus activity\n"
            "  - The 'B' line (CH2, Y6) and 'A' line (CH3, Y5) are the address\n"
            "    bits that select the latches — they should also toggle\n"
            "  - All 8 lines should show roughly comparable activity (any line\n"
            "    with zero edges is suspect — either IC11 pin is dead or the\n"
            "    line is stuck)\n"
            "  - Compare the activity pattern of the 'C' line to the 'A' and 'B'\n"
            "    lines. A shark-fin manifests as many rapid transitions on the\n"
            "    'C' line within a single bus cycle; on the scope you'd see a\n"
            "    rising edge followed by a slow RC droop rather than a clean fall."
        ), "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 11: AA2/1 IC20 Latch (74LS273 — replaced chip, verify replacement)
# -----------------------------------------------------------------------------
# IC20 was previously replaced (per wiki). This test verifies the
# replacement is functional by watching the latch capture a Second Function 3
# write.
#   LA CH1 (D0) = IC5.31  /WR
#   LA CH2 (D1) = IC5.30  ALE
#   LA CH3 (D2) = IC20.Pin11  CLK  (data-valid strobe for external latches)
#   LA CH4 (D3) = IC20.Pin3   D0  (data input 0)
#   LA CH5 (D4) = IC20.Pin4   D1
#   LA CH6 (D5) = IC20.Pin7   D2
#   LA CH7 (D6) = IC20.Pin8   D3
#   LA CH8 (D7) = IC20.Pin13  D4
# Verifies the CLK pin pulses and that D0..D4 carry data during /WR.
# -----------------------------------------------------------------------------
AA2_IC20_LATCH = TestDef(
    name="aa2_ic20_latch",
    description=(
        "AA2/1-only verification of IC20 (74LS273 octal D-latch). Probes the "
        "CLK input (Pin 11) and 5 of the 8 D inputs. The wiki notes IC20 was "
        "previously replaced and the replacement ruled out as the fault "
        "source — this test confirms the replacement is healthy. Drive it "
        "with a Second Function 3 write and verify CLK pulses and D0..D4 "
        "carry data during /WR."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== AA2/1 IC20 LATCH (74LS273 — previously replaced) ===\n"
            "Goal: verify the IC20 replacement is healthy by capturing a\n"
            "Second Function 3 write and checking that CLK pulses and the\n"
            "D0..D4 inputs carry data.\n\n"
            "PROBE MAP — all on AA2/1:\n"
            "  LA CH1 (D0) → AA2/1 IC5.Pin31   /WR  (CPU write strobe — trigger)\n"
            "  LA CH2 (D1) → AA2/1 IC5.Pin30   ALE  (address phase qualifier)\n"
            "  LA CH3 (D2) → AA2/1 IC20.Pin11  CLK  (latch data-valid strobe)\n"
            "  LA CH4 (D3) → AA2/1 IC20.Pin3   D0   (data input 0)\n"
            "  LA CH5 (D4) → AA2/1 IC20.Pin4   D1\n"
            "  LA CH6 (D5) → AA2/1 IC20.Pin7   D2\n"
            "  LA CH7 (D6) → AA2/1 IC20.Pin8   D3\n"
            "  LA CH8 (D7) → AA2/1 IC20.Pin13  D4\n\n"
            "ACTION (one Second Function 3 write to A7L2):\n"
            "  1) Power on, no Second Function yet\n"
            "  2) Press ENTER to begin 5-second capture\n"
            "  3) When dashboard shows \"Capturing\", press:\n"
            "       SECOND FUNCT → 3 → 7 → 0 → 1 → 0 → 1 → 0 → 1 → 0 → 1 → STORE\n"
            "     (this writes 0x55 to A7L2 — the LSB nibble toggles D0..D3)\n"
            "  4) Wait for capture to end\n\n"
            "Press ENTER to begin."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_ic20_latch", "duration_s": 5.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": {"channel": 0, "edge": "falling"}},

        {"type": "analyse", "id": "ana_ic20_latch", "kind": "bus_census",
         "params": {"reference": "self"}},

        {"type": "note", "id": "obs", "prompt": (
            "Interpret:\n"
            "  - IC20.CLK (CH3) should pulse at least once (the rising edge that\n"
            "    latches the data)\n"
            "  - D0..D3 (CH4..CH7) should each transition at least twice if the\n"
            "    bus_census sees the full 0x55 pattern (the bit pattern is\n"
            "    01010101 so each bit toggles four times during the byte)\n"
            "  - If CLK is silent, the 8085 is not generating latch strobes for\n"
            "    IC20 — but that is unlikely if /WR is firing."
        ), "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 12: AA2/1 IC21 Latch Detail (74LS373 — address demuxer detail)
# -----------------------------------------------------------------------------
# 8 channels all on IC21: D0..D7 inputs from the multiplexed AD0..AD7 bus,
# plus LE (= ALE).
#   LA CH1 (D0) = IC21.Pin3   D0  (input from 8085 AD0)
#   LA CH2 (D1) = IC21.Pin4   D1  (input from 8085 AD1)
#   LA CH3 (D2) = IC21.Pin7   D2
#   LA CH4 (D3) = IC21.Pin8   D3
#   LA CH5 (D4) = IC21.Pin13  D4
#   LA CH6 (D5) = IC21.Pin14  D5
#   LA CH7 (D6) = IC21.Pin17  D6
#   LA CH8 (D7) = IC21.Pin18  D7
# LE is wired to ALE — capture from a parallel pin (IC5.30). But we only
# have 8 channels. So we drop one D input to keep the LE probe — see
# aa2_address_bus for the /WR+ALE+CLK+5x Q variant. This test is the
# 8-D-input variant: pure data view of the demuxer.
# -----------------------------------------------------------------------------
AA2_IC21_LATCH = TestDef(
    name="aa2_ic21_latch",
    description=(
        "AA2/1-only 8-channel view of IC21 (74LS373) D inputs from the 8085's "
        "multiplexed AD0..AD7 bus. The D inputs carry address bits during the "
        "first half of every bus cycle (when ALE is HIGH) and data bits during "
        "the second half. Bus_census reports per-channel activity. AA2/1 only — "
        "no recipient boards touched."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== AA2/1 IC21 LATCH (74LS373 — 8 D-input view) ===\n"
            "Goal: capture the demultiplexer's 8 D inputs (AD0..AD7 from the\n"
            "8085) over 5 seconds of normal bus activity. Bus_census will\n"
            "report per-channel activity. All 8 channels are on AA2/1 IC21.\n\n"
            "PROBE MAP — all on AA2/1 IC21 (74LS373):\n"
            "  LA CH1 (D0) → AA2/1 IC21.Pin3   D0   (8085 AD0)\n"
            "  LA CH2 (D1) → AA2/1 IC21.Pin4   D1   (8085 AD1)\n"
            "  LA CH3 (D2) → AA2/1 IC21.Pin7   D2   (8085 AD2)\n"
            "  LA CH4 (D3) → AA2/1 IC21.Pin8   D3   (8085 AD3)\n"
            "  LA CH5 (D4) → AA2/1 IC21.Pin13  D4   (8085 AD4)\n"
            "  LA CH6 (D5) → AA2/1 IC21.Pin14  D5   (8085 AD5)\n"
            "  LA CH7 (D6) → AA2/1 IC21.Pin17  D6   (8085 AD6)\n"
            "  LA CH8 (D7) → AA2/1 IC21.Pin18  D7   (8085 AD7)\n\n"
            "ACTION: power ON, no key presses, just let the CPU free-run for\n"
            "5 seconds. The 8085 executes its monitor loop continuously — this\n"
            "is enough to produce a meaningful edge count per channel.\n\n"
            "Press ENTER to begin."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_ic21_latch", "duration_s": 5.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": None},

        {"type": "analyse", "id": "ana_ic21_latch", "kind": "bus_census",
         "params": {"reference": "self"}},

        {"type": "note", "id": "obs", "prompt": (
            "Interpret:\n"
            "  - D0..D7 (CH1..CH8) should all have edge counts in the same order\n"
            "    of magnitude. The 8085 reads program memory from IC1..IC4 and\n"
            "    so AD0..AD7 carry instruction bytes continuously — heavy\n"
            "    activity on all 8 channels is normal.\n"
            "  - A dead AD line (zero edges) would mean the 8085 is not driving\n"
            "    that bit, or IC21's input pin is broken.\n"
            "  - A wildly asymmetric edge count (e.g. D7 has 10x more edges\n"
            "    than D0) is normal — different bits toggle at different rates\n"
            "    in code. What's NOT normal is zero edges on any one line."
        ), "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Test 13: AA2/1 IC10 Transceiver (74LS245 — bus xcvr)
# -----------------------------------------------------------------------------
# Probes IC10 (74LS245): 4 of the 8 A-side pins + 4 control/status.
#   LA CH1 (D0) = IC10.Pin1   DIR   (direction: HIGH=AA2 drives bus)
#   LA CH2 (D1) = IC10.Pin19  /OE   (output enable — should be LOW)
#   LA CH3 (D2) = IC10.Pin2   A0    (data bit 0, motherboard side)
#   LA CH4 (D3) = IC10.Pin5   A1
#   LA CH5 (D4) = IC10.Pin7   A2
#   LA CH6 (D5) = IC10.Pin10  A3
#   LA CH7 (D6) = IC5.31      /WR   (qualifier — DIR should follow /WR)
#   LA CH8 (D7) = IC5.30      ALE   (qualifier)
# -----------------------------------------------------------------------------
AA2_IC10_XCVR = TestDef(
    name="aa2_ic10_xcvr",
    description=(
        "AA2/1-only health check of IC10 (74LS245) bus transceiver. Probes "
        "DIR, /OE, A0..A3, plus /WR and ALE as qualifiers. The DIR line "
        "should be HIGH during CPU writes (AA2 driving the cable) and LOW "
        "during reads. /OE should be LOW (enabled) for the duration of any "
        "active bus cycle. AA2/1 only — no recipient boards touched."
    ),
    steps=[
        {"type": "prompt", "id": "intro", "text": (
            "=== AA2/1 IC10 BUS TRANSCEIVER (74LS245) ===\n"
            "Goal: verify the bus transceiver is correctly driving the ribbon\n"
            "cable during writes. DIR should follow /WR; /OE should be LOW\n"
            "throughout active cycles.\n\n"
            "PROBE MAP — on AA2/1 IC10 + IC5 (control signals):\n"
            "  LA CH1 (D0) → AA2/1 IC10.Pin1   DIR  (direction control)\n"
            "  LA CH2 (D1) → AA2/1 IC10.Pin19  /OE  (output enable — expect LOW)\n"
            "  LA CH3 (D2) → AA2/1 IC10.Pin2   A0   (motherboard-side data 0)\n"
            "  LA CH4 (D3) → AA2/1 IC10.Pin5   A1\n"
            "  LA CH5 (D4) → AA2/1 IC10.Pin7   A2\n"
            "  LA CH6 (D5) → AA2/1 IC10.Pin10  A3\n"
            "  LA CH7 (D6) → AA2/1 IC5.Pin31   /WR  (CPU write strobe — qualifier)\n"
            "  LA CH8 (D7) → AA2/1 IC5.Pin30   ALE  (address phase qualifier)\n\n"
            "ACTION: power on, no key presses. Capture 5 seconds of idle.\n\n"
            "Press ENTER to begin."
        ), "wait_for": "enter"},

        {"type": "capture", "id": "cap_ic10_xcvr", "duration_s": 5.0,
         "sample_rate_hz": 24_000_000, "channels": list(range(8)),
         "trigger": None},

        {"type": "analyse", "id": "ana_ic10_xcvr", "kind": "bus_census",
         "params": {"reference": "self"}},

        {"type": "note", "id": "obs", "prompt": (
            "Interpret:\n"
            "  - /OE (CH2) should be LOW or have very few rising edges — if it's\n"
            "    stuck HIGH, IC10 is not driving the bus at all (data is not\n"
            "    reaching the ribbon cable)\n"
            "  - DIR (CH1) should toggle with /WR (CH7) — many write cycles\n"
            "    means many DIR transitions\n"
            "  - A0..A3 (CH3..CH6) should have heavy activity (the 8085 reads\n"
            "    program memory continuously, so AD0..AD7 toggle even when\n"
            "    the user is idle)\n"
            "  - Zero edges on any of A0..A3 is a hard fault — IC10 pin dead\n"
            "    or upstream AD line broken"
        ), "multiline": True},
    ],
)


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------
REGISTRY: dict[str, TestDef] = {
    "bus_census": BUS_CENSUS,
    "contention_detector": CONTENTION,
    "good_vs_bad_diff": GOOD_VS_BAD,
    "ls138_isolation": LS138_ISOLATION,
    "ls273_sequence": LS273_SEQUENCE,
    "dac_dmm_crosscheck": DAC_DMM_CROSSCHECK,
    "bus_e2e_cpu_to_ac4": BUS_E2E_CPU_TO_AC4,
    "aa2_cpu_health": AA2_CPU_HEALTH,
    "aa2_address_bus": AA2_ADDRESS_BUS,
    "aa2_ic11_alone": AA2_IC11_ALONE,
    "aa2_ic20_latch": AA2_IC20_LATCH,
    "aa2_ic21_latch": AA2_IC21_LATCH,
    "aa2_ic10_xcvr": AA2_IC10_XCVR,
}


def list_tests() -> list[dict]:
    return [{"key": k, "name": t.name, "description": t.description,
             "n_steps": len(t.steps)} for k, t in REGISTRY.items()]


def get_test(key: str) -> TestDef:
    if key not in REGISTRY:
        raise KeyError(f"unknown test '{key}'. Available: {list(REGISTRY)}")
    return REGISTRY[key]


if __name__ == "__main__":
    for t in list_tests():
        print(f"  {t['key']:25s}  {t['n_steps']:>3d} steps  {t['description'][:60]}")
