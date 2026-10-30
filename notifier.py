"""Sends the 'new notice' alert mail."""
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage

import config

log = logging.getLogger(__name__)


def _labels(width: int) -> list[str]:
    cols = list(config.NOTICE_COLUMNS)
    while len(cols) < width:
        cols.append(f"Field {len(cols) + 1}")
    return cols[:width]


def _render(notices: list[list[str]]) -> tuple[str, str]:
    width = max((len(n) for n in notices), default=0)
    cols = _labels(width)

    text_parts, html_parts = [], []
    for i, cells in enumerate(notices, 1):
        import store
        text_parts.append(f"--- {store.title_of(cells)} ---")
        rows = []
        for idx, (label, value) in enumerate(
            zip(cols, cells + [""] * (width - len(cells)))
        ):
            if not value or idx in config.NOTICE_HIDE_COLS:
                continue
            text_parts.append(f"{label}: {value}")
            rows.append(
                f"<tr><th align='left' style='padding:4px 12px 4px 0;color:#555;"
                f"white-space:nowrap;vertical-align:top'>{html.escape(label)}</th>"
                f"<td style='padding:4px 0'>{html.escape(value)}</td></tr>"
            )
        text_parts.append("")
        html_parts.append(
            "<div style='margin:0 0 20px;padding:12px 16px;border:1px solid #e3e3e3;"
            "border-left:4px solid #0b6bcb;border-radius:6px'>"
            f"<div style='font-weight:600;font-size:15px;margin-bottom:8px'>"
            f"{html.escape(__import__('store').title_of(cells))}</div>"
            f"<table style='border-collapse:collapse;font-size:14px'>{''.join(rows)}</table>"
            "</div>"
        )

    body_text = "\n".join(text_parts)
    body_html = (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:680px;color:#222\">"
        f"<h2 style='font-size:17px;margin:0 0 16px'>{len(notices)} new "
        f"notice{'s' if len(notices) != 1 else ''} on the ERP placement board</h2>"
        f"{''.join(html_parts)}"
        "<p style='font-size:12px;color:#999;border-top:1px solid #eee;padding-top:10px'>"
        "Sent by erp-notice-watcher &middot; "
        "<a href='https://erp.iitkgp.ac.in/TrainingPlacementSSO/TPStudent.jsp'>Open the ERP</a>"
        "</p></div>"
    )
    return body_text, body_html


def send_notices(notices: list, attach_fn=None):
    """One email per notice, oldest first so the newest lands on top of the inbox.

    A large backlog is digested into a single mail instead, so an outage cannot
    turn into fifty separate emails.
    """
    import store

    if not notices:
        return
    if len(notices) > config.ALERT_MAX_INDIVIDUAL:
        body_text, body_html = _render(notices)
        _send(f"[ERP] {len(notices)} new CDC notices", body_text, body_html)
        return

    for cells in sorted(notices, key=store.sort_key):
        head = store.title_of(cells)
        kind = store.cell(cells, 2)
        subject = f"[ERP] {kind + ': ' if kind else ''}{head}"[:120]
        body_text, body_html = _render([cells])

        attachment = None
        if attach_fn is not None:
            try:
                attachment = attach_fn(cells)
            except Exception as exc:
                # A missing PDF must never stop the notice itself going out.
                log.warning("Attachment for %s unavailable: %s", head, exc)
        _send(subject, body_text, body_html, attachment)


def send_alert(subject: str, body: str):
    """Operational warning (e.g. login broke) so failures aren't silent."""
    if not config.MAIL_ENABLED:
        _whatsapp_fallback(f"[ERP watcher] {subject}", body)
        return
    _send(
        f"[ERP watcher] {subject}",
        body,
        f"<pre style='font-family:ui-monospace,monospace;font-size:13px;"
        f"white-space:pre-wrap'>{html.escape(body)}</pre>",
    )


def _send(subject: str, body_text: str, body_html: str, attachment=None, to=None):
    if not config.MAIL_ENABLED:
        log.debug("Email off; not sending %r", subject)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.MAIL_FROM
    recipients = list(to) if to else list(config.MAIL_TO)
    msg["To"] = ", ".join(recipients)
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    if attachment:
        data, filename, ctype = attachment
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=filename)
        log.info("Attached %s (%d bytes)", filename, len(data))

    ctx = config.ssl_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=60) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
    log.info("Mailed %r to %s", subject, ", ".join(recipients))


def send_private(subject: str, body: str):
    """Mail the owner only. Application-related mail is personal, so it never goes
    to the shared recipient list."""
    if not config.MAIL_ENABLED:
        # Notice/application posts already go to WhatsApp themselves; only the
        # watcher's own health warnings need rerouting so failures stay visible.
        if subject.startswith("[ERP watcher]"):
            _whatsapp_fallback(subject, body)
        return
    _send(
        subject, body,
        f"<pre style='font-family:ui-monospace,Menlo,monospace;font-size:13px;"
        f"white-space:pre-wrap'>{html.escape(body)}</pre>",
        to=config.APPLY_NOTIFY_TO,
    )


def _whatsapp_fallback(subject: str, body: str):
    """With email off, send a watcher-health warning to WhatsApp instead (queued if
    the bridge is down). Never raises."""
    try:
        import whatsapp
        if not whatsapp.enabled():
            return
        text = f"*{subject}*\n\n{body[:1500]}"
        try:
            if not whatsapp.wait_ready(30):
                raise RuntimeError("bridge not ready")
            if not whatsapp.send(text)["sent"]:
                raise RuntimeError("no target accepted it")
        except Exception as exc:
            import db
            if db.enabled():
                db.wa_enqueue("text", {"text": text})
            log.warning("Health warning not delivered to WhatsApp (%s); queued", exc)
    except Exception as exc:
        log.warning("Health warning dropped: %s", exc)
