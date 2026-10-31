"""Record one live HLS stream into numbered .ts parts, then remux them into one MP4."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import hls, silence


@dataclass
class Stats:
    title: str
    phase: str = "pending"  # pending → recording → finalizing → done | failed
    variant: str = ""
    segments: int = 0
    bytes: int = 0
    recorded_seconds: float = 0.0
    missed_seconds: float = 0.0
    gaps: list[str] = field(default_factory=list)
    errors: int = 0
    started: str = ""
    finished: str = ""
    output: str = ""
    message: str = ""
    stopped: str = ""  # why recording ended before the hard cap (silence), if it did


class StateFile:
    def __init__(self, path: Path, stats: Stats):
        self.path, self.stats = path, stats
        if path.exists():  # resume after a container restart
            try:
                old = json.loads(path.read_text())
                for k in ("segments", "bytes", "recorded_seconds", "missed_seconds", "gaps", "errors", "started"):
                    setattr(stats, k, old.get(k, getattr(stats, k)))
            except (ValueError, OSError):
                pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.stats), ensure_ascii=False, indent=2))
        tmp.replace(self.path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def capture(master_url: str, max_height: int | None, until: datetime, parts: Path,
            state: StateFile, log: logging.Logger, stop: silence.SilenceStop | None = None) -> None:
    """Poll the media playlist and save every new segment until `until` (the hard cap), or, with
    `stop`, until the room has been quiet for a while after the scheduled end.

    Segment files are named <download time>-<media sequence>.ts, so sorting by name gives
    the recording order even if the server restarts and the sequence numbers start over.
    """
    st = state.stats
    parts.mkdir(parents=True, exist_ok=True)
    existing = sorted(parts.glob("*.ts"))
    seen = {int(p.stem.rsplit("-", 1)[1]) for p in existing}
    last_seq: int | None = max(seen) if seen else None
    levels = silence.load_levels(parts)
    recent: list[tuple[datetime, float | None]] = [
        (silence.part_time(p.name) or datetime.now(), levels.get(p.name)) for p in existing[-120:]]
    last_arrival: datetime | None = recent[-1][0] if recent else None
    seg_seconds = 10.0
    level_failures = 0
    media_url: str | None = None
    failures = 0
    last_save = 0.0
    st.phase = "recording"
    st.started = st.started or _now()
    state.save()

    while datetime.now() < until:
        if stop:
            reason = silence.should_stop(datetime.now(), stop, recent, last_arrival, seg_seconds)
            if reason:
                st.stopped = reason
                log.info("stopping: %s", reason)
                break
        try:
            if media_url is None:
                media_url, v = hls.resolve_media_url(master_url, max_height)
                desc = f"{v.height}p {v.bandwidth // 1000} kb/s" if v else "single variant"
                if st.variant != desc:
                    log.info("variant %s (%s)", desc, media_url.rsplit("/", 1)[-1])
                st.variant = desc
            pl = hls.parse_media(hls.fetch_text(media_url), media_url)
            if failures:
                log.info("playlist back after %d failed polls", failures)
            failures = 0
        except Exception as e:  # network, HTTP 404 of an expired session, parse error
            failures += 1
            st.errors += 1
            if failures in (1, 3) or failures % 20 == 0:
                log.warning("playlist poll failed (%d in a row): %s", failures, e)
            if failures >= 2:
                media_url = None  # re-resolve the master: Wowza session ids expire
            time.sleep(min(5 * failures, 30))
            continue

        if pl.segments and last_seq is not None and pl.segments[-1].seq < last_seq - 3:
            log.warning("media sequence restarted (%d → %d): stream was restarted", last_seq, pl.segments[-1].seq)
            st.gaps.append(f"{_now()} stream restart")
            seen.clear()
            last_seq = None

        for seg in pl.segments:
            if seg.seq in seen or (last_seq is not None and seg.seq <= last_seq):
                continue
            if last_seq is not None and seg.seq > last_seq + 1:
                missed = (seg.seq - last_seq - 1) * pl.target_duration
                st.missed_seconds += missed
                st.gaps.append(f"{_now()} missed ~{missed:.0f} s")
                log.warning("gap: %d segment(s) (~%.0f s) dropped out of the playlist window",
                            seg.seq - last_seq - 1, missed)
            try:
                data = hls.download_segment(seg.url)
            except RuntimeError as e:
                st.errors += 1
                st.missed_seconds += seg.duration
                st.gaps.append(f"{_now()} segment {seg.seq} lost")
                log.warning("segment %d lost: %s", seg.seq, e)
                last_seq = seg.seq
                continue
            name = parts / f"{datetime.now():%Y%m%d%H%M%S}-{seg.seq:010d}.ts"
            tmp = name.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(name)
            now = datetime.now()
            peak = silence.segment_peak_db(data)
            if peak is None:
                level_failures += 1
                if level_failures in (1, 10):
                    log.warning("could not measure the audio level of %s (%d so far)", name.name, level_failures)
            silence.record_level(parts, name.name, peak, now)
            recent.append((now, peak))
            del recent[:-120]
            last_arrival = now
            seg_seconds = seg.duration or seg_seconds
            seen.add(seg.seq)
            last_seq = seg.seq
            st.segments += 1
            st.bytes += len(data)
            st.recorded_seconds += seg.duration

        if time.monotonic() - last_save > 30:
            state.save()
            last_save = time.monotonic()
        if pl.ended:
            log.info("server ended the playlist (EXT-X-ENDLIST); re-checking in 30 s")
            media_url = None
            time.sleep(30)
            continue
        time.sleep(max(1.0, pl.target_duration / 2))

    state.save()


def ffmpeg(args: list[str], log: logging.Logger) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stderr.strip():
        tail = res.stderr.strip().splitlines()[-15:]
        (log.error if res.returncode else log.debug)("ffmpeg: %s", " | ".join(tail))
    if res.returncode:
        raise RuntimeError(f"ffmpeg exited {res.returncode}")


def finalize(parts: Path, out_mp4: Path, m4a: Path, normalize_audio: bool, log: logging.Logger,
             trim_after: datetime | None = None, threshold_db: float = -45.0) -> None:
    """Join the parts into one MP4 (video copied, audio optionally loudness-levelled),
    plus an audio-only .m4a for listening or transcription. With `trim_after` (the scheduled end), the
    quiet parts recorded after it, beyond ~30 s past the last sound, are left out."""
    files = sorted(parts.glob("*.ts"))
    if not files:
        raise RuntimeError("no segments were recorded")
    if trim_after is not None:
        keep = set(silence.trim_tail([f.name for f in files], silence.load_levels(parts), trim_after, threshold_db))
        dropped = len(files) - len(keep)
        if dropped:
            log.info("trimming %d quiet part(s) (~%d s) at the end", dropped, dropped * 10)
        files = [f for f in files if f.name in keep]
    lst = parts.parent / "concat.txt"
    lst.write_text("".join("file '{}'\n".format(str(f).replace("'", r"'\''")) for f in files))
    tmp = out_mp4.with_name(out_mp4.stem + ".partial.mp4")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    audio = (["-af", "dynaudnorm=f=500:g=31:p=0.9:m=20", "-c:a", "aac", "-b:a", "128k"]
             if normalize_audio else ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"])
    t0 = time.monotonic()
    log.info("joining %d parts into %s", len(files), out_mp4.name)
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", *audio, "-movflags", "+faststart", str(tmp)], log)
    tmp.replace(out_mp4)
    m4a.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(["-i", str(out_mp4), "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart",
            str(m4a.with_name(m4a.stem + ".partial.m4a"))], log)
    m4a.with_name(m4a.stem + ".partial.m4a").replace(m4a)
    log.info("finalized in %.0f s", time.monotonic() - t0)


def probe_duration(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def remove_parts(parts: Path) -> None:
    shutil.rmtree(parts, ignore_errors=True)
    (parts.parent / "concat.txt").unlink(missing_ok=True)
