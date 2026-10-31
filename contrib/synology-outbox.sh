#!/bin/bash
# Deliver the recorder's .m4a/.log files from an outbox into a Synology Cloud Sync folder.
#
# Why: Synology Cloud Sync doesn't notice files written through a container's bind mount (the
# recorder's NOTES_DIR); they would never be uploaded. Files created from the host are noticed. So point
# NOTES_DIR at an outbox and let DSM Task Scheduler run this every few minutes, as the same
# unprivileged user the container runs as:
#
#   /bin/bash /volume1/docker/czu-stream-recorder/synology-outbox.sh OUTBOX DESTINATION
#
# The audio is copied first, under a temporary name, and the .log last, because the transcriber treats the .log as
# "recording complete". The outbox keeps the same subfolders as the destination.
set -u
OUT="${1:?usage: synology-outbox.sh OUTBOX DESTINATION}"
DST="${2:?usage: synology-outbox.sh OUTBOX DESTINATION}"
cd "$OUT" 2>/dev/null || exit 0
status=0
while IFS= read -r -d '' log; do
  base="${log%.log}"
  [ -f "$base.m4a" ] || continue            # the recorder writes the .log after the .m4a
  dir="$DST/$(dirname "$log")"
  mkdir -p "$dir" || { status=1; continue; }
  if cp "$base.m4a" "$DST/$base.m4a.copying" && mv -f "$DST/$base.m4a.copying" "$DST/$base.m4a" \
     && cp "$log" "$DST/$log"; then
    rm -f "$base.m4a" "$log"
    echo "$(date '+%F %T') delivered ${base#./}"
  else
    echo "$(date '+%F %T') FAILED ${base#./}" >&2
    rm -f "$DST/$base.m4a.copying"
    status=1
  fi
done < <(find . -type f -name '*.log' -print0)
find . -mindepth 1 -type d -empty -delete 2>/dev/null
exit $status
