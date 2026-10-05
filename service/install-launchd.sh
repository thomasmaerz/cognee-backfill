#!/bin/sh
set -eu

LABEL="com.thomasmaerz.cognee-backfill"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$ROOT/var/log"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
if [ ! -f "$ROOT/.env" ]; then
    echo "Create $ROOT/.env from .env.example before installing" >&2
    exit 1
fi
chmod 600 "$ROOT/.env"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$ROOT/service/run-backfill.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>300</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/backfill.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/backfill.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed and started $LABEL"
echo "Status: launchctl print gui/$(id -u)/$LABEL"
echo "Logs:   $LOG_DIR/backfill.out.log"
