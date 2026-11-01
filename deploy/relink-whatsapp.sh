#!/bin/bash
# Re-links the server's WhatsApp bridge with an 8-digit code (no QR needed).
# Use it when WhatsApp unlinked the server (the health check tells you).
#   ./deploy/relink-whatsapp.sh 91XXXXXXXXXX      <- your number with country code
# Then on your phone: WhatsApp > Settings > Linked devices > Link a device >
# "Link with phone number instead", and type the code shown here.
set -euo pipefail
HOST="${ERP_SERVER:-ubuntu@52.66.153.67}"
KEY="${ERP_SERVER_KEY:-$HOME/keys/erp-notice-board.pem}"
PHONE="${1:?usage: $0 <your WhatsApp number with country code, e.g. 919876543210>}"

ssh -i "$KEY" "$HOST" "PHONE='$PHONE' bash -s" <<'REMOTE'
set -e
cd ~/erp-notice-watcher/wa-bridge
sudo systemctl stop erp-wa-bridge
rm -rf .baileys_auth                       # the old, unlinked session is useless
sudo truncate -s 0 bridge.log 2>/dev/null || true
printf '%s\n' "$PHONE" > pair-phone.txt
sudo systemctl start erp-wa-bridge
for i in $(seq 30); do
  code=$(curl -s -m3 127.0.0.1:8766/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("pairingCode") or "")' 2>/dev/null || true)
  [ -n "$code" ] && break; sleep 2
done
[ -n "$code" ] || { echo "No code yet - see: tail ~/erp-notice-watcher/wa-bridge/bridge.log"; exit 1; }
echo
echo "   Your code:  $code"
echo
echo "On your phone: WhatsApp > Linked devices > Link a device > Link with phone number instead"
echo "Waiting for you to enter it (3 minutes)..."
for i in $(seq 90); do
  curl -s -m3 127.0.0.1:8766/status | grep -q '"ready":true' && { echo "Linked. WhatsApp is working again."; exit 0; }
  sleep 2
done
echo "Not linked yet. Run this script again for a fresh code."
exit 1
REMOTE
