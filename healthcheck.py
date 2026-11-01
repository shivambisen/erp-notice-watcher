#!/usr/bin/env python3
"""Checks that the watcher is healthy, fixes what it safely can, and reports.

Run on a timer (every 6 hours on the server). It looks at:
  - the last watcher run: recent, and did it actually check the board?
  - the schedule: is the watcher's timer still on?
  - the WhatsApp bridge: connected? (restarted if not)
  - disk space (cleans up safe junk below 25% free), the WhatsApp retry
    queue, and failed auto-applies

Then sends ONE WhatsApp message to your own chat ("Message yourself"), or
HEALTH_NOTIFY_TO: all good, or what is wrong and what it already fixed. If
WhatsApp itself cannot be reached it falls back to one email to IMAP_USER, even
with MAIL_ENABLED=off -- otherwise a dead bridge would fail silently.

A regular "all good" is the point: if those stop arriving, the server itself is
down, which nothing running on it could ever report.

  python healthcheck.py            check, fix, report
  python healthcheck.py --dry-run  check and print only; fixes and sends nothing
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time

import requests

import config

log = logging.getLogger("health")

STALE_AFTER = int(os.getenv("HEALTH_STALE_MINUTES", "35")) * 60  # timer runs every 10 min
DISK_MIN_FREE = 0.10
CLEAN_BELOW = 0.25  # start deleting safe junk well before it is a problem
ON_SYSTEMD = shutil.which("systemctl") is not None


def _sh(*cmd) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} min ago" if m < 120 else f"{m // 60}h {m % 60}m ago"


def bridge_status() -> dict:
    try:
        return requests.get(f"{config.WA_BRIDGE_URL}/status", timeout=5).json() or {}
    except Exception as exc:
        return {"error": str(exc)[:200]}


class Report:
    def __init__(self):
        self.ok, self.problems, self.fixed = [], [], []


def check_last_run(r: Report, dry: bool):
    try:
        last = json.loads((config.STATE_DIR / "last_run.json").read_text())
    except (OSError, ValueError):
        r.problems.append("No record of any watcher run yet")
        return
    age = time.time() - last.get("at", 0)
    if age > STALE_AFTER:
        r.problems.append(f"Watcher last ran {_ago(age)} (should be every 10 min)")
        if ON_SYSTEMD and not dry:
            code, out = _sh("sudo", "-n", "systemctl", "start", "--no-block", "erp-watcher.service")
            if code == 0:
                r.fixed.append("Started a watcher run now")
            else:
                log.warning("Could not start the watcher: %s", out)
    elif not last.get("ok"):
        r.problems.append(f"Last board check failed ({_ago(age)}): {last.get('error') or 'unknown error'}")
    else:
        r.ok.append(f"Board checked {_ago(age)}")


def check_timer(r: Report, dry: bool):
    if not ON_SYSTEMD:
        return
    code, _ = _sh("systemctl", "is-active", "--quiet", "erp-watcher.timer")
    if code == 0:
        return
    r.problems.append("The 30-minute schedule was off")
    if not dry:
        code, out = _sh("sudo", "-n", "systemctl", "enable", "--now", "erp-watcher.timer")
        if code == 0:
            r.fixed.append("Turned the schedule back on")
        else:
            log.warning("Could not enable the timer: %s", out)


def check_bridge(r: Report, dry: bool) -> dict:
    if config.WHATSAPP_PROVIDER != "bridge":
        return {}
    import whatsapp

    if whatsapp.wait_ready(60):
        r.ok.append("WhatsApp connected")
        return bridge_status()
    st = bridge_status()
    if st.get("hasQr") or "401" in str(st.get("error")):
        r.problems.append("WhatsApp unlinked this server - re-link it: ./deploy/relink-whatsapp.sh")
        return st
    r.problems.append("WhatsApp bridge was not connected")
    if ON_SYSTEMD and not dry:
        code, out = _sh("sudo", "-n", "systemctl", "restart", "erp-wa-bridge.service")
        if code == 0 and whatsapp.wait_ready(120):
            r.fixed.append("Restarted the WhatsApp bridge; it reconnected")
            return bridge_status()
        log.warning("Bridge restart did not help: %s", out)
    return st


def free_space() -> int:
    """Delete what is safe to delete: package caches, old system logs, oversized
    or rotated logs of our own, old debug dumps. Returns MB freed. Never touches
    the WhatsApp login (.baileys_auth), .env or the database."""
    before = shutil.disk_usage(config.STATE_DIR).free
    if ON_SYSTEMD:
        _sh("sudo", "-n", "apt-get", "clean")
        _sh("sudo", "-n", "journalctl", "--vacuum-size=50M")
        # old snap revisions are kept around by default and can be 100s of MB
        code, out = _sh("snap", "list", "--all")
        for line in out.splitlines():
            parts = line.split()
            if code == 0 and len(parts) >= 3 and "disabled" in line:
                _sh("sudo", "-n", "snap", "remove", parts[0], "--revision", parts[2])
    bridge_dir = config.BASE / "wa-bridge"
    for log_file in list(bridge_dir.glob("*.log")) + list((bridge_dir / "logs").glob("*.log")):
        if log_file.stat().st_size > 20 * 2**20:
            _sh("sudo", "-n", "truncate", "-s", "0", str(log_file)) if ON_SYSTEMD else log_file.write_text("")
    week_ago = time.time() - 7 * 86400
    for old in list(config.STATE_DIR.glob("*.log.[0-9]*")) + list(config.STATE_DIR.glob("last_*.html")):
        if old.stat().st_mtime < week_ago:
            old.unlink(missing_ok=True)
    return max(shutil.disk_usage(config.STATE_DIR).free - before, 0) // 2**20


def check_disk(r: Report, dry: bool):
    du = shutil.disk_usage(config.STATE_DIR)
    free = du.free / du.total
    if free < CLEAN_BELOW and not dry:
        freed = free_space()
        r.fixed.append(f"Cleaned up disk space, freed {freed} MB")
        du = shutil.disk_usage(config.STATE_DIR)
        free = du.free / du.total
    if free < DISK_MIN_FREE:
        # a full disk is what crashed the bridge before (ENOSPC)
        r.problems.append(f"Disk almost full even after cleanup: {free:.0%} free ({du.free // 2**20} MB)")
    else:
        r.ok.append(f"Disk {free:.0%} free")


def check_db(r: Report):
    import db

    if not db.enabled():
        return
    try:
        pending = db.wa_pending()
        if pending:
            r.problems.append(f"{pending} WhatsApp message(s) waiting in the retry queue")
        profiles = db.load_profiles()
    except Exception as exc:
        r.problems.append(f"Database unreachable: {str(exc)[:150]}")
        return
    statuses = [p.get("status") for p in profiles.values()]
    applied = statuses.count("applied")
    failed = [f"{p.get('company')} - {p.get('profile')}" for p in profiles.values()
              if p.get("status") == "failed"]
    if config.AUTO_APPLY:
        r.ok.append(f"Auto-apply on (CV{config.AUTO_APPLY_CV}), {applied} applied so far")
    if failed:
        r.problems.append("Auto-apply failed for: " + "; ".join(failed[:5])
                          + (f" (+{len(failed) - 5} more)" if len(failed) > 5 else ""))


def compose(r: Report) -> str:
    if r.problems:
        head = f"*ERP watcher: {len(r.problems)} problem(s)*"
    else:
        head = "*ERP watcher: all good*"
    lines = [head]
    lines += [f"- {p}" for p in r.problems]
    lines += [f"Fixed: {f}" for f in r.fixed]
    if r.ok:
        lines.append("")
        lines += [f"- {o}" for o in r.ok]
    lines.append(f"\n_Next health check in 6 hours. No message by then = the server is down._")
    return "\n".join(lines)


def deliver(text: str, problems: bool) -> str:
    import whatsapp

    targets = whatsapp.owner_targets()
    if targets and config.WHATSAPP_PROVIDER != "off":
        try:
            res = whatsapp.send(text, targets)
            if res["sent"]:
                return "whatsapp"
        except Exception as exc:
            log.warning("Health report over WhatsApp failed: %s", exc)
    if not problems:
        return "none"  # an "all good" is not worth breaking the no-email rule for
    import notifier

    subject = "[ERP watcher] health check: WhatsApp unreachable"
    notifier._send(subject, text, "<pre>" + text + "</pre>", to=[config.IMAP_USER], force=True)
    return "email"


def main() -> int:
    ap = argparse.ArgumentParser(description="Check the watcher's health, fix what it can, report.")
    ap.add_argument("--dry-run", action="store_true", help="check and print only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    r = Report()
    check_timer(r, args.dry_run)
    check_last_run(r, args.dry_run)
    st = check_bridge(r, args.dry_run)
    check_disk(r, args.dry_run)
    check_db(r)

    text = compose(r)
    print(text)
    if args.dry_run:
        return 0
    via = deliver(text, bool(r.problems))
    log.info("Health report: %d problem(s), %d fixed, sent via %s", len(r.problems), len(r.fixed), via)
    return 1 if r.problems and not r.fixed else 0


if __name__ == "__main__":
    sys.exit(main())
