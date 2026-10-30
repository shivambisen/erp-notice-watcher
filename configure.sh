#!/bin/bash
# Asks for the settings .env needs. Secrets are read with hidden input, so they
# never appear on screen or in shell history. Press Enter to keep a value.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "No .env yet. Run ./setup.sh first." >&2; exit 1; }

# set_env KEY VALUE -- replaces the KEY= line in .env, appends it if absent.
set_env() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  # awk, so the value is never parsed as a sed replacement pattern
  awk -v k="$key" -v v="$val" \
    'index($0, k "=") == 1 { print k "=" v; done=1; next } { print }
     END { if (!done) print k "=" v }' .env > "$tmp"
  cat "$tmp" > .env
  rm -f "$tmp"
}
current() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }

ask() {  # ask KEY "prompt"
  local v
  read -r -p "  $2 [$(current "$1")]: " v
  [ -n "$v" ] && set_env "$1" "$v"
  return 0
}
ask_secret() {  # ask_secret KEY "prompt"
  local v shown="not set"
  [ -n "$(current "$1")" ] && shown="set, Enter keeps it"
  read -r -s -p "  $2 ($shown): " v; echo
  [ -n "$v" ] && set_env "$1" "$v"
  return 0
}

echo "== ERP login =="
ask ERP_USER_ID "Roll number"
ask_secret ERP_PASSWORD "ERP password"
echo "  The ERP asks one of your 3 security questions at random. Give a word from"
echo "  each question and its answer, e.g.  friend = rahul; language = cpp; pet = tom"
ask_secret ERP_SECURITY_ANSWERS "Question words = answer; ..."
ask_secret ERP_SECURITY_ANSWER_DEFAULT "Fallback answer (used if no question matches)"

echo
echo "== OTP mailbox (the address your ERP OTP is sent to) =="
ask IMAP_USER "Email"
echo "  App password: https://myaccount.google.com/apppasswords (needs 2-Step Verification)"
ask_secret IMAP_PASSWORD "App password"

echo
echo "== Database (needed for auto-apply) =="
echo "  Free Postgres at https://neon.tech -> new project -> copy the connection string"
ask_secret DATABASE_URL "postgresql://..."

echo
echo "== Auto-apply =="
ask AUTO_APPLY "Apply automatically? on/off"
ask AUTO_APPLY_CV "Which CV to apply with (1/2/3)"

echo
echo "== WhatsApp =="
echo "  Group id (...@g.us) or number (+91...). Leave blank and pick the group"
echo "  later in the web UI: ./venv/bin/python serve.py -> Settings -> Find my groups"
ask WHATSAPP_TO "Send to"

chmod 600 .env
echo
echo "Saved. Check it with:  ./venv/bin/python watch.py --check-config"
