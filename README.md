# czu-stream-recorder

[![CI](https://github.com/vojtechzicha/czu-stream-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/vojtechzicha/czu-stream-recorder/actions/workflows/ci.yml)

Records scheduled sessions from the live streams of the Czech University of Life Sciences Prague (ČZU), such as
[Kongresový sál Knihovny ČZU](https://video.czu.cz/live/kongresovy-sal/), so you can watch a lecture you couldn't
attend. It runs as a small Docker container on an always-on machine (a Synology NAS, a home server, a Raspberry Pi)
and turns each scheduled time window into one MP4 file.

> *Česky:* nahrává naplánované přednášky z živých přenosů ČZU (video.czu.cz/live) do MP4, typicky na domácím NASu.

## Why not just ffmpeg?

The ČZU streams are HLS from a Wowza server: 1080p / 720p / 360p, 10-second MPEG-TS segments, and **only a 30-second
window with no DVR**. Anything missed is gone, so the recorder must be running while the session is on. A single
long `ffmpeg -i playlist.m3u8` connection can hang on this server ("Error reading HTTP response: End of file").
This recorder downloads segment by segment instead, so it can recover from these failures:

- **Expired Wowza sessions and network drops** are retried, and the playlist is re-resolved when needed.
- **Missed segments are counted.** Each gap is logged with its length.
- **A container restart in the middle of a window resumes** into the same set of parts. A restart after the window
  ends joins whatever was recorded.
- **A server-side stream restart** (the media sequence starts over) is detected and recording continues.

At the end, ffmpeg joins the parts into `YYYY-MM-DD <title>.mp4`. The video is copied without re-encoding. The audio
is levelled with `dynaudnorm`, because room microphones are often quiet (the Kongresový sál feed is about −37 dB).
The recorder also writes `…m4a` (audio only, for listening or transcription) and `…log`, either next to the MP4 or
in a separate notes folder. For example, the MP4 can go to a Plex library while the audio and log go to a folder
synced to the cloud.

Size: about **1.5 GB per hour** at 1080p. `max_height = 720` roughly halves it.

## Quick start (Docker)

```sh
mkdir czu-stream-recorder && cd czu-stream-recorder
curl -LO https://raw.githubusercontent.com/vojtechzicha/czu-stream-recorder/main/compose.yaml
mkdir config && curl -L -o config/schedule.toml \
  https://raw.githubusercontent.com/vojtechzicha/czu-stream-recorder/main/config/schedule.example.toml
# edit config/schedule.toml, then optionally create .env (see below)
docker compose up -d
docker compose logs -f
```

`.env` (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `RECORDINGS_DIR` | `./recordings` | Where the MP4 files go (e.g. a Plex library such as `/volume1/Media/Courses`) |
| `NOTES_DIR` | `./notes` | Where each recording's `.m4a` and `.log` go |
| `PUID` / `PGID` | `1000` / `100` | The user the recorder runs as. Use a dedicated unprivileged user that can write only the mounted folders |
| `TZ` | `Europe/Prague` | Time zone used to read the schedule |
| `IMAGE_TAG` | `latest` | Image version. Pin a build (`sha-xxxxxxx` from the packages page) so updates only happen when you change it |

The image `ghcr.io/vojtechzicha/czu-stream-recorder` is built for `linux/amd64` and `linux/arm64`.

### Synology DSM

1. Create a DSM user for the container, e.g. `czu-recorder`: Control Panel → User & Group → Create. Give it a
   random password, leave every shared folder **unset** (not "No access": a deny entry overrides any allow), deny
   all applications, and keep it out of the administrators group. Its UID is `PUID`, and `PGID` is `100` (`users`).
2. In File Station → Properties → Permission, give that user **Read & Write** (this folder, child folders, files)
   on just the folders the container mounts: the recordings folder, the notes folder and the project folder.
   Mount those folders directly (`RECORDINGS_DIR=/volume1/Media/Courses/Lectures`, not the share root). That way
   the container never traverses the parent shares and needs no permissions on them.
3. Install **Container Manager**. Put `compose.yaml`, `.env` and `config/schedule.toml` in
   `/volume1/docker/czu-stream-recorder`, add an empty `work/` folder, and create a project from that folder.

4. **If `NOTES_DIR` should reach the cloud through Cloud Sync,** don't mount the Cloud Sync folder directly.
   Cloud Sync doesn't notice files written through a container's bind mount, so they would never upload. Set
   `NOTES_DIR` to an outbox (e.g. `/volume1/docker/czu-stream-recorder/outbox`) and add a Task Scheduler job
   (user-defined script, run as the same user, every 5 minutes) that delivers the files from the host:
   `/bin/bash /volume1/docker/czu-stream-recorder/synology-outbox.sh <outbox> "<Cloud Sync folder>"`
   (the script is [`contrib/synology-outbox.sh`](contrib/synology-outbox.sh)).

At startup, the recorder logs an error (and keeps running) for any mounted folder it can't write. Segment parts
stay in `work/` while recording, so a Plex library never sees half-finished files.

## Schedule

```toml
[defaults]
stream = "kongresovy-sal"
lead_minutes = 10        # start this much earlier
tail_minutes = 20        # and stop this much later

[streams]
kongresovy-sal = "https://wcavp-wowza.czu.cz:4443/live-kongresovy-sal/smil:live-kongresovy-sal.smil/playlist.m3u8"

[[recording]]
title = "Přednáška"
start = 2026-11-21T08:45:00
end   = 2026-11-21T15:30:00
# stream = "…"  max_height = 720  lead_minutes = 0  tail_minutes = 30
# video = "ČZU/S01 Ekonomika pro manažery/s01e02.Cvičení"   # MP4 path under RECORDINGS_DIR, no extension
# notes = "Ekonomika pro manažery/2026-11-21 Cvičení"       # .m4a/.log path under NOTES_DIR, no extension
```

The file is re-read every 20 seconds, so you can add sessions without restarting. To record another room, open its page on
video.czu.cz/live, find the `<source src="….m3u8">` in the page source and add it under `[streams]`. Any other
HLS live stream should work too.

## Commands

```sh
python -m recorder check  --config config/schedule.toml              # parse the schedule, probe every stream
python -m recorder list   --config config/schedule.toml --out DIR    # schedule and status of each recording
python -m recorder record --config config/schedule.toml --stream kongresovy-sal --minutes 2 --title Test --out DIR
python -m recorder run    --config config/schedule.toml --out DIR    # the scheduler (the container's default command)
```

In the container: `docker exec czu-stream-recorder python -m recorder check --config /config/schedule.toml`.
Each recording's progress is in `work/<id>/state.json` and `recording.log`. Its parts are deleted once the
MP4 is written; if joining fails, they are kept. Outside Docker, `--notes` and `--work` default to the `--out` folder
and `<out>/.work`.

Requirements without Docker: Python ≥ 3.11 (standard library only) and `ffmpeg`/`ffprobe` on the `PATH`. The
transcriber needs [uv](https://docs.astral.sh/uv/), which installs its dependencies on first run.

## Transcripts (optional, Apple Silicon Mac)

`transcribe/czu_transcribe.py` turns each recording's `.m4a` into a Czech transcript with
[MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) (`whisper-large-v3-turbo`). It runs
locally on the Mac's GPU, so there's no API key and nothing to pay. The transcript is written next to the audio as
`<name>.txt` in the style of the Plaud CLI:

```
[00:12:31 - 00:13:02] Co se týká objektivizované hodnoty, tak tu byste v literatuře našli i pod pojmem nesporný základ…
```

- Silence and breaks are removed with Silero VAD before Whisper sees the audio. Whisper invents text on
  silence otherwise (the classic "Titulky vytvořil…"), and a few known phantom phrases are filtered as well.
- The subject is taken from the parent folder name and used as the prompt, which helps with terminology.
- Work is checkpointed every ~30 minutes of audio, so a Mac that goes to sleep just resumes on the next run.
  On an M2 Pro, transcription runs at about 10× real time (a 7-hour day takes about 40 minutes).
- Speakers are not separated.

Point `NOTES_DIR` at a folder the Mac sees (for example one synced to OneDrive via Cloud Sync), then install the agent:

```sh
transcribe/install-mac.sh "$HOME/Library/CloudStorage/OneDrive-Personal/…/Video"
```

It runs every 15 minutes and at login, picks up every `.m4a` that has the recorder's `.log` next to it (the log is
written last) and no `.txt` yet, keeps the Mac awake while it works, and posts a notification when a transcript is
ready. The log is in `~/Library/Logs/czu-transcribe.log`. Run `install-mac.sh --uninstall` to remove it. For a single
file: `uv run transcribe/czu_transcribe.py file recording.m4a`.

## Development

```sh
python -m unittest discover -s tests -t .
docker build -t czu-stream-recorder .
```

## Disclaimer

This is an unofficial personal project, not affiliated with ČZU. It records streams that the university publishes
openly, for your own study. Recordings of lectures are subject to the lecturer's rights and the university's rules:
don't redistribute them.

## License

[MIT](LICENSE)
