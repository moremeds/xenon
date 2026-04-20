#!/bin/bash
#
# Monitor daemon wrapper for launchd
#
# Executes xenon-monitor-daemon --once via the project venv. launchd fires this
# every 60 seconds; individual handlers manage their own cadence.
#

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"
export PATH="$PROJECT_DIR/.venv/bin:$PATH"

if [ ! -x "$PROJECT_DIR/.venv/bin/xenon-monitor-daemon" ]; then
    echo "$(date): xenon-monitor-daemon not found at $PROJECT_DIR/.venv/bin/ — run 'uv sync'"
    exit 1
fi

mkdir -p logs

exec "$PROJECT_DIR/.venv/bin/xenon-monitor-daemon" --once "$@"
