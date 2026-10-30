#!/bin/bash
# Starts the WhatsApp bridge (Baileys - no browser needed).
# Scan the QR once; credentials persist in .baileys_auth.
set -euo pipefail
cd "$(dirname "$0")"
[ -d node_modules ] || npm install --no-fund --no-audit
exec node bridge.js
