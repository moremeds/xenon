#!/usr/bin/env bash
# Installs the launchd job that fires `xenon-futu-history-sync` at 16:30 ET
# daily. Idempotent: re-running unloads and re-loads with the latest plist
# body.
#
# Usage:
#   FUTU_ACCOUNT=12345 ./scripts/launchd/install_futu_history_sync.sh

set -euo pipefail

: "${FUTU_ACCOUNT:?FUTU_ACCOUNT env var required (Futu broker account id)}"

SRC="$(dirname "$0")/com.xenon.futu-history-sync.plist"
DEST="$HOME/Library/LaunchAgents/com.xenon.futu-history-sync.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s/__FUTU_ACCOUNT__/${FUTU_ACCOUNT}/" "$SRC" > "$DEST"

if launchctl list | grep -q com.xenon.futu-history-sync; then
    launchctl unload "$DEST"
fi
launchctl load "$DEST"
echo "Loaded $DEST"
launchctl list | grep com.xenon.futu-history-sync
