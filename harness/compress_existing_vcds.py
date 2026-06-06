"""One-shot migration: compress existing .vcd files in captures/ to .vcd.gz.

Run this once after pulling the gzip-compression commit, to shrink the
54 MB cap_cpu_health_20260606_161443.vcd (and any other uncompressed
captures already in the captures/ directory) down to ~12 MB each.

What it does, per captures/*.vcd:
  1. Writes captures/<name>.vcd.gz  (gzip -6, same as live capture)
  2. Deletes the original .vcd       (keeps the dir tidy and avoids
                                      double-storage in git)
  3. Skips files where .vcd.gz already exists (idempotent — re-runs
     are no-ops).

After running this, `git status` in the repo will show:
  - a new captures/*.vcd.gz   (the gzipped form, commit this)
  - a deletion of captures/*.vcd   (also commit this)

So one commit: "compress capture VCDs to .vcd.gz".

Old report JSONs that still point at .vcd (not .vcd.gz) will keep
working, because parse_vcd_transitions() auto-detects the .gz suffix.
"""
from __future__ import annotations
import sys
from pathlib import Path

from harness.capture import gzip_capture


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    captures_dir = repo_root / "captures"
    if not captures_dir.is_dir():
        print(f"error: {captures_dir} is not a directory", file=sys.stderr)
        return 1

    vcd_files = sorted(captures_dir.glob("*.vcd"))
    if not vcd_files:
        print("nothing to compress — no .vcd files in captures/")
        return 0

    print(f"found {len(vcd_files)} .vcd file(s) in captures/")
    total_raw = 0
    total_gz = 0
    for vcd in vcd_files:
        gz = vcd.with_suffix(".vcd.gz")
        if gz.exists():
            print(f"  SKIP {vcd.name}  (.vcd.gz already exists)")
            continue
        raw_size = vcd.stat().st_size
        gz_path = gzip_capture(vcd, keep_original=True)
        if gz_path is None:
            print(f"  FAIL {vcd.name}  (compression failed, see warning above)")
            continue
        # gzip_capture(keep_original=True) leaves the .vcd in place; delete
        # it now so the captures/ dir doesn't have both forms cluttering git.
        gz_size = gz_path.stat().st_size
        vcd.unlink()
        total_raw += raw_size
        total_gz += gz_size
        ratio = (1 - gz_size / raw_size) * 100 if raw_size else 0
        print(f"  {vcd.name}  {raw_size:>12,} -> {gz_size:>12,}  "
              f"({ratio:.1f}% reduction)")

    if total_raw:
        print()
        print(f"total: {total_raw:,} bytes -> {total_gz:,} bytes  "
              f"({(1 - total_gz / total_raw) * 100:.1f}% reduction)")
    print()
    print("next steps:")
    print("  git add -A captures/")
    print("  git commit -m 'compress capture VCDs to .vcd.gz'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
