#!/usr/bin/env bash
# Nightly refresh: rebuild the bot image with the latest yt-dlp (+ its yt-dlp-ejs) and gallery-dl,
# restart the bot, drop dangling images. Safe to run from cron or over SSH.
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."

log() { printf '%s update.sh: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

exec 9>"${TMPDIR:-/tmp}/novabot-update.lock"
if ! flock -n 9; then
  log "another update is running, skipping"
  exit 0
fi

log "building bot in $PWD"
docker compose build --build-arg YTDLP_REFRESH="$(date +%s)" bot

log "restarting bot"
docker compose up -d bot

if versions=$(docker compose exec -T bot sh -c 'echo "yt-dlp $(yt-dlp --version), gallery-dl $(gallery-dl --version)"'); then
  log "$versions"
else
  log "warning: could not query versions; check: docker compose logs bot"
fi

log "pruning dangling images"
docker image prune -f

log "done"
