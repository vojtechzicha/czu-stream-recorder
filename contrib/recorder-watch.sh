#!/bin/bash
# Watch the recorder from the Synology host and report what happens to each scheduled recording.
#
# Run it from DSM Task Scheduler every 5 minutes, as the same unprivileged user as the container, with
# "Send run details by email" + "only when the script terminates abnormally":
#
#   /bin/bash /volume1/docker/czu-stream-recorder/recorder-watch.sh [STATUS_FILE]
#
# The script exits 1 whenever it has news, so DSM e-mails its output. Each event is reported once:
#   PRE-FLIGHT  in the hour before a window: the stream answers (or not)
#   STARTED     the recorder is writing segments
#   PROBLEM     the window started but nothing is recording, no new segment for 3+ min, the recording failed,
#               or the hall has been silent for 15 min during the scheduled session (repeated at most every 30/60 min)
#   DONE        the MP4 is finalized (length and missed seconds); a warning if the audio isn't delivered later
# STATUS_FILE (optional, e.g. in a Cloud Sync folder) gets a short progress summary on every run near a window.
#
# It needs no Docker access: it reads config/schedule.toml and work/<id>/{state.json,parts/,levels.jsonl}.
# Per-recording lead_minutes/tail_minutes overrides are ignored; the [defaults] values are used.
set -u
P="${RECORDER_DIR:-/volume1/docker/czu-stream-recorder}"
STATUS="${1:-}"
SCHED="$P/config/schedule.toml"
WORK="$P/work"
OUTBOX="${OUTBOX_DIR:-$P/outbox}"
SEEN="$P/watch"
NOW="${WATCH_NOW:-$(date +%s)}"
mkdir -p "$SEEN" || { echo "PROBLEM: cannot create $SEEN"; exit 1; }
[ -r "$SCHED" ] || { echo "PROBLEM: cannot read $SCHED"; exit 1; }

news=()      # lines to e-mail
status=()    # lines for STATUS_FILE
active=0

ts() { date -d "@$1" '+%a %d.%m. %H:%M'; }
# once KEY [MIN_INTERVAL_S]: true if KEY wasn't reported yet (or was reported more than MIN_INTERVAL_S ago)
once() {
  local f="$SEEN/$1" last
  if [ -f "$f" ]; then
    [ -n "${2:-}" ] || return 1
    last=$(cat "$f" 2>/dev/null || echo 0)
    [ $((NOW - last)) -ge "$2" ] || return 1
  fi
  echo "$NOW" > "$f"
}
num() { grep -o "\"$2\": *-\?[0-9.]*" "$1" 2>/dev/null | head -1 | sed 's/.*: *//'; }
str() { grep -o "\"$2\": *\"[^\"]*\"" "$1" 2>/dev/null | head -1 | sed 's/.*: *"//; s/"$//'; }
hms() { local s=${1%.*}; printf '%d:%02d' $((s / 3600)) $((s % 3600 / 60)); }

def() { awk -v k="$1" '/^\[defaults\]/{d=1;next} /^\[/{d=0} d && $1==k {sub(/^[^=]*= */,""); sub(/ *#.*/,""); print; exit}' "$SCHED"; }
LEAD=$(def lead_minutes); LEAD=${LEAD:-10}
TAIL=$(def tail_minutes); TAIL=${TAIL:-20}
DB=$(def silence_threshold_db); DB=${DB:--45}
STREAM_NAME=$(def stream | tr -d '"')
STREAM_URL=$(awk -v k="$STREAM_NAME" '/^\[streams\]/{s=1;next} /^\[/{s=0} s && $1==k {sub(/^[^=]*= *"/,""); sub(/".*/,""); print; exit}' "$SCHED")

# one line per recording: start|end|title
recs=$(awk '
  function flush() { if (st != "") print st "|" en "|" ti; st = en = ti = "" }
  /^\[\[recording\]\]/ { flush(); r = 1; next }
  /^\[/ { flush(); r = 0; next }
  r && $1 == "title" { v = $0; sub(/^[^=]*= *"/, "", v); sub(/".*/, "", v); ti = v }
  r && $1 == "start" { v = $0; sub(/^[^=]*= */, "", v); sub(/ *#.*/, "", v); st = v }
  r && $1 == "end"   { v = $0; sub(/^[^=]*= */, "", v); sub(/ *#.*/, "", v); en = v }
  END { flush() }' "$SCHED")

while IFS='|' read -r start end title; do
  [ -n "$start" ] || continue
  s=$(date -d "${start/T/ }" +%s 2>/dev/null) && e=$(date -d "${end/T/ }" +%s 2>/dev/null) \
    || { once "badtime-$start" && news+=("PROBLEM: cannot read start/end of '$title' ($start)"); continue; }
  ws=$((s - LEAD * 60)); we=$((e + TAIL * 60))
  [ "$NOW" -ge $((ws - 3600)) ] && [ "$NOW" -le $((we + 7200)) ] || continue
  active=1
  key=$(date -d "@$s" +%Y%m%d-%H%M)
  dir=$(ls -d "$WORK/$key-"* 2>/dev/null | head -1)
  label="$title ($(ts "$s")–$(date -d "@$e" +%H:%M))"

  if [ "$NOW" -lt "$ws" ]; then                                   # the hour before the window
    if once "$key.preflight"; then
      if [ -n "$STREAM_URL" ] && curl -fsS -m 20 -o /dev/null "$STREAM_URL" 2>/dev/null; then
        news+=("PRE-FLIGHT OK: $label starts recording at $(date -d "@$ws" +%H:%M); the stream answers.")
      else
        news+=("PROBLEM (pre-flight): $label: the stream doesn't answer right now ($STREAM_URL). It may only go live at the start.")
      fi
    fi
    status+=("$label: waiting, recording starts $(date -d "@$ws" +%H:%M)")
    continue
  fi

  if [ -z "$dir" ] || [ ! -f "$dir/state.json" ]; then
    if [ "$NOW" -ge $((ws + 180)) ] && once "$key.nostart" 1800; then
      news+=("PROBLEM: $label: the window opened at $(date -d "@$ws" +%H:%M) but the recorder hasn't started (is the container running?).")
    fi
    status+=("$label: NOT STARTED")
    continue
  fi

  st="$dir/state.json"
  phase=$(str "$st" phase); segs=$(num "$st" segments); rec=$(num "$st" recorded_seconds)
  missed=$(num "$st" missed_seconds); msg=$(str "$st" message)
  case "$phase" in
    recording|pending)
      newest=$(ls -t "$dir/parts/"*.ts 2>/dev/null | head -1)
      age=-1; [ -n "$newest" ] && age=$((NOW - $(stat -c %Y "$newest")))
      loud=""; last=""
      if [ -f "$dir/parts/levels.jsonl" ]; then
        # segments in the last 15 min (90 × 10 s) with a peak above the silence threshold
        loud=$(tail -n 90 "$dir/parts/levels.jsonl" | grep -o '"peak": *-\?[0-9.]*' | sed 's/.*: *//' \
               | awk -v t="$DB" '$1 > t {n++} END {print n + 0}')
        last=$(tail -n 1 "$dir/parts/levels.jsonl" | grep -o '"peak": *-\?[0-9.]*' | sed 's/.*: *//')
      fi
      if [ -n "$newest" ] && [ "$age" -le 180 ]; then
        if once "$key.started"; then
          news+=("STARTED: $label is recording (${segs:-0} segments so far, last peak ${last:-?} dB).")
        fi
        if [ -f "$SEEN/$key.stale" ]; then
          news+=("RECOVERED: $label: segments are arriving again (missed ~${missed:-0} s in total).")
          rm -f "$SEEN/$key.stale"
        fi
      elif [ "$NOW" -ge $((ws + 180)) ] && once "$key.stale" 1800; then
        if [ -z "$newest" ]; then
          news+=("PROBLEM: $label: the recorder is on but hasn't received a single segment (stream down?).")
        else
          news+=("PROBLEM: $label: no new segment for $((age / 60)) min (stream stopped or recorder stuck). Recorded so far $(hms "${rec:-0}"), missed ~${missed:-0} s.")
        fi
      fi
      lines=$(tail -n 90 "$dir/parts/levels.jsonl" 2>/dev/null | wc -l)
      if [ "${loud:-1}" = 0 ] && [ "$lines" -ge 90 ] && [ "$NOW" -ge $((s + 900)) ] && [ "$NOW" -le "$e" ] \
         && once "$key.silent" 3600; then
        news+=("WARNING: $label: the hall has been silent for 15 min (all peaks below $DB dB). A break, or nobody in the Kongresový sál?")
      fi
      status+=("$label: recording, $(hms "${rec:-0}") recorded, missed ~${missed:-0} s, last segment ${age} s ago, last peak ${last:-?} dB, loud segments in last 15 min: ${loud:-?}/90")
      ;;
    finalizing)
      status+=("$label: finalizing (joining parts into the MP4)") ;;
    done)
      once "$key.done" && news+=("DONE: $label: $msg")
      pending=$(find "$OUTBOX" -type f -name "$(date -d "@$s" +%Y-%m-%d)*" 2>/dev/null | head -1)
      fin=$(stat -c %Y "$st")
      if [ -n "$pending" ] && [ $((NOW - fin)) -ge 1800 ] && once "$key.undelivered" 3600; then
        news+=("PROBLEM: $label: the audio is still in the outbox $(( (NOW - fin) / 60 )) min after finishing (Cloud Sync delivery task?).")
      fi
      status+=("$label: done, $msg$([ -n "$pending" ] && echo ', audio waiting in the outbox')") ;;
    failed)
      once "$key.failed" && news+=("PROBLEM: $label: the recording FAILED: $msg (parts kept in $dir/parts).")
      status+=("$label: FAILED: $msg") ;;
    *)
      status+=("$label: unknown phase '$phase'") ;;
  esac
done <<< "$recs"

if [ -n "$STATUS" ] && [ "$active" = 1 ]; then
  { echo "czu-stream-recorder status, checked $(date -d "@$NOW" '+%Y-%m-%d %H:%M:%S')"
    printf '%s\n' "${status[@]}"; } > "$STATUS.tmp" && mv -f "$STATUS.tmp" "$STATUS"
fi

if [ ${#news[@]} -gt 0 ]; then
  printf '%s\n' "${news[@]}"
  echo
  printf '%s\n' "${status[@]}"
  exit 1
fi
exit 0
