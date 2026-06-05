# Marconi 2019A Test Harness

Sigrok + Python guided test harness for the Marconi 2019A synthesized signal
generator, designed for investigating the AC4 board digital bus fault
(non-linearity + ~5.6 dBm shortfall). Walks the operator through each test
with prompts, captures with sigrok-cli, and produces a JSON report per test.

For the full feature list, the channel map for each test, and the bench session
runbook, see `~/wiki/projects/marconi-2019a/test-harness.md`.

## Quick start

```bash
# Verify it works without hardware (uses synthetic captures)
python3 run.py --simulate --list
for t in bus_census contention_detector good_vs_bad_diff \
         ls138_isolation ls273_sequence dac_dmm_crosscheck \
         bus_e2e_cpu_to_ac4; do
    python3 run.py --test $t --simulate
done

# With a real logic analyser (Saleae Logic 8ch/24MHz, FX2LP-based)
sudo apt install sigrok-cli pulseview sigrok-firmware-fx2lafw
sudo usermod -aG plugdev $USER    # then log out / log in
python3 run.py                    # interactive menu
```

## Supported hardware

**Saleae Logic** (the original 8-channel/24MHz/100MSps unit, VID 0925:3881).
This is a Cypress FX2LP-based device and is supported by sigrok via the
`fx2lafw` driver. The first time the LA is plugged in, sigrok auto-uploads
the `fx2lafw-saleae-logic.fw` firmware to the FX2's RAM — the device then
appears as `fx2lafw:conn=3.5 - Saleae Logic [S/N: ...]` in `sigrok-cli --scan`.
The original Saleae firmware is preserved in the FX2's EEPROM/flash and can
be re-flashed with the Saleae Logic 1.x desktop app if you ever need to
revert (this is documented at https://sigrok.org/wiki/Saleae_Logic).

If you have a different FX2-based clone (Hantek 6022BE/BL, EE Electronics
ESLA100, etc.) it should also work — the harness hard-codes the
`fx2lafw` driver, so any sigrok-compatible FX2 LA will be picked up
automatically as long as the firmware is loaded.

## Tests

| Test | Purpose | Stages |
|------|---------|--------|
| `bus_census`              | Per-channel health check across all recipient boards | 1 (per-recipient) |
| `contention_detector`     | Focused on the 'C' address line | 1 |
| `good_vs_bad_diff`        | Diff a known-good DAC write vs a suspect one | 2 |
| `ls138_isolation`         | Confirm/rule out AD4 IC1 74LS138 as contention source | 2 (control, lift) |
| `ls273_sequence`          | Detect a stuck 74LS273 output bit | 3 (codes 1, 2, 4) |
| `dac_dmm_crosscheck`      | Cross-check AD7522LN digital inputs vs analog output | 4 (codes 0, 1024, 2048, 4095) |
| `bus_e2e_cpu_to_ac4`      | **End-to-end CPU→AC4 pipeline verification** | 4 (CPU, buffer, decoder, DAC) |

The `bus_e2e_cpu_to_ac4` test is the flagship health-check: it produces a
stage-by-stage verdict table that pinpoints which stage of the canonical
4-stage pipeline (8085 → 74LS245/244 → AC3/ACL3 IC1 74LS138 → AD7522)
the bus transaction first goes wrong.

## Layout

```
marconi-test-harness/
├── run.py                # entry point
├── harness/
│   ├── analysis.py       # bus_census, contention, diff, n_way_diff, analogue_vs_code, bus_e2e
│   ├── capture.py        # sigrok wrapper, VCD parser (handles both sim and sigrok VCD), synthetic VCD
│   ├── report.py         # per-test JSON report builder
│   ├── tests.py          # test definitions
│   └── ui.py             # terminal dashboard
├── captures/             # raw VCDs (gitignored, one per capture step)
├── reports/              # per-test JSON (gitignored, one per test run)
├── tests/                # future saved captures for regression testing
├── configs/              # future per-test threshold overrides
└── README.md
```

## VCD parsing

The harness's `parse_vcd_transitions()` function transparently handles both
VCD flavours:

* **Simulator output** (mode `simulate`): 1 ns timescale, var IDs `a..h`,
  names `ch0..ch7`.
* **sigrok-cli output** (mode `hardware`): 100 ps timescale, var IDs
  `!"#$%&'(`, names `D0..D7`. Timestamps are converted to ns automatically
  so the analysers always see a uniform timebase.

A small in-process cache avoids re-parsing VCD headers when the same file
is read by multiple analysers in a single test run.

## Probe-map audit

The channel-to-pin labels in every test's `clip` step have been audited
against the AA2/1 IC pinout in `~/wiki/projects/marconi-2019a/aa2-1-ic-inventory.md`.
See `~/wiki/projects/marconi-2019a/probe-map-audit.md` for the row-by-row
audit. **Before the first bench session, re-verify every ❓ row in the
audit against page-014 Fig. 3 of the service manual** — the A/B/C
address-line ↔ IC11 Y0–Y7 mapping is not documented in the wiki and is
the one piece of probe-map data that still needs human confirmation.

## Repository

This project is git-version-controlled. See `.gitignore` for the runtime
artifacts excluded from tracking.
