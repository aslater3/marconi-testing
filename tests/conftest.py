"""Pytest fixtures shared across the test_harness unit tests."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

# Make the harness package importable from the tests/ dir
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def tmp_vcd_dir(tmp_path: Path) -> Path:
    """Per-test scratch dir for VCDs."""
    d = tmp_path / "vcds"
    d.mkdir()
    return d
