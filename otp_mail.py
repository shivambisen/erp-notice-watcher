"""Reads the ERP login OTP out of your mailbox over IMAP."""
import email
import email.utils
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

import config

log = logging.getLogger(__name__)

# ERP mails a numeric code; require an OTP-ish keyword nearby so we never latch
# onto a random number in an unrelated mail.
# A digit run not glued to letters: "10001" inside "22XX10001" is a roll number,
# not an OTP, and \b would happily match it.
OTP_RE = re.compile(r"(?<![A-Za-z0-9])(\d{4,8})(?![A-Za-z0-9])")
KEYWORDS = ("otp", "one time password", "one-time password", "verification code")


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _body_text(msg: email.message.Message) -> str:
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True) or b""
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            chunks.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
        except Exception:
            pass
    text = "\n".join(chunks)
    return re.sub(r"<[^>]+>", " ", text)  # strip tags so HTML mails read cleanly


def _extract_otp(subject: str, body: str) -> str | None:
    blob = f"{subject}\n{body}"
    low = blob.lower()
    if not any(k in low for k in KEYWORDS):
        return None
    candidates = OTP_RE.findall(blob)
    if not candidates:
        return None

    # Prefer a number sitting right after an OTP keyword.
    near = re.search(
        r"(?:otp|one[\s-]?time password|verification code)\D{0,40}?"
        r"(?<![A-Za-z0-9])(\d{4,8})(?![A-Za-z0-9])",
        low,
    )
    if near:
        return near.group(1)

    # Otherwise prefer a 6-digit code, the usual shape, over other numbers.
    six = [c for c in candidates if len(c) == 6]
    chosen = six[0] if six else candidates[0]
    log.info(
        "No OTP keyword adjacent to a code; picked a %d-digit number from %d candidate(s)",
        len(chosen), len(candidates),
    )
    return chosen


class Mark:
    """Snapshot of the mailbox taken just before an OTP is requested."""

    def __init__(self, uidnext, cutoff):
        self.uidnext = uidnext
        self.cutoff = cutoff

    def __repr__(self):
        return f"Mark(uidnext={self.uidnext}, cutoff={self.cutoff:%H:%M:%S})"


def _connect():
    imap = imaplib.IMAP4_SSL(
        config.IMAP_HOST, config.IMAP_PORT, ssl_context=config.ssl_context()
    )
    imap.login(config.IMAP_USER, config.IMAP_PASSWORD)
    return imap


def mark() -> Mark:
    """Record where the mailbox ends, so only mail arriving after this counts.

    Freshness is decided by UIDNEXT -- the UID the next arriving message will be
    given -- rather than by comparing timestamps. Gmail omits INTERNALDATE when
    it is fetched alongside RFC822, which used to leave the timestamp check
    silently disabled and let a previous run's (already consumed) OTP through.
    The timestamp is kept only as a fallback.
    """
    uidnext = None
    try:
        imap = _connect()
        try:
            typ, data = imap.status(config.IMAP_FOLDER, "(UIDNEXT)")
            if typ == "OK" and data:
                m = re.search(rb"UIDNEXT\s+(\d+)", data[0] or b"")
                if m:
                    uidnext = int(m.group(1))
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except Exception as exc:
        log.warning("Could not read UIDNEXT (%s); will fall back to timestamps", exc)

    snapshot = Mark(uidnext, datetime.now(timezone.utc) - timedelta(seconds=30))
    log.debug("Mailbox %s", snapshot)
    return snapshot


def _received_at(imap, uid: bytes):
    """INTERNALDATE, fetched on its own so Gmail actually returns it."""
    try:
        typ, data = imap.uid("FETCH", uid, "(INTERNALDATE)")
        if typ != "OK" or not data or not data[0]:
            return None
        raw = data[0] if isinstance(data[0], bytes) else data[0][0]
        parsed = imaplib.Internaldate2tuple(raw)
        if parsed:
            return datetime.fromtimestamp(time.mktime(parsed), timezone.utc)
    except Exception as exc:
        log.debug("INTERNALDATE fetch failed for uid %s: %s", uid, exc)
    return None


def wait_for_otp(since: Mark, timeout: int | None = None) -> str:
    """Poll the mailbox until an OTP mail that arrived after `since` shows up."""
    if isinstance(since, datetime):  # tolerate the old signature
        since = Mark(None, since)
    timeout = timeout or config.OTP_WAIT_SECONDS
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        try:
            imap = _connect()
            try:
                imap.select(config.IMAP_FOLDER)

                if since.uidnext is not None:
                    typ, data = imap.uid("SEARCH", None, f"UID {since.uidnext}:*")
                else:
                    day = (since.cutoff - timedelta(days=1)).strftime("%d-%b-%Y")
                    typ, data = imap.uid("SEARCH", None, "SINCE", day)
                if typ != "OK":
                    raise RuntimeError(f"IMAP search failed: {typ}")

                uids = (data[0] or b"").split()
                for uid in reversed(uids[-40:]):  # newest first
                    # "uidnext:*" also returns the final message even when its
                    # UID is lower, so enforce the bound ourselves.
                    if since.uidnext is not None and int(uid) < since.uidnext:
                        continue

                    typ, raw = imap.uid("FETCH", uid, "(RFC822)")
                    if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                        continue
                    msg = email.message_from_bytes(raw[0][1])

                    sender = _decode(msg.get("From", "")).lower()
                    if config.OTP_SENDER_HINT and config.OTP_SENDER_HINT not in sender:
                        continue

                    if since.uidnext is None:
                        # No UID bound available, so fall back to time -- and if
                        # the time cannot be established either, refuse the mail
                        # rather than risk replaying a stale OTP.
                        received = _received_at(imap, uid)
                        if received is None:
                            hdr = msg.get("Date")
                            received = (
                                email.utils.parsedate_to_datetime(hdr) if hdr else None
                            )
                            if received and received.tzinfo is None:
                                received = received.replace(tzinfo=timezone.utc)
                        if received is None:
                            log.warning("Skipping mail uid %s: no usable date", uid)
                            continue
                        if received < since.cutoff:
                            continue

                    subject = _decode(msg.get("Subject", ""))
                    body = _body_text(msg)
                    otp = _extract_otp(subject, body)
                    if otp:
                        try:
                            dump = config.STATE_DIR / "last_otp_mail.txt"
                            dump.write_text(
                                f"uid: {uid.decode()}\nFrom: {sender}\n"
                                f"Subject: {subject}\n\n{body}"
                            )
                            dump.chmod(0o600)
                        except Exception:
                            pass
                        log.info(
                            "Got %d-digit OTP from %s (uid %s, subject: %s)",
                            len(otp), sender, uid.decode(), subject[:60],
                        )
                        return otp
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Mailbox check %d failed: %s", attempt, exc)

        log.debug("No new OTP mail yet (check %d); waiting", attempt)
        time.sleep(5)

    raise TimeoutError(
        f"No OTP mail from '{config.OTP_SENDER_HINT}' arrived in {config.IMAP_USER} "
        f"within {timeout}s. Check IMAP_USER / OTP_SENDER_HINT / IMAP_FOLDER."
    )
