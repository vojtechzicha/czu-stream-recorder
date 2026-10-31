"""Schedule file (TOML). Re-read on every scheduler tick, so edits apply without a restart."""
from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Recording:
    title: str
    stream_url: str
    start: datetime  # naive local time (container TZ)
    end: datetime
    max_height: int | None
    lead: timedelta
    tail: timedelta

    @property
    def window_start(self) -> datetime:
        return self.start - self.lead

    @property
    def window_end(self) -> datetime:
        return self.end + self.tail

    @property
    def basename(self) -> str:
        return f"{self.start:%Y-%m-%d} {safe_name(self.title)}"

    @property
    def id(self) -> str:
        return f"{self.start:%Y%m%d-%H%M}-{slug(self.title)}"


def safe_name(s: str) -> str:
    """Keep Czech letters (SMB/OneDrive handle them); drop characters that break paths."""
    return re.sub(r'[\\/:*?"<>|]+', "-", s).strip(" .")


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "recording"


def _dt(v) -> datetime:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone().replace(tzinfo=None)
    return datetime.fromisoformat(str(v))


def load(path: Path) -> tuple[dict, list[Recording]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    streams: dict[str, str] = data.get("streams", {})
    recs: list[Recording] = []
    for i, r in enumerate(data.get("recording", [])):
        stream = r.get("stream", defaults.get("stream"))
        url = streams.get(stream, stream)
        if not url or not str(url).startswith("http"):
            raise ValueError(f"recording #{i + 1} ({r.get('title')}): unknown stream {stream!r}")
        start, end = _dt(r["start"]), _dt(r["end"])
        if end <= start:
            raise ValueError(f"recording #{i + 1} ({r.get('title')}): end before start")
        recs.append(Recording(
            title=r["title"],
            stream_url=url,
            start=start,
            end=end,
            max_height=r.get("max_height", defaults.get("max_height")),
            lead=timedelta(minutes=r.get("lead_minutes", defaults.get("lead_minutes", 10))),
            tail=timedelta(minutes=r.get("tail_minutes", defaults.get("tail_minutes", 20))),
        ))
    return data, recs
