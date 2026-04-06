#!/bin/bash
#
# MenthorQ CTA Sync Scheduled Service Manager
#
# Runs a dedicated CTA cache refresh after the close at two verification windows.
# `RunAtLoad` provides catch-up behavior on reboot/login/wake if the machine was
# asleep during the scheduled windows. ET slots are converted into local machine
# time at install time so launchd fires correctly on this machine.
#
# Usage:
#   ./scripts/setup_cta_sync_service.sh install   - Install and load service
#   ./scripts/setup_cta_sync_service.sh uninstall - Stop and remove service
#   ./scripts/setup_cta_sync_service.sh status    - Check service status
#   ./scripts/setup_cta_sync_service.sh logs      - Tail service logs
#   ./scripts/setup_cta_sync_service.sh start     - Run CTA sync manually now

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.radon.cta-sync.plist"
PLIST_SRC="$PROJECT_DIR/config/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LABEL="com.radon.cta-sync"
LOG_DIR="$PROJECT_DIR/logs"
WRAPPER="$PROJECT_DIR/scripts/run_cta_sync.sh"

generate_plist() {
    local entries
    entries=$(PROJECT_DIR_ENV="$PROJECT_DIR" python3.13 - <<'PY'
import os
import sys

project_dir = os.environ["PROJECT_DIR_ENV"]
sys.path.insert(0, os.path.join(project_dir, "scripts"))

from utils.cta_sync import CTA_SYNC_ET_SLOTS
from utils.launchd_calendar import build_local_calendar_entries, render_calendar_interval_xml

entries = build_local_calendar_entries(CTA_SYNC_ET_SLOTS, weekdays=[1, 2, 3, 4, 5])
print(render_calendar_interval_xml(entries), end="")
PY
)

    cat > "$PLIST_SRC" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${WRAPPER}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>StartCalendarInterval</key>
    <array>
${entries}    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/cta-sync.out.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/cta-sync.err.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST
}

install() {
    echo "Installing CTA Sync service..."
    echo ""

    if [[ ! -f "$WRAPPER" ]]; then
        echo "ERROR: Wrapper script not found at $WRAPPER"
        exit 1
    fi
    chmod +x "$WRAPPER"

    if [[ ! -f "$PROJECT_DIR/scripts/fetch_menthorq_cta.py" ]]; then
        echo "ERROR: fetch_menthorq_cta.py not found"
        exit 1
    fi

    mkdir -p "$LOG_DIR"
    mkdir -p "$PROJECT_DIR/data/menthorq_cache"
    echo "  Log directory: $LOG_DIR"
    echo "  Data directory: data/menthorq_cache/"

    echo "  Generating plist (10 schedule entries + RunAtLoad catch-up)..."
    generate_plist

    if ! plutil -lint "$PLIST_SRC" > /dev/null 2>&1; then
        echo "ERROR: Generated plist is invalid"
        plutil -lint "$PLIST_SRC"
        exit 1
    fi
    echo "  Plist validated OK"

    launchctl unload "$PLIST_DST" 2>/dev/null || true

    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load "$PLIST_DST"

    echo ""
    echo "Service installed and loaded."
    echo ""
    status
}

uninstall() {
    echo "Uninstalling CTA Sync service..."

    if [[ -f "$PLIST_DST" ]]; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "Service removed."
    else
        echo "Service not installed."
    fi
}

status() {
    echo "CTA Sync Service Status"
    echo "======================="

    if [[ ! -f "$PLIST_DST" ]]; then
        echo "Service: NOT INSTALLED"
        echo ""
        echo "Run: ./scripts/setup_cta_sync_service.sh install"
        return 1
    fi

    if launchctl list 2>/dev/null | grep -q "$LABEL"; then
        echo "Service: LOADED"
    else
        echo "Service: NOT LOADED (plist exists but not loaded)"
    fi

    echo "Schedule: 4:15 PM ET and 5:00 PM ET, Mon-Fri"
    echo "Catch-up behavior: RunAtLoad covers reboot/login/wake when the machine missed the scheduled windows."

    local latest
    latest=$(ls -t "$PROJECT_DIR/data/menthorq_cache"/cta_20*.json 2>/dev/null | head -1)
    if [[ -n "$latest" ]]; then
        local filename
        filename=$(basename "$latest")
        echo "Latest cache: $filename"
    else
        echo "Latest cache: (none yet)"
    fi

    if [[ -f "$LOG_DIR/cta-sync.out.log" ]]; then
        echo ""
        echo "Recent log:"
        tail -5 "$LOG_DIR/cta-sync.out.log" 2>/dev/null || echo "  (empty)"
    fi
}

logs() {
    echo "Tailing CTA Sync logs..."
    echo "(Ctrl+C to stop)"
    echo ""
    tail -f "$LOG_DIR"/cta-sync.out.log "$LOG_DIR"/cta-sync.err.log 2>/dev/null
}

start_now() {
    echo "Running CTA sync manually..."
    exec bash "$WRAPPER"
}

case "${1:-status}" in
    install)
        install
        ;;
    uninstall)
        uninstall
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    start)
        start_now
        ;;
    *)
        echo "Usage: $0 {install|uninstall|status|logs|start}"
        exit 1
        ;;
esac
