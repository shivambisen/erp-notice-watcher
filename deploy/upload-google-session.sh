#!/bin/bash
# Gives the form filler your Google session, so it can submit CDC forms that
# record a verified @kgpian email.
#   1. In Chrome, signed in to your kgpian account, install the extension
#      "Get cookies.txt LOCALLY", open docs.google.com/forms, click it, Export.
#   2. ./deploy/upload-google-session.sh ~/Downloads/docs.google.com_cookies.txt
# The file is copied to the server (readable only by you) and deleted here.
# Treat it like a password: it lets the holder act as your Google account.
set -euo pipefail
HOST="${ERP_SERVER:-ubuntu@52.66.153.67}"
KEY="${ERP_SERVER_KEY:-$HOME/keys/erp-notice-board.pem}"
FILE="${1:?usage: $0 path/to/cookies.txt}"
grep -q "google.com" "$FILE" || { echo "That file has no google.com cookies." >&2; exit 1; }
scp -q -i "$KEY" "$FILE" "$HOST:erp-notice-watcher/state/google_cookies.txt"
ssh -i "$KEY" "$HOST" 'chmod 600 ~/erp-notice-watcher/state/google_cookies.txt'
rm -P "$FILE" 2>/dev/null || rm -f "$FILE"
echo "Google session saved on the server and removed from this Mac."
