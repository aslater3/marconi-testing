"""Per-test JSON report builder.

The report is the canonical artifact — it's what gets attached to a wiki page,
pasted into a chat, or fed back to analysis. Schema:

{
  "schema_version": 1,
  "test": "bus_census",
  "test_description": "...",
  "operator": "andyman5002",
  "started_at": "2026-06-04T21:00:00",
  "finished_at": "2026-06-04T21:15:00",
  "duration_s": 900,
  "hardware": "fx2lafw 24MHz 8ch",
  "captures": {
    "cap_baseline_idle": { ... Capture fields ... }
  },
  "events": [
    {"t": "2026-06-04T21:00:00", "step_id": "intro", "type": "prompt",
     "text": "...", "operator_input": ""},
    {"t": "...", "step_id": "clip_baseline", "type": "clip", ...},
    {"t": "...", "step_id": "cap_baseline_idle", "type": "capture", ...},
    {"t": "...", "step_id": "ana_baseline", "type": "analysis",
     "result": { ... analyser output ... }},
    {"t": "...", "step_id": "obs_ac13", "type": "note",
     "operator_input": "Shark fin present on 'C' line, 50ns droop"}
  ],
  "verdicts": [
    {"channel": 0, "verdict": "stuck-at-high", "evidence": "..."}
  ]
}
"""
from __future__ import annotations
import json
import math
import os
import time
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Report:
    def __init__(self, test_name: str, test_description: str, operator: Optional[str] = None,
                 hardware: Optional[str] = None):
        self.test_name = test_name
        self.test_description = test_description
        self.operator = operator or os.environ.get("USER", "unknown")
        self.hostname = socket.gethostname()
        self.hardware = hardware or "unknown"
        self.started_at = _now_iso()
        self.finished_at: Optional[str] = None
        self.events: list[dict] = []
        self.captures: dict[str, dict] = {}
        self.verdicts: list[dict] = []
        self._state: dict[str, Any] = {}  # sticky state from set_state steps

    def add_event(self, step_id: str, step_type: str, **kwargs) -> dict:
        ev = {"t": _now_iso(), "step_id": step_id, "type": step_type, **kwargs}
        self.events.append(ev)
        return ev

    def add_capture(self, capture_dict: dict) -> None:
        self.captures[capture_dict["name"]] = capture_dict

    def add_verdict(self, channel: int, verdict: str, evidence: str) -> None:
        self.verdicts.append({"channel": channel, "verdict": verdict, "evidence": evidence})

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def add_measurement(self, step_id: str, channel: str, value_v: float,
                        expected_v: float = None, code: int = None,
                        tolerance_pct: float = 2.0, unit: str = "V",
                        notes: str = "") -> dict:
        """Record a DMM (or other analog) measurement with expected value and tolerance.

        Used by tests that cross-check digital bus activity against analog readings.
        """
        m = {
            "step_id": step_id,
            "channel": channel,
            "value_v": value_v,
            "expected_v": expected_v,
            "code": code,
            "tolerance_pct": tolerance_pct,
            "unit": unit,
            "notes": notes,
        }
        if expected_v is not None:
            diff = abs(value_v - expected_v)
            within = diff <= abs(expected_v) * (tolerance_pct / 100.0)
            m["diff_v"] = diff
            m["diff_pct"] = (diff / abs(expected_v) * 100) if expected_v else None
            m["within_tolerance"] = within
        # Store in sticky_state under a dedicated key for easy retrieval
        if "analog_measurements" not in self._state:
            self._state["analog_measurements"] = []
        self._state["analog_measurements"].append(m)
        return m

    def finish(self) -> None:
        self.finished_at = _now_iso()

    def to_dict(self) -> dict:
        return _jsonify({
            "schema_version": SCHEMA_VERSION,
            "test": self.test_name,
            "test_description": self.test_description,
            "operator": self.operator,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "finished_at": self.finished_at or _now_iso(),
            "hardware": self.hardware,
            "captures": self.captures,
            "events": self.events,
            "verdicts": self.verdicts,
            "sticky_state": self._state,
        })

    def write(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"{self.test_name}_{ts}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, allow_nan=False))
        # Update 'latest' symlink
        latest = out_dir / "latest.json"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(path.name)
        return path


def _jsonify(obj):
    """Recursively replace non-finite floats with strings so JSON stays valid."""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        if math.isnan(obj):
            return "NaN"
    return obj
