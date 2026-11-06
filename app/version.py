"""Which commit this copy of the app is (shown on /healthz and error pages).

scripts/replit-setup.sh writes it to .version at build time, because the
published copy may not include .git; `git rev-parse` is the fallback here.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def commit() -> str:
    try:
        return (ROOT / ".version").read_text().strip() or "unknown"
    except OSError:
        pass
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=3).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
