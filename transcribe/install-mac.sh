#!/bin/bash
# Install the transcriber as a launchd agent: every 15 minutes (and at login) it transcribes finished
# recordings under ROOT. Re-run to update the script or change ROOT/MODEL; `--uninstall` removes it.
#
#   transcribe/install-mac.sh "/Users/me/Library/CloudStorage/OneDrive-Personal/…/Video" [model]
set -euo pipefail

LABEL="name.zicha.czu-transcribe"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Library/Application Support/czu-transcribe"
LOG="$HOME/Library/Logs/czu-transcribe.log"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (script and log left in place: $APP, $LOG)"
  exit 0
fi

ROOT="${1:?usage: install-mac.sh ROOT [MODEL] | --uninstall}"
MODEL="${2:-mlx-community/whisper-large-v3-turbo}"
UV="$(command -v uv)" || { echo "uv not found (brew install uv)"; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not found (brew install ffmpeg)"; exit 1; }
[[ -d "$ROOT" ]] || { echo "ROOT does not exist: $ROOT"; exit 1; }
[[ "$(uname -m)" == "arm64" ]] || { echo "MLX needs an Apple Silicon Mac"; exit 1; }

mkdir -p "$APP" "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
cp "$(dirname "$0")/czu_transcribe.py" "$APP/czu_transcribe.py"

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$(xml "$UV")</string><string>run</string><string>--quiet</string>
    <string>$(xml "$APP/czu_transcribe.py")</string>
    <string>scan</string><string>$(xml "$ROOT")</string>
    <string>--model</string><string>$(xml "$MODEL")</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(xml "$(dirname "$UV")"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <!-- not ProcessType=Background: that pins the job to efficiency cores and makes a 7-hour file take hours -->
  <key>Nice</key><integer>5</integer>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL: every 15 min, root $ROOT, model $MODEL"
echo "log: $LOG    run now: launchctl kickstart gui/$(id -u)/$LABEL"
