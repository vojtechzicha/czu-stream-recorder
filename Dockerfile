FROM python:3.12-alpine
LABEL org.opencontainers.image.source="https://github.com/vojtechzicha/czu-stream-recorder" \
      org.opencontainers.image.description="Records scheduled ČZU live streams (HLS) to MP4" \
      org.opencontainers.image.licenses="MIT"
RUN apk add --no-cache ffmpeg tzdata ca-certificates
ENV TZ=Europe/Prague PYTHONUNBUFFERED=1
WORKDIR /app
COPY recorder ./recorder
# Runs as the UID/GID set in compose (`user:`), so the files are owned by the host user.
CMD ["python", "-m", "recorder", "run", "--config", "/config/schedule.toml", "--out", "/recordings"]
