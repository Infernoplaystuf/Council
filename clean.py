#!/usr/bin/env python3
"""
Clear stale Python bytecode caches.

Run this if updates don't seem to be taking effect — it deletes every
__pycache__ folder under the project so the next launch recompiles
from source.

Usage:
    python clean.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    cleared = 0
    for d in root.rglob("__pycache__"):
        try:
            shutil.rmtree(d)
            cleared += 1
        except Exception as e:
            print(f"  could not remove {d}: {e}", file=sys.stderr)
    if cleared:
        print(f"Cleared {cleared} __pycache__ folder(s).")
    else:
        print("No __pycache__ folders found — already clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
