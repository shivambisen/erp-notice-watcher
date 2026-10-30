#!/bin/bash
# Runs the WhatsApp bridge as a login agent so it survives closing the terminal
# and restarts if it crashes. Pair it once with ./start.sh before installing.
set -euo pipefail
cd "$(dirname "$0")"

DIR="$(pwd)"
LABEL="com.shivam.erpwabridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
NODE="$(command -v node)"

if [ ! -d "$DIR/.baileys_auth" ]; then
  echo "Not paired yet. Run ./start.sh and scan the QR first." >&2
  exit 1
fi
if [ -z "$NODE" ]; then
  echo "node not found on PATH" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$NODE</string>
        <string>$DIR/bridge.js</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>$DIR/logs/bridge.out.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/logs/bridge.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed $LABEL - the bridge now runs in the background."
echo
echo "  status:    launchctl list | grep erpwabridge"
echo "  logs:      tail -f $DIR/logs/bridge.out.log"
echo "  restart:   launchctl kickstart -k gui/$UID/$LABEL"
echo "  uninstall: launchctl bootout gui/$UID/$LABEL && rm $PLIST"
