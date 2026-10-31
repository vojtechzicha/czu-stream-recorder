# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["mlx-whisper>=0.4.2", "faster-whisper>=1.1", "numpy"]
# ///
"""Transcribe the recorder's .m4a files on an Apple Silicon Mac (MLX Whisper, local, free).

  uv run czu_transcribe.py scan  ROOT [--model M]   # every finished recording under ROOT without a transcript
  uv run czu_transcribe.py file  AUDIO [--model M] [--prompt TEXT] [--force]

A recording is "finished" when the recorder has written its .log next to the .m4a. The transcript goes next to
it as <name>.txt, one paragraph per ~30 s or pause, in the Plaud CLI style:

  [00:12:31 - 00:13:02] Text …

Silence is cut out with Silero VAD before Whisper sees it, because Whisper invents text on silence (the classic
"Titulky vytvořil JohnyX"). Work is checkpointed per ~30-minute block in <name>.transcript.partial.jsonl, so a
sleeping Mac only pauses it.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

SR = 16000
BLOCK = 30 * 60            # seconds per block
BLOCK_SLACK = 90           # look this far past the block end for a pause to cut at
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
HALLUCINATIONS = re.compile(
    r"titulky|johnyx|amara\.org|děkuji za (vaši )?pozornost\.?$|odebírejte|subscribe|www\.\S+\.(cz|com)",
    re.IGNORECASE)


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def decode(path: Path, start: float, length: float) -> np.ndarray:
    """16 kHz mono float32 slice of the file (ffmpeg seeks, so memory stays per block)."""
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                          "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def speech_regions(audio: np.ndarray) -> list[tuple[float, float]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=700, speech_pad_ms=250,
                                                 min_speech_duration_ms=300))
    return [(t["start"] / SR, t["end"] / SR) for t in ts]


def merge(regions: list[tuple[float, float]], gap: float = 2.0, max_len: float = 600) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in regions:
        if out and s - out[-1][1] < gap and e - out[-1][0] < max_len:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def cut_point(regions: list[tuple[float, float]], target: float, limit: float) -> float:
    """A pause between target-120 s and limit to end the block at, so no word is cut in half."""
    best = None
    for (s1, e1), (s2, _) in zip(regions, regions[1:]):
        mid = (e1 + s2) / 2
        if target - 120 <= mid <= limit and s2 - e1 >= 0.5:
            if best is None or abs(mid - target) < abs(best - target):
                best = mid
    if best is not None:
        return best
    # no pause: cut inside the longest silence after target, else at the limit
    return limit


def transcribe_clips(audio: np.ndarray, clips: list[tuple[float, float]], model: str, prompt: str | None) -> list[dict]:
    import mlx_whisper
    if not clips:
        return []
    flat = ",".join(f"{s:.2f},{e:.2f}" for s, e in clips)
    res = mlx_whisper.transcribe(audio, path_or_hf_repo=model, language="cs", task="transcribe",
                                 clip_timestamps=flat, condition_on_previous_text=False, initial_prompt=prompt,
                                 compression_ratio_threshold=2.4, logprob_threshold=-1.0, no_speech_threshold=0.6,
                                 verbose=None)
    return res["segments"]


def keep(seg: dict) -> bool:
    text = seg["text"].strip()
    if not text:
        return False
    if HALLUCINATIONS.search(text):
        return False
    if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -1.0:
        return False
    if seg.get("compression_ratio", 0) > 2.6:
        return False
    return True


def drop_repeats(segs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in segs:
        if len(out) >= 2 and s["text"] == out[-1]["text"] == out[-2]["text"]:
            continue
        out.append(s)
    return out


def paragraphs(segs: list[dict], target: float = 30.0, pause: float = 3.0) -> list[tuple[float, float, str]]:
    out: list[list] = []
    for s in segs:
        if out and s["start"] - out[-1][1] < pause and s["end"] - out[-1][0] <= target + 15:
            out[-1][1] = s["end"]
            out[-1][2] += " " + s["text"]
        else:
            out.append([s["start"], s["end"], s["text"]])
    return [(a, b, re.sub(r"\s+", " ", t).strip()) for a, b, t in out]


def hms(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def default_prompt(audio: Path) -> str:
    subject = audio.parent.name
    return f"Přednáška z předmětu {subject}." if subject and subject not in (".", "Video") else "Vysokoškolská přednáška."


def transcribe_file(audio: Path, model: str, prompt: str | None, force: bool = False) -> Path:
    txt = audio.with_suffix(".txt")
    ckpt = audio.with_name(audio.stem + ".transcript.partial.jsonl")
    if txt.exists() and not force:
        log(f"skip, transcript exists: {txt.name}")
        return txt
    if force:
        ckpt.unlink(missing_ok=True)
    prompt = prompt or default_prompt(audio)
    total = duration(audio)
    segs: list[dict] = []
    pos = 0.0
    if ckpt.exists():  # resume
        for line in ckpt.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            segs.extend(rec["segments"])
            pos = rec["block_end"]
        log(f"resuming {audio.name} at {hms(pos)}")
    t0 = time.monotonic()
    log(f"transcribing {audio.name} ({hms(total)}) with {model}")
    while pos < total - 0.5:
        length = min(BLOCK + BLOCK_SLACK, total - pos)
        block = decode(audio, pos, length)
        regions = speech_regions(block)
        end = length if pos + length >= total - 0.5 else cut_point(regions, BLOCK, length)
        clips = [(s, min(e, end)) for s, e in merge(regions) if s < end]
        raw = transcribe_clips(block[: int(end * SR)], clips, model, prompt)
        kept = [{"start": round(pos + s["start"], 2), "end": round(pos + s["end"], 2), "text": s["text"].strip()}
                for s in raw if keep(s)]
        segs.extend(kept)
        with ckpt.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"block_end": pos + end, "segments": kept}, ensure_ascii=False) + "\n")
        speech = sum(e - s for s, e in clips)
        log(f"  {hms(pos)}–{hms(pos + end)}: {speech / 60:.1f} min speech, {len(kept)}/{len(raw)} segments kept "
            f"({time.monotonic() - t0:.0f} s elapsed)")
        pos += end

    paras = paragraphs(drop_repeats(segs))
    head = [
        f"Přepis: {audio.parent.name} – {audio.stem}" if audio.parent.name else f"Přepis: {audio.stem}",
        f"Zdroj: {audio.name} ({hms(total)}); Whisper {model.split('/')[-1]} (MLX, lokálně); "
        f"přepsáno {datetime.now():%Y-%m-%d %H:%M}.",
        "Mluvčí nejsou rozlišeni. Ticho a přestávky jsou vynechány.",
        "",
    ]
    body = [f"[{hms(a)} - {hms(b)}] {t}" for a, b, t in paras]
    tmp = txt.with_name(txt.stem + ".partial.txt")
    tmp.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    tmp.replace(txt)
    ckpt.unlink(missing_ok=True)
    log(f"done: {txt.name}, {len(paras)} paragraphs, {time.monotonic() - t0:.0f} s")
    return txt


def finished_recordings(root: Path) -> list[Path]:
    out = []
    for m4a in sorted(root.rglob("*.m4a")):
        if m4a.name.endswith(".partial.m4a") or m4a.with_suffix(".txt").exists():
            continue
        if not m4a.with_suffix(".log").exists():  # recorder still finalizing, or not one of ours
            continue
        out.append(m4a)
    return out


def fully_synced(m4a: Path, settle: float = 5.0) -> bool:
    """The file arrives through a cloud sync client: wait until its size has stopped changing and
    ffprobe can read the whole container (an MP4/M4A whose download is unfinished has no usable index)."""
    try:
        a = m4a.stat().st_size
        time.sleep(settle)
        if m4a.stat().st_size != a or a == 0:
            return False
        duration(m4a)
        return True
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False


def notify(title: str, text: str) -> None:
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(text)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def cmd_scan(args) -> int:
    root = Path(args.root).expanduser()
    if not root.is_dir():
        log(f"root not found: {root}")
        return 1
    lock_path = Path.home() / "Library" / "Caches" / "czu-transcribe.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another run is in progress")
            return 0
        todo = finished_recordings(root)
        if not todo:
            return 0
        bad = 0
        for m4a in todo:
            if not fully_synced(m4a):
                log(f"not fully synced yet, next run: {m4a.name}")
                continue
            try:
                txt = transcribe_file(m4a, args.model, None)
                notify("Přepis hotový", f"{m4a.parent.name}: {txt.stem}")
            except Exception as e:  # keep going with the others; the checkpoint lets the next run resume
                bad += 1
                log(f"FAILED {m4a}: {e!r}")
                notify("Přepis selhal", f"{m4a.name}: {e}")
        return 1 if bad else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="czu_transcribe")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("root"); s.add_argument("--model", default=DEFAULT_MODEL)
    f = sub.add_parser("file"); f.add_argument("audio"); f.add_argument("--model", default=DEFAULT_MODEL)
    f.add_argument("--prompt"); f.add_argument("--force", action="store_true")
    a = p.parse_args()
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if a.cmd == "scan":
        return cmd_scan(a)
    transcribe_file(Path(a.audio), a.model, a.prompt, a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
