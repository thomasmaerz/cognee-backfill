#!/bin/sh
set -eu

LABEL="com.thomasmaerz.cognee-backfill"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "Removed $LABEL"
