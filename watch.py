#!/usr/bin/env python3
"""Watch the IITKGP ERP placement notice board and mail anything new.

Usage:
    python3 watch.py              one poll, then exit (what launchd runs)
    python3 watch.py --loop 600   poll forever, every 600s
    python3 watch.py --dump       fetch and print the board, mail nothing
    python3 watch.py --login      force a fresh OTP login and exit
    python3 watch.py --test-mail  send yourself a test alert
"""
import argparse
import logging
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler

import config
import notifier
import store
from erp_client import ErpClient, LoginFailed

log = logging.getLogger("watch")

# Don't mail the same failure over and over if the ERP is down for hours.
ALERT_COOLDOWN = 6 * 3600


def setup_logging(verbose: bool):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fh = RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def make_attach_fn(client: ErpClient):
    """Return a function that fetches a notice's file, or None if it has none.

    Downloads are cached per poll so the same PDF is not pulled twice when it
    goes out by both email and WhatsApp.
    """
    cache = {}

    def attach_for(cells):
        notice_id = store.cell(cells, 0)
        info = (client.last_attachments or {}).get(notice_id)
        if not info:
            return None
        if notice_id not in cache:
            cache[notice_id] = client.download_attachment(info["year"], info["id"])
        return cache[notice_id]

    return attach_for


def poll(client: ErpClient) -> int:
    """One check. Returns how many new notices were mailed."""
    notices = client.fetch_notices()
    log.info("Fetched %d rows (%d columns)", len(notices), len(notices[0]) if notices else 0)
    store.save_notices(notices)
    if not notices:
        # An empty board is a normal state for this grid, not just a blip. Seed
        # on it so the first notice that ever appears counts as new and gets
        # mailed, instead of being swallowed as part of the initial snapshot.
        state = store.load()
        if not state["seeded"]:
            state["seeded"] = True
            store.save(state)
            log.info("Board is empty; seeded - the next notice to appear will be mailed")
        else:
            log.info("Board is empty; nothing new")
        return 0

    use_db = False
    try:
        import db
        use_db = db.enabled()
    except Exception:
        use_db = False

    if use_db:
        try:
            return poll_with_db(notices, make_attach_fn(client))
        except Exception as exc:
            # Fall through to the file-based path rather than skipping the alert.
            log.warning("Database unavailable (%s); using local state", exc)

    state = store.load()
    seen = set(state["seen"])

    fresh = []
    for cells in notices:
        fp = store.fingerprint(cells)
        if fp not in seen:
            fresh.append((fp, cells))

    if not fresh:
        log.info("Nothing new")
        return 0

    if not state["seeded"]:
        state["seen"] = [fp for fp, _ in fresh]
        state["seeded"] = True
        store.save(state)
        if config.SEED_SILENTLY:
            log.info("First run: recorded %d existing notices without mailing", len(fresh))
            return 0
        log.info("First run: mailing all %d existing notices", len(fresh))
    else:
        log.info("%d new notice(s)", len(fresh))

    payload = [cells for _, cells in reversed(fresh)]
    payload, skipped = store.split_alertable(payload)
    if skipped:
        log.info("Skipping %d notice(s) of excluded type(s)", len(skipped))
    if not payload:
        state["seen"] = list(state["seen"]) + [fp for fp, _ in fresh]
        state["seeded"] = True
        store.save(state)
        return 0
    attach_fn = make_attach_fn(client)
    notifier.send_notices(payload, attach_fn)

    # WhatsApp is best-effort: a provider outage must not stop the mail going
    # out, and must not stop the notices being marked as seen.
    try:
        import whatsapp
        if whatsapp.enabled():
            result = whatsapp.send_notices(payload, attach_fn)
            log.info("WhatsApp: %d sent, %d failed",
                     len(result["sent"]), len(result["failed"]))
    except Exception as exc:
        log.warning("WhatsApp delivery skipped: %s", exc)

    state["seen"] = list(state["seen"]) + [fp for fp, _ in fresh]
    state["seeded"] = True
    store.save(state)
    return len(fresh)


def poll_with_db(notices: list, attach_fn=None) -> int:
    """Same contract as poll(), backed by Postgres."""
    import db

    db.init()
    fresh = db.sync_notices(notices, store.fingerprint, store.title_of, store.time_of)
    seeded = bool(db.get_meta("seeded", False))

    if not fresh:
        if not seeded:
            db.set_meta("seeded", True)
        log.info("Nothing new (%d stored)", db.counts()["total"])
        return 0

    if not seeded:
        db.mark_alerted(fresh, store.fingerprint)
        db.set_meta("seeded", True)
        if config.SEED_SILENTLY:
            log.info("First run: recorded %d existing notices without mailing", len(fresh))
            return 0

    # Excluded types are stored and shown in the UI but never alerted. Mark them
    # alerted straight away so they cannot queue up and fire later.
    alertable, skipped = store.split_alertable(fresh)
    if skipped:
        log.info("Skipping %d notice(s) of excluded type(s) %s",
                 len(skipped), ", ".join(config.NOTICE_EXCLUDE_TYPES) or "-")
        db.mark_alerted(skipped, store.fingerprint)
    if not alertable:
        return 0

    fresh = alertable
    fresh.sort(key=store.sort_key, reverse=True)
    log.info("%d new notice(s) to alert", len(fresh))
    notifier.send_notices(fresh, attach_fn)

    deliver_whatsapp(fresh, attach_fn)

    db.mark_alerted(fresh, store.fingerprint)
    return len(fresh)


def deliver_whatsapp(notices: list, attach_fn=None):
    """Send each notice to WhatsApp; anything that cannot be delivered is queued and
    retried on later polls rather than lost.

    Each notice is delivered on its own, so one failure cannot abandon the rest of
    the batch. Never raises: WhatsApp trouble must not affect the email path or the
    marking of notices as handled.
    """
    try:
        import whatsapp
        if not whatsapp.enabled():
            return
        import db

        def park(item, kind, why):
            try:
                if db.enabled():
                    db.wa_enqueue(kind, item)
                    log.warning("WhatsApp not delivered (%s); queued for retry", why)
                else:
                    log.warning("WhatsApp not delivered (%s); no database to queue in", why)
            except Exception as qexc:
                log.error("WhatsApp not delivered (%s) AND could not be queued: %s", why, qexc)

        if len(notices) > config.ALERT_MAX_INDIVIDUAL:
            # a backlog goes out as one digest message
            text = whatsapp.summarise(notices)
            try:
                if not whatsapp.wait_ready(60):
                    raise RuntimeError("bridge not ready")
                res = whatsapp.send(text)
                if not res["sent"]:
                    raise RuntimeError(f"all targets failed: {res['failed']}")
                log.info("WhatsApp: digest of %d notices sent", len(notices))
            except Exception as exc:
                park({"text": text}, "text", f"digest: {exc}")
            return

        ready = whatsapp.wait_ready(60)
        sent = queued = 0
        for i, cells in enumerate(sorted(notices, key=store.sort_key)):
            if i:
                time.sleep(config.ALERT_SEND_GAP)
            try:
                if not ready:
                    raise RuntimeError("bridge not ready after 60s")
                res = whatsapp.send_notices([cells], attach_fn)
                if not res["sent"]:
                    raise RuntimeError(f"all targets failed: {res['failed']}")
                sent += 1
            except Exception as exc:
                park({"cells": cells}, "notice", f"{store.title_of(cells)}: {exc}")
                queued += 1
        log.info("WhatsApp: %d sent, %d queued for retry", sent, queued)
    except Exception as exc:
        log.warning("WhatsApp delivery skipped: %s", exc)


def retry_whatsapp(client: ErpClient):
    """Deliver WhatsApp messages parked by earlier failures. Contained."""
    try:
        import db
        import whatsapp
        if not (whatsapp.enabled() and db.enabled()):
            return
        db.init()
        if not db.wa_pending():
            return
        if not whatsapp.wait_ready(10):
            log.info("WhatsApp bridge not ready; %d queued message(s) wait for the next check",
                     db.wa_pending())
            return
        attach_fn = make_attach_fn(client)
        done = failed = 0
        for row in db.wa_claim(10):
            try:
                if row["kind"] == "notice":
                    res = whatsapp.send_notices([row["payload"]["cells"]], attach_fn)
                else:
                    res = whatsapp.send(row["payload"]["text"], row["payload"].get("to"))
                if not res["sent"]:
                    raise RuntimeError(f"all targets failed: {res['failed']}")
                db.wa_done(row["id"])
                done += 1
                time.sleep(config.ALERT_SEND_GAP)
            except Exception as exc:
                db.wa_release(row["id"], f"{type(exc).__name__}: {exc}")
                failed += 1
                log.warning("Retry of queued WhatsApp message %s failed (attempt %d): %s",
                            row["id"], row["attempts"], exc)
        log.info("WhatsApp retry: %d delivered, %d still queued", done, db.wa_pending())
    except Exception as exc:
        log.warning("WhatsApp retry skipped: %s", exc)


def check_companies(client: ErpClient):
    """Look for new company profiles you are eligible for. Contained: a problem
    here is logged (and mailed at most once every 6h) but never stops notices."""
    try:
        import companies
        res = companies.run(client)
        if res:
            log.info("Companies: %d listed, %d new, %d alerted, %d applied, %d failed",
                     res["listed"], res["new"], res["alerted"],
                     res.get("applied", 0), res.get("apply_failed", 0))
    except Exception as exc:
        log.warning("Company check failed: %s: %s", type(exc).__name__, exc)
        try:
            import db
            if db.enabled():
                last = float(db.get_meta("companies_fail_alert_at", 0) or 0)
                if time.time() - last > ALERT_COOLDOWN:
                    notifier.send_private(
                        "[ERP watcher] company check is failing",
                        f"The new-company check hit an error:\n\n{type(exc).__name__}: {exc}\n\n"
                        "Notice alerts are unaffected. You will not be told about new company "
                        "profiles until this clears -- check the ERP company list by hand meanwhile.")
                    db.set_meta("companies_fail_alert_at", time.time())
        except Exception as mail_exc:
            log.warning("Could not send the company-check failure mail: %s", mail_exc)


def report_failure(exc: Exception):
    state = store.load()
    last = state.get("last_alert_at", 0)
    now = time.time()
    detail = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
    log.error("Poll failed: %s", detail)

    if now - last < ALERT_COOLDOWN:
        log.info("Failure alert suppressed (cooldown)")
        return
    try:
        notifier.send_alert(
            "could not check the notice board",
            "The ERP notice watcher hit an error and could not check the board.\n\n"
            f"{detail}\n"
            "If this is a login problem, your ERP password or security answer may have\n"
            "changed, or the OTP mailbox settings need updating.\n"
            f"Log: {config.LOG_FILE}",
        )
        state["last_alert_at"] = now
        store.save(state)
    except Exception as mail_exc:
        log.error("Could not even send the failure alert: %s", mail_exc)


def mask(value: str) -> str:
    """Show enough to recognise a value, never enough to reuse it."""
    if not value:
        return "(empty)"
    return f"{value[0]}{'*' * max(len(value) - 2, 1)}{value[-1]} ({len(value)} chars)"


def check_config() -> int:
    print(f"Config from {config.BASE / '.env'}\n")
    print(f"  ERP_USER_ID        {config.ERP_USER_ID}")
    print(f"  ERP_PASSWORD       {mask(config.ERP_PASSWORD)}")
    print(f"  default answer     {mask(config.ERP_SECURITY_ANSWER_DEFAULT)}")

    answers = config.ERP_SECURITY_ANSWERS
    if answers:
        print(f"  security answers   {len(answers)} question rule(s):")
        for question, answer in answers.items():
            print(f"                       match {question!r} -> {mask(answer)}")
    else:
        print("  security answers   none - every login uses the default answer")
    if config.ERP_SECURITY_ANSWERS_PROBLEM:
        print(f"  !! ERP_SECURITY_ANSWERS: {config.ERP_SECURITY_ANSWERS_PROBLEM}")

    print(f"\n  IMAP               {config.IMAP_USER} @ {config.IMAP_HOST}:{config.IMAP_PORT}"
          f" [{config.IMAP_FOLDER}]")
    print(f"  IMAP password      {mask(config.IMAP_PASSWORD)}")
    print(f"  OTP sender hint    {config.OTP_SENDER_HINT!r}")
    print(f"  SMTP               {config.SMTP_USER} @ {config.SMTP_HOST}:{config.SMTP_PORT}")
    print(f"  alerts to          {', '.join(config.MAIL_TO)}")
    print(f"\n  notice rows        {config.NOTICE_ROWS} (query id {config.JQ_QUERY_ID})")
    print(f"  column labels      {', '.join(config.NOTICE_COLUMNS) or '(unset)'}")
    print(f"  silent first run   {config.SEED_SILENTLY}")
    print(f"  never alert types  {', '.join(config.NOTICE_EXCLUDE_TYPES) or '(none)'}")
    if config.NOTICE_EXCLUDE_KEYWORDS:
        print(f"  never alert words  {', '.join(config.NOTICE_EXCLUDE_KEYWORDS)}")
    print(f"  one alert per      notice (digest above {config.ALERT_MAX_INDIVIDUAL})")

    state = store.load()
    print(f"\n  seeded             {state.get('seeded')} "
          f"({len(state.get('seen', []))} notices remembered)")
    print(f"  cached session     {'yes' if config.COOKIE_FILE.exists() else 'no'}")

    if config.ERP_SECURITY_ANSWERS_PROBLEM:
        print("\nOne warning above. It is not fatal: logins will use the default "
              "answer.\nSet ERP_SECURITY_ANSWERS={} in .env to silence it.")
        return 1
    print("\nConfig parsed cleanly.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Watch the IITKGP ERP placement notice board.")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever at this interval")
    ap.add_argument("--dump", action="store_true", help="print the board and exit")
    ap.add_argument("--login", action="store_true", help="force a fresh OTP login and exit")
    ap.add_argument("--test-mail", action="store_true", help="send a test alert and exit")
    ap.add_argument("--check-config", action="store_true", help="show parsed settings and exit")
    ap.add_argument("--companies", action="store_true",
                    help="read-only: show each listed company profile, the portal's eligibility "
                         "verdict, and what the watcher would do (sends and records nothing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.check_config:
        return check_config()

    setup_logging(args.verbose)

    if args.test_mail:
        notifier.send_alert("test message", "If you are reading this, mail delivery works.")
        print(f"Sent a test mail to {', '.join(config.MAIL_TO)}")
        return 0

    client = ErpClient()

    if args.login:
        client.login()
        print("Login OK - session cached at", config.COOKIE_FILE)
        return 0

    if args.companies:
        import companies
        rows = companies.scan(client)
        print(f"{len(rows)} profile(s) listed\n")
        for r in rows:
            print(f"  {r['company'][:30]:30} {r['profile'][:28]:28} {r['form'] or '-':10} "
                  f"{r['verdict']:10} recorded={r['recorded'] or '-':10} "
                  f"window->{r['window_end']}" + (f"   ({r['reason']})" if r['reason'] else ""))
        return 0

    if args.dump:
        rows = client.fetch_notices()
        print(f"{len(rows)} rows, {len(rows[0]) if rows else 0} columns\n")
        for i, cells in enumerate(rows, 1):
            print(f"--- row {i} ---")
            for j, cell in enumerate(cells, 1):
                print(f"  [{j}] {cell}")
        return 0

    if args.loop:
        log.info("Polling every %ds; Ctrl-C to stop", args.loop)
        while True:
            try:
                poll(client)
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                report_failure(exc)
            retry_whatsapp(client)
            check_companies(client)
            time.sleep(args.loop)

    code, error = 0, ""
    try:
        poll(client)
    except LoginFailed as exc:
        report_failure(exc)
        record_run(False, f"login failed: {exc}")
        return 2
    except Exception as exc:
        report_failure(exc)
        code, error = 1, f"{type(exc).__name__}: {exc}"
    retry_whatsapp(client)
    check_companies(client)
    record_run(code == 0, error)
    return code


def record_run(ok: bool, error: str = ""):
    """Leave a note of this run for healthcheck.py: when it ran and whether the
    board check worked. Never fails the run itself."""
    import json

    try:
        (config.STATE_DIR / "last_run.json").write_text(json.dumps(
            {"at": time.time(), "ok": ok, "error": error[:300]}))
    except OSError as exc:
        log.warning("Could not record the run: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
