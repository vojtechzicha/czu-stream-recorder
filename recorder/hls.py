"""Minimal HLS client: master/media playlist parsing and segment download.

Only what a Wowza live stream needs: a master playlist with a few variants, each a
sliding-window media playlist of MPEG-TS segments. No encryption, no byte ranges.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin

USER_AGENT = "czu-stream-recorder/1.0"


@dataclass
class Variant:
    url: str
    bandwidth: int
    height: int


@dataclass
class Segment:
    seq: int
    url: str
    duration: float


@dataclass
class MediaPlaylist:
    target_duration: float
    segments: list[Segment]
    ended: bool


def fetch(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str, timeout: float = 15.0) -> str:
    return fetch(url, timeout).decode("utf-8", errors="replace")


def _attrs(line: str) -> dict[str, str]:
    """Parse 'KEY=VALUE,KEY="quoted, value"' after the tag's colon."""
    out: dict[str, str] = {}
    body = line.split(":", 1)[1] if ":" in line else ""
    key, val, in_q, cur = None, "", False, ""
    for ch in body + ",":
        if ch == '"':
            in_q = not in_q
        elif ch == "=" and not in_q and key is None:
            key, cur = cur.strip(), ""
        elif ch == "," and not in_q:
            if key is not None:
                out[key.upper()] = cur.strip()
            key, cur = None, ""
        else:
            cur += ch
    return out


def parse_master(text: str, base_url: str) -> list[Variant]:
    variants: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = _attrs(line)
        elif line and not line.startswith("#") and pending is not None:
            res = pending.get("RESOLUTION", "0x0")
            height = int(res.split("x")[1]) if "x" in res else 0
            variants.append(Variant(urljoin(base_url, line), int(pending.get("BANDWIDTH", "0")), height))
            pending = None
    return variants


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    seq = 0
    target = 10.0
    ended = False
    segments: list[Segment] = []
    dur: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            dur = float(line.split(":", 1)[1].split(",")[0])
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line and not line.startswith("#"):
            segments.append(Segment(seq, urljoin(base_url, line), dur if dur is not None else target))
            seq += 1
            dur = None
    return MediaPlaylist(target, segments, ended)


def pick_variant(variants: list[Variant], max_height: int | None) -> Variant:
    ok = [v for v in variants if not max_height or v.height <= max_height] or variants
    return max(ok, key=lambda v: (v.height, v.bandwidth))


def resolve_media_url(master_url: str, max_height: int | None) -> tuple[str, Variant | None]:
    """Return the media-playlist URL to poll. Wowza puts a session id in the chunklist
    name, so call this again whenever the chunklist stops answering."""
    text = fetch_text(master_url)
    if "#EXT-X-STREAM-INF" not in text:
        return master_url, None
    variants = parse_master(text, master_url)
    if not variants:
        raise ValueError("master playlist lists no variants")
    v = pick_variant(variants, max_height)
    return v.url, v


def download_segment(url: str, attempts: int = 4, timeout: float = 30.0) -> bytes:
    last: Exception | None = None
    for i in range(attempts):
        try:
            data = fetch(url, timeout)
            if data:
                return data
            last = ValueError("empty segment")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
        time.sleep(min(2 ** i, 8))
    raise RuntimeError(f"segment failed after {attempts} attempts: {last}")
