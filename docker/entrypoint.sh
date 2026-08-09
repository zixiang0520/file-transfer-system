#!/bin/sh
set -e
HOST="${FTS_HOST:-0.0.0.0}"
PORT="${FTS_PORT:-8790}"

# ensure writable runtime dirs (bind mounts may reset ownership)
mkdir -p /app/data /app/storage /app/logs 2>/dev/null || true

exec python main.py --host "$HOST" --port "$PORT"
