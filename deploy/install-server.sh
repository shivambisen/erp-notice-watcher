#!/bin/bash
# One-time server install (Ubuntu): systemd units, log limits, and timers.
# Run from the project directory after ./setup.sh, ./configure.sh and pairing
# WhatsApp once with wa-bridge/start.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo cp deploy/logrotate-erp-watcher /etc/logrotate.d/erp-watcher
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp deploy/journald-erp.conf /etc/systemd/journald.conf.d/erp.conf
sudo systemctl restart systemd-journald
# keep only 2 revisions of each snap instead of 3
command -v snap >/dev/null && sudo snap set system refresh.retain=2 || true

sudo systemctl daemon-reload
sudo systemctl enable --now erp-wa-bridge.service erp-watcher.timer erp-health.timer erp-forms-inbox.timer erp-digest.timer
systemctl list-timers 'erp-*' --no-pager
