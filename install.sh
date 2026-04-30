#!/usr/bin/env bash
# AI Events — one-time installer.
#
# Idempotent: safe to re-run.
#   1. Locks down file permissions (700 on dir, 600 on data files).
#   2. Installs and bootstraps the launchd LaunchAgent.
#   3. Prints next steps.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

LABEL="com.siddanth.ai-events"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> AI Events installer"
echo "    project: $PROJECT_DIR"

# --- macOS check -----------------------------------------------------------
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: this is macOS-only (uses launchd, osascript, Keychain)." >&2
    exit 1
fi

# --- Python check ----------------------------------------------------------
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
    echo "ERROR: python3 not found. Install via 'brew install python' or xcode-select." >&2
    exit 1
fi
echo "    python3: $PY"

# --- Permissions -----------------------------------------------------------
echo "==> Setting file permissions"
chmod 700 "$PROJECT_DIR" || true
chmod 700 "$PROJECT_DIR/run.py" "$PROJECT_DIR/cli.py" 2>/dev/null || true
chmod 600 "$PROJECT_DIR/config.json" 2>/dev/null || true
[[ -f "$PROJECT_DIR/seen-events.json" ]] && chmod 600 "$PROJECT_DIR/seen-events.json" || true
[[ -f "$PROJECT_DIR/state.json"       ]] && chmod 600 "$PROJECT_DIR/state.json"       || true
mkdir -p "$PROJECT_DIR/logs"
chmod 700 "$PROJECT_DIR/logs"

# --- Read run_time from config --------------------------------------------
RUN_TIME="$("$PY" -c '
import json, sys
try:
    cfg = json.load(open("config.json"))
except Exception:
    cfg = {}
print(cfg.get("run_time", "07:00"))
')"
HOUR="${RUN_TIME%%:*}"
MINUTE="${RUN_TIME##*:}"
HOUR=$((10#$HOUR))
MINUTE=$((10#$MINUTE))
echo "    run time: $RUN_TIME (hour=$HOUR minute=$MINUTE)"

# --- Render launchd plist --------------------------------------------------
echo "==> Installing LaunchAgent at $PLIST_DEST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_DEST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$PROJECT_DIR/run.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>$HOUR</integer>
        <key>Minute</key><integer>$MINUTE</integer>
    </dict>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>StandardOutPath</key><string>$PROJECT_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/launchd.err.log</string>
    <key>Nice</key><integer>10</integer>
    <key>LowPriorityIO</key><true/>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST
chmod 644 "$PLIST_DEST"

# --- (Re)load with launchctl ----------------------------------------------
DOMAIN="gui/$(id -u)"
echo "==> Bootstrapping launchd (domain $DOMAIN)"
launchctl bootout "$DOMAIN" "$PLIST_DEST" >/dev/null 2>&1 || true
if ! launchctl bootstrap "$DOMAIN" "$PLIST_DEST" 2>/tmp/aievents-bootstrap.err; then
    echo "ERROR: launchctl bootstrap failed:" >&2
    cat /tmp/aievents-bootstrap.err >&2 || true
    exit 2
fi
rm -f /tmp/aievents-bootstrap.err

echo
echo "==> Done."
echo
echo "Next steps:"
echo "  1. Try a dry run:"
echo "       ./cli.py run-now --dry-run"
echo "  2. Status:"
echo "       ./cli.py status"
