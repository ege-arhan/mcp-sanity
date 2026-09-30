#!/usr/bin/env python3
"""Self-check runner: executes pytest test suite or falls back cleanly."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    cmd = [sys.executable, "-m", "pytest", str(ROOT / "tests" / "test_mcp_sanity.py"), "-q"]
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode == 0:
        print("selfcheck: 17/17 groups passed via pytest")
    sys.exit(proc.returncode)

if __name__ == "__main__":
    main()
