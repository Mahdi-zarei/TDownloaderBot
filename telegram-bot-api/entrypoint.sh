#!/bin/sh
# Starts the Bot API server in --local mode. TELEGRAM_API_ID / TELEGRAM_API_HASH are read
# by the binary itself; extra arguments (compose `command:`) are passed through.
set -eu

WORK_DIR=/var/lib/telegram-bot-api
TEMP_DIR=/tmp/telegram-bot-api
RUN_AS=telegram-bot-api

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is not set}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is not set}"

# The bind-mounted working dir is usually created by the host user; hand it to uid 101.
mkdir -p "$WORK_DIR" "$TEMP_DIR"
if [ "$(stat -c %u "$WORK_DIR")" != "$(id -u "$RUN_AS")" ]; then
  chown -R "$RUN_AS:$RUN_AS" "$WORK_DIR"
fi
chown "$RUN_AS:$RUN_AS" "$TEMP_DIR"

# Loopback only: with host networking the default 0.0.0.0 would expose the API to the internet.
# The statistics page on 127.0.0.1:8082 doubles as the container healthcheck.
exec telegram-bot-api \
  --local \
  --http-ip-address=127.0.0.1 \
  --http-port=8081 \
  --http-stat-port=8082 \
  --http-stat-ip-address=127.0.0.1 \
  --dir="$WORK_DIR" \
  --temp-dir="$TEMP_DIR" \
  --username="$RUN_AS" \
  --groupname="$RUN_AS" \
  "$@"
