#!/usr/bin/env bash
# AI Events — uninstaller.
# Removes launchd job. Leaves data files (config, logs, seen-events,
# state) untouched so you can re-install later if you want.

set -euo pipefail

LABEL="com.siddanth.ai-events"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

echo "==> AI Events uninstaller"

if [[ -f "$PLIST_DEST" ]]; then
    echo "    booting out launchd job"
    launchctl bootout "$DOMAIN" "$PLIST_DEST" >/dev/null 2>&1 || true
    rm -f "$PLIST_DEST"
fi

# Best-effort cleanup of any stale Keychain entry from prior versions.
security delete-generic-password -s "ai-events.luma.subscribed-ics" >/dev/null 2>&1 || true

echo
echo "==> Done. Data files (config, logs, seen-events) preserved."
echo "    Delete the project folder manually to remove them."
