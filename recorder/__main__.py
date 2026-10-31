"""czu-stream-recorder: record scheduled ČZU live streams (video.czu.cz/live/...).

  python -m recorder run    --config schedule.toml --out DIR [--notes DIR] [--work DIR]   # scheduler daemon
  python -m recorder list   --config schedule.toml --out DIR [--work DIR]   # schedule and status of each recording
  python -m recorder check  --config schedule.toml                          # validate the file, probe every stream
  python -m recorder record --stream URL|NAME --minutes N --title T --out DIR [--config F]   # record now

--out   MP4 files (e.g. a Plex library); a recording's `video` path is relative to it
--notes the .log and .m4a (and later the transcript); a recording's `notes` path is relative to it (default: --out)
--work  segment parts while recording, and state (default: <out>/.work)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import capture, hls, schedule

log = logging.getLogger("recorder")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


class Dirs:
    def __init__(self, out: str, notes: str | None = None, work: str | None = None):
        self.out = Path(out)
        self.notes = Path(notes) if notes else self.out
        self.work = Path(work) if work else self.out / ".work"


def paths(d: Dirs, rec: schedule.Recording) -> tuple[Path, Path, Path, Path]:
    """work dir, parts dir, MP4, notes base (without extension)."""
    work = d.work / rec.id
    return work, work / "parts", d.out / f"{rec.video_path}.mp4", d.notes / rec.notes_path


def status_of(d: Dirs, rec: schedule.Recording) -> str:
    work, parts, mp4, _ = paths(d, rec)
    if (work / "state.json").exists():
        import json
        try:
            return json.loads((work / "state.json").read_text()).get("phase", "?")
        except ValueError:
            return "?"
    return "done" if mp4.exists() else "scheduled"


def job(rec: schedule.Recording, d: Dirs, normalize: bool) -> None:
    """Record one schedule entry to the end of its window, then finalize. Safe to re-run:
    it resumes into the same parts folder after a restart."""
    work, parts, mp4, notes = paths(d, rec)
    jlog = logging.getLogger(f"rec.{rec.id}")
    try:
        work.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(work / "recording.log", encoding="utf-8")
    except OSError as e:
        log.error("%s: cannot create %s: %s", rec.title, work, e)
        return
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    jlog.addHandler(fh)
    state = capture.StateFile(work / "state.json", capture.Stats(title=rec.title))
    st = state.stats
    try:
        if datetime.now() < rec.window_end:
            jlog.info("recording %r %s → %s", rec.title, f"{rec.window_start:%H:%M}", f"{rec.window_end:%H:%M}")
            capture.capture(rec.stream_url, rec.max_height, rec.window_end, parts, state, jlog)
        st.phase = "finalizing"
        state.save()
        capture.finalize(parts, mp4, notes.with_name(notes.name + ".m4a"), normalize, jlog)
        dur = capture.probe_duration(mp4)
        expected = (rec.window_end - rec.window_start).total_seconds()
        st.phase, st.finished, st.output = "done", capture._now(), mp4.name
        st.message = f"{dur / 3600:.2f} h of {expected / 3600:.2f} h window, missed ~{st.missed_seconds:.0f} s"
        state.save()
        jlog.info("done: %s (%s)", mp4.name, st.message)
        capture.remove_parts(parts)
        shutil.copy2(work / "recording.log", notes.with_name(notes.name + ".log"))
    except Exception as e:
        st.phase, st.message, st.finished = "failed", str(e), capture._now()
        state.save()
        jlog.exception("recording failed; parts kept in %s", parts)
    finally:
        jlog.removeHandler(fh)
        fh.close()


def check_writable(d: Dirs) -> None:
    """Log (don't exit) when a folder isn't writable, with the usual cause on a Synology."""
    for p in dict.fromkeys((d.out, d.notes, d.work)):
        probe = p / f".write-test-{os.getpid()}"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            log.error("cannot write to %s (%s). Running as uid %d, groups %s. Give this user Read & Write "
                      "on the folder (Synology: File Station → Properties → Permission).",
                      p, e.strerror, os.getuid(), sorted(os.getgroups()))


def cmd_run(args) -> None:
    d = Dirs(args.out, args.notes, args.work)
    for p in (d.out, d.notes, d.work):
        p.mkdir(parents=True, exist_ok=True)
    active: dict[str, threading.Thread] = {}
    failing: set[str] = set()
    check_writable(d)
    last_error = None
    last_listing = None
    log.info("scheduler started (config %s, video %s, notes %s, work %s, TZ now %s)", args.config, d.out, d.notes, d.work,
             f"{datetime.now():%Y-%m-%d %H:%M}")
    while True:
        try:
            cfg, recs = schedule.load(Path(args.config))
            last_error = None
        except Exception as e:
            if str(e) != last_error:
                log.error("schedule unreadable, keeping the current jobs: %s", e)
                last_error = str(e)
            time.sleep(30)
            continue
        normalize = cfg.get("defaults", {}).get("normalize_audio", True)
        now = datetime.now()
        upcoming = [(r.window_start, r.title) for r in recs if r.window_start > now]
        listing = sorted(upcoming)[:3]
        if listing != last_listing:
            log.info("next: %s", "; ".join(f"{t:%a %d.%m. %H:%M} {n}" for t, n in listing) or "nothing scheduled")
            last_listing = listing
        for r in recs:
            if r.id in active and active[r.id].is_alive():
                continue
            # One bad entry (unreadable path, full disk, ...) must never stop the daemon:
            # a crash-looping container records nothing and floods the host with alerts.
            try:
                st = status_of(d, r)
                in_window = r.window_start <= now < r.window_end
                # unfinished after a restart past the window → just finalize what was recorded
                leftover = now >= r.window_end and st in ("recording", "finalizing")
                if (in_window and st not in ("done", "failed")) or leftover:
                    if shutil.disk_usage(d.work).free < 20 * 1024 ** 3:
                        log.warning("less than 20 GB free on the work volume")
                    t = threading.Thread(target=job, args=(r, d, normalize), name=r.id, daemon=True)
                    active[r.id] = t
                    t.start()
                failing.discard(r.id)
            except Exception as e:
                if r.id not in failing:
                    log.error("%s: %s", r.title, e)
                    failing.add(r.id)
        time.sleep(20)


def cmd_list(args) -> None:
    _, recs = schedule.load(Path(args.config))
    d = Dirs(args.out, None, args.work) if args.out else None
    for r in sorted(recs, key=lambda r: r.start):
        st = status_of(d, r) if d else "-"
        print(f"{r.window_start:%a %Y-%m-%d %H:%M}–{r.window_end:%H:%M}  {st:<10} {r.title}  →  {r.video_path}.mp4")


def cmd_check(args) -> int:
    cfg, recs = schedule.load(Path(args.config))
    print(f"{len(recs)} recording(s) parsed")
    bad = 0
    for name, url in cfg.get("streams", {}).items():
        try:
            media, v = hls.resolve_media_url(url, None)
            pl = hls.parse_media(hls.fetch_text(media), media)
            seg = pl.segments[-1]
            size = len(hls.download_segment(seg.url, attempts=2))
            print(f"OK   {name}: {v.height if v else '?'}p, live seq {seg.seq}, last segment {size // 1024} KiB")
        except Exception as e:
            bad += 1
            print(f"FAIL {name}: {e}")
    return 1 if bad else 0


def cmd_record(args) -> None:
    streams = {}
    if args.config:
        cfg, _ = schedule.load(Path(args.config))
        streams = cfg.get("streams", {})
    url = streams.get(args.stream, args.stream)
    start = datetime.now()
    rec = schedule.Recording(args.title, url, start, start + timedelta(minutes=args.minutes),
                             args.max_height, timedelta(0), timedelta(0))
    job(rec, Dirs(args.out, args.notes, args.work), not args.no_normalize)


def main() -> int:
    setup_logging()
    p = argparse.ArgumentParser(prog="recorder")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--config", required=True); r.add_argument("--out", required=True)
    r.add_argument("--notes"); r.add_argument("--work")
    l = sub.add_parser("list"); l.add_argument("--config", required=True); l.add_argument("--out"); l.add_argument("--work")
    c = sub.add_parser("check"); c.add_argument("--config", required=True)
    n = sub.add_parser("record")
    n.add_argument("--stream", required=True); n.add_argument("--minutes", type=float, required=True)
    n.add_argument("--title", default="Test"); n.add_argument("--out", required=True)
    n.add_argument("--config"); n.add_argument("--max-height", type=int)
    n.add_argument("--no-normalize", action="store_true"); n.add_argument("--notes"); n.add_argument("--work")
    a = p.parse_args()
    if a.cmd == "run":
        cmd_run(a)
    elif a.cmd == "list":
        cmd_list(a)
    elif a.cmd == "check":
        return cmd_check(a)
    elif a.cmd == "record":
        cmd_record(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
