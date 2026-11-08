#Requires -Version 7
# Windows: install whisper.cpp (native build for this CPU) and the models for czu_transcribe.py. No scheduler: on
# Windows transcription is manual (the Mac keeps the launchd job). Re-run to update; -Force re-downloads everything.
#
#   pwsh transcribe/install-windows.ps1 [-Model ggml-large-v3-turbo-q8_0.bin] [-Force]
#   uv run transcribe/czu_transcribe.py file "…\Video\<Předmět>\<YYYY-MM-DD> Přednáška.m4a"
#   uv run transcribe/czu_transcribe.py scan "…\Video"          # all finished recordings without a .txt
#
# Needs uv, ffmpeg/ffprobe (winget Gyan.FFmpeg) and gh (for the release download). The Adreno/OpenCL build crashed on
# a Snapdragon X Elite (2026-11), so the CPU build is used; ~5x real time with large-v3-turbo q8_0 on 8 threads.
param([string]$Model = 'ggml-large-v3-turbo-q8_0.bin', [switch]$Force)
$ErrorActionPreference = 'Stop'
$W = Join-Path $env:LOCALAPPDATA 'Programs\whisper.cpp'
foreach ($c in 'uv', 'ffmpeg', 'ffprobe', 'gh') {
  if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { throw "$c not found on PATH" }
}
$asset = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'whisper-bin-win-cpu-arm64.zip' } else { 'whisper-bin-x64.zip' }
New-Item -ItemType Directory -Force -Path "$W\dl", "$W\models" | Out-Null

if ($Force -or -not (Test-Path "$W\cpu\Release\whisper-cli.exe")) {
  # the newest release (pre-releases included) that carries the asset; the tagged v* releases often have no binaries
  $tag = gh api 'repos/ggml-org/whisper.cpp/releases?per_page=15' --jq "[.[] | select(any(.assets[]; .name == `"$asset`"))][0].tag_name"
  if (-not $tag) { throw "no whisper.cpp release with $asset" }
  gh release download $tag --repo ggml-org/whisper.cpp --pattern $asset --dir "$W\dl" --clobber
  if (Test-Path "$W\cpu") { Remove-Item -Recurse -Force "$W\cpu" }
  Expand-Archive "$W\dl\$asset" "$W\cpu" -Force
  Set-Content "$W\VERSION" "$tag $asset"
  "whisper.cpp $tag ($asset)"
}
$hf = @{ $Model = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$Model"
         'ggml-silero-v5.1.2.bin' = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin' }
foreach ($m in $hf.Keys) {
  $p = Join-Path "$W\models" $m
  if ($Force -or -not (Test-Path $p) -or (Get-Item $p).Length -lt 100KB) {
    curl.exe -fL --retry 3 -o $p $hf[$m]; if ($LASTEXITCODE) { throw "download failed: $m" }
  }
  "{0,-32} {1,8:N0} MB" -f $m, ((Get-Item $p).Length / 1MB)
}
& "$W\cpu\Release\whisper-cli.exe" --help *> $null
if ($LASTEXITCODE -ne 0) { throw 'whisper-cli.exe does not start' }
"ok: $W"
