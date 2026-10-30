#!/bin/bash
# One-time install: Python deps, the WhatsApp bridge's deps, and a .env to fill in.
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
echo "Python environment ready."

if command -v npm >/dev/null; then
  (cd wa-bridge && npm install --no-fund --no-audit --silent)
  echo "WhatsApp bridge ready."
else
  echo "npm not found - install Node.js 20+ to use WhatsApp (brew install node)."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi
chmod 600 .env

echo
echo "Next: ./configure.sh"
