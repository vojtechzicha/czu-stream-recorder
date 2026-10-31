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
The recorder also writes `…m4a` (audio only, for listening or transcription) and `…log`.

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
| `RECORDINGS_DIR` | `./recordings` | Where the MP4/M4A files go (e.g. `/volume1/Media/Lectures` on a Synology) |
| `PUID` / `PGID` | `1000` / `100` | Owner of the files (`id <user>` on the host; Synology DSM users start at 1026, group `users` = 100) |
| `TZ` | `Europe/Prague` | Time zone used to read the schedule |

The image `ghcr.io/vojtechzicha/czu-stream-recorder` is built for `linux/amd64` and `linux/arm64`.

### Synology DSM

Install **Container Manager**. Put `compose.yaml`, `.env` and `config/schedule.toml` in
`/volume1/docker/czu-stream-recorder`, then create a project from that folder in Container Manager (or run
`sudo docker compose up -d` there over SSH). The target folder must exist and be writable by `PUID`.

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
Each recording's progress is in `<out>/.work/<id>/state.json` and `recording.log`. Its parts are deleted once the
MP4 is written; if joining fails, they are kept.

Requirements without Docker: Python ≥ 3.11 (standard library only) and `ffmpeg`/`ffprobe` on the `PATH`.

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
