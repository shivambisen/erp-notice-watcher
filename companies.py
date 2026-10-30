"""Watch the company list: alert on new profiles, and (AUTO_APPLY=on) apply.

For every listed job profile this reads the profile's own page, where the portal
states whether *you* are eligible. It acts only on that verdict:

  * the portal shows the Apply-with-CV button  -> eligible, open, not yet applied
  * "Cancel apply" is shown                    -> already applied
  * a reason is shown instead                  -> not eligible

With AUTO_APPLY=on, an eligible open profile is applied to with AUTO_APPLY_CV,
then the page is re-read to confirm the portal now shows it as applied, and the
result is posted to email and the WhatsApp group. Nothing is applied unless the
portal itself is offering the button at that moment.

State lives in the `company_profiles` table. Every action is claimed atomically
first, so a profile is announced -- and applied to -- at most once per attempt
even if two machines poll together.
"""
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import notifier

log = logging.getLogger("companies")
IST = ZoneInfo("Asia/Kolkata")


def _key(c: dict) -> tuple:
    return (c["year"], c["com_id"], c["jnf_id"])


def _dt(raw: str):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    except ValueError:
        return None


def window_state(c: dict, now=None) -> str:
    """open | not_started | closed | unknown, from the CV-upload window."""
    now = now or datetime.now(IST)
    start, end = _dt(c.get("window_start")), _dt(c.get("window_end"))
    if end and now > end:
        return "closed"
    if start and now < start:
        return "not_started"
    return "open" if (start or end) else "unknown"


def closes_in(c: dict, now=None) -> str:
    end = _dt(c.get("window_end"))
    if not end:
        return ""
    now = now or datetime.now(IST)
    delta = end - now
    if delta <= timedelta(0):
        return "closed"
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    mins = rem // 60
    return f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h {mins}m"


def reminder_level(c: dict, now=None) -> int:
    """0 = no reminder due, 1 = within COMPANY_REMINDER_HOURS, 2 = within the last hour."""
    if config.COMPANY_REMINDER_HOURS <= 0:
        return 0
    end = _dt(c.get("window_end"))
    if not end:
        return 0
    left = end - (now or datetime.now(IST))
    if left <= timedelta(0):
        return 0
    if left <= timedelta(hours=1):
        return 2
    return 1 if left <= timedelta(hours=config.COMPANY_REMINDER_HOURS) else 0


def _ctc_number(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else 0


def _passes_filters(c: dict, st: dict) -> tuple:
    """(ok, reason). Applied only to profiles you are eligible for."""
    form = (st.get("form_type") or "").upper()
    if config.COMPANY_ALERT_FORM_TYPES and form and form not in config.COMPANY_ALERT_FORM_TYPES:
        return False, f"form type {form}"
    name = c["company"].lower()
    for word in config.COMPANY_ALERT_SKIP:
        if word in name:
            return False, f"matches skip rule '{word}'"
    if config.COMPANY_ALERT_MIN_CTC and 0 < _ctc_number(c.get("ctc")) < config.COMPANY_ALERT_MIN_CTC:
        return False, f"CTC below {config.COMPANY_ALERT_MIN_CTC}"
    return True, ""


def _details(c: dict, st: dict) -> str:
    rows = [
        ("Company", c["company"]),
        ("Profile", c["profile"]),
        ("Form", st.get("form_type") or "-"),
        ("CTC", f"{c.get('ctc') or '-'} {c.get('currency') or ''}".strip()),
        ("CGPA cut-off", st.get("cgpa_cutoff") or "-"),
        ("Bond", st.get("bond") or "-"),
        ("CV window", f"{c.get('window_start') or '?'} -> {c.get('window_end') or '?'} IST"
                      + (f"  (closes in {closes_in(c)})" if closes_in(c) not in ("", "closed") else "")),
    ]
    return "\n".join(f"{k:<13}: {v}" for k, v in rows)


def _wa(text: str):
    """Post to the WhatsApp targets. Best effort: never raises."""
    if not config.COMPANY_ALERT_WHATSAPP:
        return
    try:
        import whatsapp
        if not whatsapp.enabled():
            return
        try:
            if not whatsapp.wait_ready(30):
                raise RuntimeError("bridge not ready")
            res = whatsapp.send(text)
            if not res["sent"]:
                raise RuntimeError(f"all targets failed: {res['failed']}")
            log.info("WhatsApp: %d sent, %d failed", len(res["sent"]), len(res["failed"]))
        except Exception as exc:
            # park it: an "Applied with CV2" confirmation is worth delivering late
            if db.enabled():
                db.wa_enqueue("text", {"text": text})
                log.warning("WhatsApp post not delivered (%s); queued for retry", exc)
            else:
                log.warning("WhatsApp post not delivered (%s)", exc)
    except Exception as exc:
        log.warning("WhatsApp post skipped: %s", exc)


def _short(c: dict, st: dict) -> str:
    left = closes_in(c)
    bits = [f"CTC {c.get('ctc') or '-'} {c.get('currency') or ''}".strip()]
    if st.get("cgpa_cutoff"):
        bits.append(f"CGPA cut-off {st['cgpa_cutoff']}")
    if st.get("form_type"):
        bits.append(st["form_type"])
    bits.append(f"closes {c.get('window_end')} IST" + (f" ({left} left)" if left not in ("", "closed") else ""))
    return " | ".join(bits)


def _notify_eligible(c: dict, st: dict):
    body = (
        "The portal shows you as eligible for this profile, and you have not applied yet.\n\n"
        + _details(c, st)
        + "\n\nTo apply: ERP -> Training & Placement -> company list -> this profile ->"
          f" \"Apply with CV{config.AUTO_APPLY_CV}\".\nNothing was submitted for you."
    )
    notifier.send_private(
        f"[ERP] Open for you: {c['company']} - {c['profile']}", body)
    _wa(f"*Open for you: {c['company']} - {c['profile']}*\n{_short(c, st)}\n"
        f"Not applied yet - apply on the ERP with CV{config.AUTO_APPLY_CV}.")


def _notify_reminder(c: dict, st: dict, level: int):
    left = closes_in(c)
    tag = "LAST HOUR" if level == 2 else "closing soon"
    notifier.send_private(
        f"[ERP] {tag}, not applied: {c['company']} - {c['profile']} ({left} left)",
        "You are eligible for this profile and have not applied. The CV window closes in "
        f"{left}.\n\n" + _details(c, st)
        + "\n\nTo apply: ERP -> Training & Placement -> company list -> this profile ->"
          f" \"Apply with CV{config.AUTO_APPLY_CV}\".\nNothing was submitted for you.")
    _wa(f"*{tag.upper()} - not applied: {c['company']} - {c['profile']}*\n{_short(c, st)}")


def _notify_applied(c: dict, st: dict, cv: int, note: str = ""):
    """Report a confirmed application. Never raises: the application already
    happened, so a mail problem must not undo or repeat anything."""
    body = (f"Applied with CV{cv}, and the portal now shows this profile as applied.\n\n"
            + _details(c, st)
            + (f"\n\n{note}" if note else "")
            + "\n\nTo undo it, use \"Cancel apply\" on the profile's page on the ERP before"
              " the window closes.")
    try:
        notifier.send_private(f"[ERP] Applied with CV{cv}: {c['company']} - {c['profile']}", body)
    except Exception as exc:
        log.warning("Applied-mail failed (the application itself went through): %s", exc)
    _wa(f"*Applied with CV{cv}: {c['company']} - {c['profile']}*\n{_short(c, st)}\n"
        "Confirmed on the portal.")


def _notify_apply_failed(c: dict, st: dict, cv: int, reason: str, attempt: int):
    left = max(config.AUTO_APPLY_MAX_ATTEMPTS - attempt, 0)
    again = (f"It will retry on the next check ({left} more attempt{'s' if left != 1 else ''})."
             if left else "No more automatic attempts - please apply on the ERP yourself.")
    try:
        notifier.send_private(
            f"[ERP] AUTO-APPLY FAILED ({attempt}/{config.AUTO_APPLY_MAX_ATTEMPTS}): "
            f"{c['company']} - {c['profile']}",
            f"I could not confirm an application with CV{cv}.\n\nReason: {reason}\n\n"
            + _details(c, st) + f"\n\n{again}")
    except Exception as exc:
        log.warning("Failure-mail failed: %s", exc)
    _wa(f"*AUTO-APPLY FAILED ({attempt}/{config.AUTO_APPLY_MAX_ATTEMPTS}): "
        f"{c['company']} - {c['profile']}*\n{_short(c, st)}\n{again}")


def _apply_one(client, c: dict, st: dict) -> str:
    """Apply to one profile the portal is currently offering. Returns
    'applied', 'failed' or 'busy' (someone else holds the claim / no attempts left).

    Order matters: the attempt is claimed in the database FIRST, the application is
    submitted, then the profile's page is re-read -- success means the portal itself
    now says "applied", not that the request returned 200.
    """
    key = _key(c)
    cv = config.AUTO_APPLY_CV
    attempt = db.claim_apply(key, config.AUTO_APPLY_MAX_ATTEMPTS)
    if not attempt:
        return "busy"

    http, said, after = None, "", {}
    sent = True
    try:
        http, said = client.apply_with_cv(st, cv)
    except ValueError as exc:
        sent = False  # the guard refused before anything was sent
        said = f"ValueError: {exc}"
    except Exception as exc:
        # The request may well have reached the portal before this error, so it is
        # NOT proof of failure -- the page below is the judge.
        said = f"{type(exc).__name__}: {exc}"

    if sent:
        try:
            time.sleep(2)
            after = client.jnf_state(c["year"], c["com_id"], c["jnf_id"])
        except Exception as exc:
            said += f" (and could not re-read the page: {type(exc).__name__}: {exc})"
    confirmed = sent and after.get("state") == "applied"

    if confirmed:
        db.set_profile_status(key, "applied_auto", f"CV{cv}")
        log.info("APPLIED with CV%d: %s / %s", cv, c["company"], c["profile"])
        _notify_applied(c, st, cv)
        return "applied"

    reason = (f"HTTP {http}; portal said: {said or '(nothing)'}; "
              f"page afterwards: {after.get('state', 'not re-read')} {after.get('reason', '')}").strip()
    db.set_profile_status(key, "apply_failed", reason[:300])
    log.warning("Apply FAILED (attempt %d) for %s / %s: %s", attempt, c["company"], c["profile"], reason)
    _notify_apply_failed(c, st, cv, reason, attempt)
    return "failed"


def _notify_other(c: dict, st: dict, why: str):
    notifier.send_private(
        f"[ERP] New company: {c['company']} - {c['profile']} ({why})",
        _details(c, st) + f"\n\nPortal says: {why}\nNothing was submitted for you.")


def _classify(client, c: dict) -> tuple:
    """(status, reason, verdict) for one profile, by reading the portal."""
    if c.get("applied"):
        return "applied", "", {}
    ws = window_state(c)
    if ws == "closed":
        return "closed", "CV window ended", {}
    if ws == "not_started":
        return "wait", "window has not opened", {}
    st = client.jnf_state(c["year"], c["com_id"], c["jnf_id"])
    if st["state"] == "applied":
        return "applied", "", st
    if st["state"] == "ineligible":
        return "ineligible", st["reason"], st
    if st["state"] == "eligible":
        ok, why = _passes_filters(c, st)
        return ("eligible" if ok else "skipped"), why, st
    return "unknown", "could not read the portal's verdict", st


def scan(client) -> list:
    """Read-only dry run: every listed profile with the portal's verdict and what
    this watcher would do. Records and sends nothing."""
    known = {}
    if db.enabled():
        db.init()  # idempotent schema creation; no data is written
        known = db.load_profiles()
    out = []
    for c in client.fetch_companies():
        status, reason, st = _classify(client, c)
        seen = known.get(_key(c))
        out.append({**c, "verdict": status, "reason": reason, "form": st.get("form_type", ""),
                    "recorded": seen["status"] if seen else None})
    return out


def run(client) -> dict:
    if not config.COMPANY_ALERTS:
        return {}
    if not db.enabled():
        log.info("Company alerts need the database to remember what was announced; skipping")
        return {}

    db.init()
    companies = client.fetch_companies()
    known = db.load_profiles()
    seeded = bool(db.get_meta("companies_seeded", False))
    fresh = [c for c in companies if _key(c) not in known]
    summary = {"listed": len(companies), "new": 0, "alerted": 0, "applied": 0, "apply_failed": 0}
    acted = set()  # profiles applied to (or tried) this run: no reminders for them

    if not seeded:
        # Everything already listed predates the watcher. Record it, and send ONE
        # summary so you can see what is open right now -- no per-profile spam.
        lines = []
        for c in fresh:
            status, reason, st = _classify(client, c)
            db.claim_profile(_key(c), c["company"], c["profile"], c.get("ctc", ""),
                             st.get("form_type", ""), "applied" if status == "applied"
                             else "seeded", reason)
            tag = {"eligible": "ELIGIBLE, not applied", "applied": "you have applied",
                   "ineligible": f"not eligible - {reason}", "closed": "window closed",
                   "skipped": f"eligible but skipped ({reason})",
                   "wait": "window not open yet"}.get(status, status)
            lines.append(f"  - {c['company']} / {c['profile']}: {tag}"
                         + (f"  [closes in {closes_in(c)}]" if status == "eligible" else ""))
        db.set_meta("companies_seeded", True)
        if lines:
            notifier.send_private(
                "[ERP] Company alerts are on - what is listed right now",
                "From now on you get a private email when a new profile you are eligible "
                "for appears.\nThese were already listed, so no separate alert was sent:\n\n"
                + "\n".join(lines) + "\n\nNothing was submitted for you.")
        log.info("Company list seeded with %d profile(s)", len(fresh))
        return summary

    for c in fresh:
        status, reason, st = _classify(client, c)
        if status == "wait":
            continue  # not claimed, so it is looked at again next poll
        summary["new"] += 1
        key = _key(c)
        stored = {"eligible": "eligible", "skipped": "skipped", "ineligible": "ineligible",
                  "applied": "applied", "closed": "closed"}.get(status, "unknown")
        if not db.claim_profile(key, c["company"], c["profile"], c.get("ctc", ""),
                                st.get("form_type", ""), stored, reason):
            continue  # another machine announced it first

        if status == "eligible" and config.AUTO_APPLY:
            # The apply path keeps its own state (applying / applied_auto / apply_failed)
            # in the database, so it must NOT be un-claimed on an error the way a plain
            # alert is: that could hide an application that did go through.
            acted.add(key)
            outcome = _apply_one(client, c, st)
            if outcome in ("applied", "failed"):
                summary["applied" if outcome == "applied" else "apply_failed"] += 1
            continue

        try:
            if status == "eligible":
                _notify_eligible(c, st)
                summary["alerted"] += 1
                lvl = reminder_level(c)
                if lvl:
                    db.claim_reminder(key, lvl)  # the alert itself already says it is closing soon
            elif status == "unknown":
                _notify_other(c, st, "could not read eligibility - check the portal")
            elif status == "ineligible" and config.COMPANY_ALERT_INELIGIBLE:
                _notify_other(c, st, f"not eligible: {reason}")
            log.info("Profile %s/%s %s: %s -> %s", c["com_id"], c["jnf_id"], c["company"],
                     c["profile"], status + (f" ({reason})" if reason else ""))
        except Exception:
            # Un-claim so the next poll tries again; a lost alert is worse than a repeat.
            db.release_profile(key)
            raise

    # ---- an attempt that looked unconfirmed, but the portal now shows applied ----
    # Corrects a wrong "failed" / left-over "applying" record, and says so once.
    for c in companies:
        key = _key(c)
        row = known.get(key)
        if row and key not in acted and c.get("applied") and row["status"] in ("applying", "apply_failed"):
            db.set_profile_status(key, "applied_auto", f"CV{config.AUTO_APPLY_CV}")
            _notify_applied(
                c, {}, config.AUTO_APPLY_CV,
                "An earlier attempt had looked unconfirmed (a 'failed' alert may have gone out),"
                " but the portal now shows this profile as applied - please disregard that alert.")
            summary["applied"] += 1
            acted.add(key)

    # ---- profiles already on file that are still waiting to be applied to ----
    # (e.g. ones recorded when the watcher was first set up, or an earlier failed try)
    if config.AUTO_APPLY:
        for c in companies:
            key = _key(c)
            row = known.get(key)
            if not row or key in acted or c.get("applied"):
                continue
            if row["status"] not in ("eligible", "seeded", "apply_failed", "applying"):
                continue
            if row["status"] == "apply_failed" and (row.get("attempts") or 0) >= config.AUTO_APPLY_MAX_ATTEMPTS:
                continue  # out of automatic attempts; the failure alert already said so
            if window_state(c) != "open":
                continue

            st = client.jnf_state(c["year"], c["com_id"], c["jnf_id"])
            if st["state"] == "applied":
                if row["status"] in ("applying", "apply_failed"):
                    # an earlier attempt went through but was never confirmed
                    db.set_profile_status(key, "applied_auto", f"CV{config.AUTO_APPLY_CV}")
                    _notify_applied(c, st, config.AUTO_APPLY_CV, "(confirmed on a later check)")
                    summary["applied"] += 1
                else:
                    db.set_profile_status(key, "applied")
                acted.add(key)
                continue
            if st["state"] != "eligible":
                db.set_profile_status(key, "ineligible" if st["state"] == "ineligible" else row["status"],
                                      st.get("reason", ""))
                acted.add(key)
                continue
            ok, why = _passes_filters(c, st)
            if not ok:
                db.set_profile_status(key, "skipped", why)
                acted.add(key)
                continue

            acted.add(key)
            outcome = _apply_one(client, c, st)
            if outcome in ("applied", "failed"):
                summary["applied" if outcome == "applied" else "apply_failed"] += 1

    # ---- closing-soon reminders for profiles already known and still unapplied ----
    summary.setdefault("reminded", 0)
    if config.COMPANY_REMINDER_HOURS > 0:
        for c in companies:
            key = _key(c)
            row = known.get(key)
            if (not row or key in acted or c.get("applied")
                    or row["status"] not in ("eligible", "seeded", "apply_failed")):
                continue
            level = reminder_level(c)
            if level <= (row.get("reminders") or 0):
                continue
            st = client.jnf_state(c["year"], c["com_id"], c["jnf_id"])
            if st["state"] != "eligible":
                # applied since, or never eligible: stop checking it every poll
                db.set_profile_status(key, "applied" if st["state"] == "applied"
                                      else "ineligible" if st["state"] == "ineligible"
                                      else row["status"], st.get("reason", ""))
                continue
            if not db.claim_reminder(key, level):
                continue
            try:
                _notify_reminder(c, st, level)
                summary["reminded"] += 1
                log.info("Reminder %d sent for %s / %s", level, c["company"], c["profile"])
            except Exception:
                db.release_reminder(key, level)
                raise
    return summary
