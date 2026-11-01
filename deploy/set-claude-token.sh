#!/bin/bash
# Stores your Claude login token on the server, so the form filler can use
# Claude (Haiku) on your own subscription -- no API key, no per-use charge.
#   1. on your Mac run:  claude setup-token     (opens a browser, prints a token)
#   2. then run this:    ./deploy/set-claude-token.sh
# The token is typed hidden and never written on your Mac.
set -euo pipefail
HOST="${ERP_SERVER:-ubuntu@52.66.153.67}"
KEY="${ERP_SERVER_KEY:-$HOME/keys/erp-notice-board.pem}"
read -r -s -p "Paste the token from 'claude setup-token' (hidden): " TOKEN; echo
[ -n "$TOKEN" ] || { echo "No token given." >&2; exit 1; }
printf '%s\n' "$TOKEN" | ssh -i "$KEY" "$HOST" 'read -r T; cd ~/erp-notice-watcher &&
  grep -v "^CLAUDE_CODE_OAUTH_TOKEN=" .env > .env.tmp; printf "CLAUDE_CODE_OAUTH_TOKEN=%s\n" "$T" >> .env.tmp;
  mv .env.tmp .env; chmod 600 .env; echo "Token saved on the server."'
