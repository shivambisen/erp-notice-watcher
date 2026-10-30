#!/bin/bash
# Installs a launchd agent that polls the notice board every INTERVAL seconds.
set -euo pipefail
cd "$(dirname "$0")"

DIR="$(pwd)"
LABEL="com.shivam.erpnoticewatcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${1:-1800}"

if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "No venv found. Run ./setup.sh first." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/state"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/venv/bin/python</string>
        <string>$DIR/watch.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DIR/state/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/state/launchd.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed $LABEL -- polling every ${INTERVAL}s."
echo
echo "  status:    launchctl list | grep erpnotice"
echo "  run now:   launchctl kickstart -k gui/$UID/$LABEL"
echo "  logs:      tail -f $DIR/state/watcher.log"
echo "  uninstall: launchctl bootout gui/$UID/$LABEL && rm $PLIST"
