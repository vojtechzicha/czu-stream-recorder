"""Ending a recording when the room goes quiet, instead of at a fixed time.

Lectures overrun by unpredictable amounts. After the scheduled end, the recorder keeps going until the
last `window` of audio stayed below `threshold_db` (a segment's peak level), or no new segment arrived for
`window`, or the hard cap is reached. On 2026-10-31 the Kongresový sál feed measured about -37 dB mean and
-9 to -17 dB peaks during speech, and -75 dB mean and -62 dB peak with the room empty (microphones off), so a
-45 dB peak threshold sits safely in between.

Each segment's peak is stored in `levels.jsonl` next to the parts, so the decision survives a restart and
the trailing silence can be trimmed when the parts are joined.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

LEVELS = "levels.jsonl"


@dataclass(frozen=True)
class SilenceStop:
    earliest: datetime      # never stop before this (scheduled end + a minimum tail)
    window: timedelta       # this much quiet (or no data) in a row ends the recording
    threshold_db: float     # a segment is "quiet" when its peak stays below this


def segment_peak_db(data: bytes) -> float | None:
    """Peak level of the segment's audio in dBFS (None if it has no decodable audio)."""
    try:
        res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", "pipe:0", "-map", "0:a:0",
                              "-af", "volumedetect", "-f", "null", "-"],
                             input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(rb"max_volume:\s*(-?[\d.]+|-inf) dB", res.stderr)
    if not m:
        return None
    v = m.group(1)
    return -120.0 if v == b"-inf" else float(v)


def record_level(parts: Path, name: str, peak: float | None, at: datetime) -> None:
    with (parts / LEVELS).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"part": name, "at": at.isoformat(timespec="seconds"), "peak": peak}) + "\n")


def load_levels(parts: Path) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    p = parts / LEVELS
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                out[rec["part"]] = rec["peak"]
            except (ValueError, KeyError):
                continue
    return out


def is_quiet(peak: float | None, threshold_db: float) -> bool:
    # no decodable audio counts as quiet: there is nothing worth recording in it
    return peak is None or peak < threshold_db


def should_stop(now: datetime, rule: SilenceStop, recent: list[tuple[datetime, float | None]],
                last_arrival: datetime | None, segment_seconds: float) -> str | None:
    """Return the reason to stop now, or None. `recent` = (arrival time, peak) of the latest segments,
    oldest first."""
    if now < rule.earliest:
        return None
    if last_arrival is not None and now - last_arrival >= rule.window:
        return f"no new segments for {rule.window.total_seconds() / 60:.0f} min"
    need = max(1, round(rule.window.total_seconds() / max(segment_seconds, 1.0)))
    if len(recent) < need:
        return None
    tail = recent[-need:]
    if all(is_quiet(p, rule.threshold_db) for _, p in tail):
        return (f"room quiet for {rule.window.total_seconds() / 60:.0f} min "
                f"(no segment above {rule.threshold_db:.0f} dB since {tail[0][0]:%H:%M:%S})")
    return None


def part_time(name: str) -> datetime | None:
    """Download time encoded in a part's file name (YYYYmmddHHMMSS-sequence.ts)."""
    try:
        return datetime.strptime(name[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def trim_tail(names: list[str], levels: dict[str, float | None], trim_after: datetime,
              threshold_db: float, keep_after_last_loud: int = 3) -> list[str]:
    """Drop the quiet parts at the end, but only those recorded after `trim_after` (the scheduled end),
    keeping `keep_after_last_loud` parts (~30 s) after the last one with sound."""
    if not names:
        return names
    last_loud = -1
    for i, n in enumerate(names):
        if not is_quiet(levels.get(n), threshold_db) or n not in levels:
            last_loud = i  # parts without a measurement are kept, to be safe
    keep_until = last_loud + keep_after_last_loud
    out = []
    for i, n in enumerate(names):
        t = part_time(n)
        if i <= keep_until or t is None or t < trim_after:
            out.append(n)
    return out
