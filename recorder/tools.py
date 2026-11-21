"""Finding ffmpeg and ffprobe.

Jobs started by launchd or cron get a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), where Homebrew's ffmpeg
isn't. Without this lookup every segment's level comes out unmeasured, so silence never stops a recording,
and joining the parts fails at the end of the window.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def find(name: str, fallback_dirs: tuple[str, ...] | None = None) -> str | None:
    """Full path of `name` from the PATH or the usual install folders, or None if it isn't installed."""
    found = shutil.which(name)
    if found:
        return found
    for d in FALLBACK_DIRS if fallback_dirs is None else fallback_dirs:
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def cmd(name: str) -> str:
    """What to run for `name`: its full path when found, else the bare name (the call then fails visibly)."""
    return find(name) or name


def missing() -> list[str]:
    return [n for n in ("ffmpeg", "ffprobe") if find(n) is None]
