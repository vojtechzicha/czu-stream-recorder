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
    video: str | None = None  # path of the MP4 under the recordings dir, without extension
    notes: str | None = None  # path of the log/audio/transcript under the notes dir, without extension
    # End on silence: after end + min_tail, stop once `silence` passed without sound above silence_db.
    # `tail` (end + tail = window_end) is then the hard cap.
    stop_on_silence: bool = False
    min_tail: timedelta = timedelta(minutes=5)
    silence: timedelta = timedelta(minutes=5)
    silence_db: float = -45.0

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
    def video_path(self) -> str:
        return self.video or self.basename

    @property
    def notes_path(self) -> str:
        return self.notes or self.basename

    @property
    def id(self) -> str:
        return f"{self.start:%Y%m%d-%H%M}-{slug(self.title)}"


def safe_name(s: str) -> str:
    """Keep Czech letters (SMB/OneDrive handle them); drop characters that break paths."""
    return re.sub(r'[\\/:*?"<>|]+', "-", s).strip(" .")


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "recording"


def _rel(v, what: str) -> str | None:
    if v is None:
        return None
    parts = [safe_name(x) for x in str(v).replace("\\", "/").split("/") if x.strip()]
    if not parts or any(x in ("", ".", "..") for x in parts):
        raise ValueError(f"invalid {what} path {v!r}")
    return "/".join(parts)


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

        def opt(key, default, r=r):
            return r.get(key, defaults.get(key, default))
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
            video=_rel(r.get("video"), "video"),
            notes=_rel(r.get("notes"), "notes"),
            stop_on_silence=bool(opt("stop_on_silence", False)),
            min_tail=timedelta(minutes=opt("min_tail_minutes", 5)),
            silence=timedelta(minutes=opt("silence_minutes", 5)),
            silence_db=float(opt("silence_threshold_db", -45.0)),
        ))
    return data, recs
