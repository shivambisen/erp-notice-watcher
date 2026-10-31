"""WhatsApp delivery for notice alerts.

Two official providers are supported, chosen with WHATSAPP_PROVIDER:

  meta    WhatsApp Cloud API (Meta, free tier) -- needs WHATSAPP_TOKEN and
          WHATSAPP_PHONE_ID from a Meta Business app.
  twilio  Twilio WhatsApp -- needs TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM.

Important: WhatsApp only permits free-form business-initiated messages inside a
24-hour window opened by the recipient messaging you first. Outside it, Meta
requires a pre-approved TEMPLATE. So for alerts that arrive at any hour, set
WHATSAPP_TEMPLATE to an approved template whose body has a single {{1}}
variable; the notice summary is passed into it. Plain text is used only when no
template is configured, and will silently fail outside the 24-hour window.

Unofficial WhatsApp Web automation is deliberately not supported: it violates
WhatsApp's terms and gets numbers banned.
"""
import logging
import os

import requests

import config

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"


class NotConfigured(Exception):
    """WhatsApp delivery is switched off or missing credentials."""


def enabled() -> bool:
    return config.WHATSAPP_PROVIDER in ("meta", "twilio", "bridge")


def chat_id(entry: str) -> str:
    """Bridge targets are WhatsApp chat ids. A group id is already in that form
    (...@g.us); a phone number becomes <digits>@c.us."""
    entry = entry.strip()
    if entry.endswith("@g.us") or entry.endswith("@c.us"):
        return entry
    return "".join(ch for ch in entry if ch.isdigit()) + "@c.us"


def _check():
    if config.WHATSAPP_PROVIDER == "meta":
        if not (config.WHATSAPP_TOKEN and config.WHATSAPP_PHONE_ID):
            raise NotConfigured("WHATSAPP_TOKEN and WHATSAPP_PHONE_ID are required")
    elif config.WHATSAPP_PROVIDER == "bridge":
        try:
            r = requests.get(f"{config.WA_BRIDGE_URL}/status", timeout=5)
            if not (r.json() or {}).get("ready"):
                raise NotConfigured(
                    "The WhatsApp bridge is running but not linked yet - "
                    "scan the QR shown by 'node bridge.js'."
                )
        except NotConfigured:
            raise
        except Exception:
            raise NotConfigured(
                f"No WhatsApp bridge at {config.WA_BRIDGE_URL}. "
                "Start it with: cd wa-bridge && node bridge.js"
            )
    elif config.WHATSAPP_PROVIDER == "twilio":
        if not (config.TWILIO_SID and config.TWILIO_TOKEN and config.TWILIO_FROM):
            raise NotConfigured("TWILIO_SID, TWILIO_TOKEN and TWILIO_FROM are required")
    else:
        raise NotConfigured("WHATSAPP_PROVIDER is not set to 'meta' or 'twilio'")


def summarise(notices: list) -> str:
    """Compact message body. Template variables cannot contain newlines, so
    each notice is one line."""
    import store

    lines = []
    for cells in notices[:5]:
        kind = store.cell(cells, 2)            # Urgent / Shortlist / CV Submission
        who = store.title_of(cells)            # company
        when = store.time_of(cells)
        body = store.cell(cells, config.NOTICE_BODY_COL)
        bits = [b for b in (kind, who) if b]
        line = " - ".join(bits) if bits else "New notice"
        if body:
            line += ": " + (body[:120] + ("..." if len(body) > 120 else ""))
        if when:
            line += " (" + when + ")"
        lines.append(line)
    more = len(notices) - len(lines)
    if more > 0:
        lines.append(f"+{more} more")
    head = f"{len(notices)} new CDC notice{'s' if len(notices) != 1 else ''}: "
    return (head + " | ".join(lines))[:1400]


def build_request(to: str, text: str) -> dict:
    """Exactly what would go on the wire, with the token redacted.

    Exposed so the UI can show a real preview before any credentials exist --
    useful for seeing the shape of the call, and for debugging a rejection.
    """
    if config.WHATSAPP_PROVIDER == "bridge":
        return {
            "provider": "bridge (unofficial WhatsApp Web)",
            "method": "POST",
            "url": f"{config.WA_BRIDGE_URL}/send",
            "auth": "none (local bridge)",
            "body": {"to": chat_id(to), "text": text},
            "mode": "free-form via linked account (groups supported)",
        }

    if config.WHATSAPP_PROVIDER == "twilio":
        return {
            "provider": "twilio",
            "method": "POST",
            "url": f"https://api.twilio.com/2010-04-01/Accounts/"
                   f"{config.TWILIO_SID or '<TWILIO_SID>'}/Messages.json",
            "auth": f"{config.TWILIO_SID or '<TWILIO_SID>'}:<TWILIO_TOKEN>",
            "body": {
                "From": f"whatsapp:{config.TWILIO_FROM or '<TWILIO_FROM>'}",
                "To": f"whatsapp:{to}",
                "Body": text,
            },
            "mode": "free-form (works only inside a 24h window)",
        }

    payload = _meta_payload(to, text)
    return {
        "provider": "meta",
        "method": "POST",
        "url": f"{GRAPH}/{config.WHATSAPP_PHONE_ID or '<WHATSAPP_PHONE_ID>'}/messages",
        "auth": "Bearer <WHATSAPP_TOKEN>",
        "body": payload,
        "mode": ("template '%s' (works any time)" % config.WHATSAPP_TEMPLATE
                 if config.WHATSAPP_TEMPLATE
                 else "free-form (works only inside a 24h window)"),
    }


def _meta_payload(to: str, text: str) -> dict:
    if config.WHATSAPP_TEMPLATE:
        template = {
            "name": config.WHATSAPP_TEMPLATE,
            "language": {"code": config.WHATSAPP_TEMPLATE_LANG},
        }
        # A template with no variables (e.g. Meta's "hello_world") rejects a
        # parameters block outright, so only attach one when it takes variables.
        if config.WHATSAPP_TEMPLATE_VARS > 0:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": text}],
            }]
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": template,
        }
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }


def _send_meta(to: str, text: str):
    url = f"{GRAPH}/{config.WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"}

    payload = _meta_payload(to, text)

    r = requests.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Meta API {r.status_code}: {r.text[:300]}")
    return r.json()


def _send_bridge(to: str, text: str):
    r = requests.post(
        f"{config.WA_BRIDGE_URL}/send",
        json={"to": chat_id(to), "text": text},
        timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"bridge {r.status_code}: {r.text[:300]}")
    return r.json()


def _send_twilio(to: str, text: str):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_SID}/Messages.json"
    data = {
        "From": f"whatsapp:{config.TWILIO_FROM}",
        "To": f"whatsapp:{to}",
        "Body": text,
    }
    r = requests.post(
        url, data=data, auth=(config.TWILIO_SID, config.TWILIO_TOKEN), timeout=30
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio {r.status_code}: {r.text[:300]}")
    return r.json()


def send(text: str, numbers: list | None = None) -> dict:
    """Send one message to every registered number. Returns per-number results;
    one bad number must not stop the rest, and must never break the email path."""
    _check()
    numbers = numbers if numbers is not None else config.load_whatsapp_numbers()
    if not numbers:
        raise NotConfigured("No WhatsApp numbers registered")

    sender = {
        "meta": _send_meta,
        "twilio": _send_twilio,
        "bridge": _send_bridge,
    }[config.WHATSAPP_PROVIDER]
    results = {"sent": [], "failed": {}}
    for number in numbers:
        try:
            sender(number, text)
            results["sent"].append(number)
            log.info("WhatsApp sent to %s", number)
        except Exception as exc:
            results["failed"][number] = str(exc)[:200]
            log.warning("WhatsApp to %s failed: %s", number, exc)
    return results


def tidy_body(body: str) -> str:
    """Restore the line breaks the ERP's HTML had.

    The portal's notice text arrives as one run-on line ("...2026.1. COMMONWEALTH
    BANK (Profile: X)2. URBAN COMPANY..."), which is unreadable in a chat. Put each
    numbered item on its own line, break after sentences, and set the closing
    "CDC, IIT Kharagpur" signature apart.
    """
    import re

    import html

    # entities the portal leaves escaped ("&#8288;"), sometimes twice, and the
    # invisible word-joiner / zero-width characters they decode to
    b = html.unescape(html.unescape(body or ""))
    b = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", b)
    b = " ".join(b.split())
    # links glued to the word before them ("Riskhttps://...")
    b = re.sub(r"(?<=\S)(?=https?://)", "\n", b)
    # ...and the text glued after them: a job id running into the next title
    # ("/job/26010998AI Research"), or a heading word ("...IdEApplication Process")
    b = re.sub(r"(https?://\S*?/\d{4,})(?=[A-Z])", r"\1\n", b)
    # ("?event=1538945All students": the capitalised word ends the link token)
    b = re.sub(r"(https?://\S*?\d{3,})(?=[A-Z][A-Za-z]*\s)", r"\1\n", b)
    # ("?event=15449411. Ensure": a numbered list starting right after the link)
    b = re.sub(r"(https?://\S*?\d)1\.\s+(?=[A-Z])", r"\1\n1. ", b)
    b = re.sub(r"(https?://\S*?[A-Za-z0-9])(Application|Important|Role|Registration|Interested"
               r"|Students|Please|Kindly|Note|Deadline|Venue)(?=[\s:-])", r"\1\n\2", b)
    # numbered items: "1." "12." glued after a full stop, bracket, colon or
    # space, followed by the item text (never inside a date like 28.10.2026)
    b = re.sub(r"(?:(?<=[.)\]:])|(?<=\s))(\d{1,2})\.\s*(?=[A-Za-z(\[])", r"\n\1. ", b)
    # time ranges that lost their dash ("(8:30 AM 10:30 AM)")
    b = re.sub(r"(\d{1,2}:\d{2}\s*[AP]M)\s+(?=\d{1,2}:\d{2}\s*[AP]M)", r"\1 - ", b)
    # sentence ends glued to the next sentence ("details.The test", "2026.Qualtrics")
    b = re.sub(r"(?<=[a-z0-9)])\.(?=[A-Z][a-z])", ".\n", b)
    # a heading glued to its content ("Requirements:Data Science", "Notes:Both")
    b = re.sub(r"(?<=[a-z]):(?=[A-Z])", ":\n", b)
    # a new sentence glued straight onto the previous word or bracket, with no
    # full stop ("Complex" + "The room...", "...Developer )" + "Interested ...")
    b = re.sub(r"(?<=[a-z)])(?=(?:The|Interested|Students|Please|All|Further|For more|Kindly|Note)\b)",
               "\n", b)
    # labels that start a new line in the original ("Venue:", "POC:")
    # (also when glued to the previous word: "Mode: OnlinePOC: Aditya")
    b = re.sub(r"\s*(?<![A-Z])(Venue|POC|Note|Date|Time|Deadline|Mode|Link)\s*:", r"\n\1:", b)
    # any other label glued onto the text before it ("2026Reporting Time:",
    # "11:59 PMRound 2:", "...(10 AM)Duration:")
    b = re.sub(r"(?:(?<=[a-z0-9)])|(?<=[AP]M))(?=(?:[A-Z][a-z]+ ){0,3}[A-Z][a-z]+(?: \d+)?\s*:(?!//))",
               "\n", b)
    # signature
    b = re.sub(r"\s*(CDC,?\s*IIT\s*Kharagpur\.?)\s*$", r"\n\n_\1_", b)
    lines = [ln.strip() for ln in b.split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def one_line(cells: list) -> str:
    """A single notice, formatted for a chat message."""
    import store

    kind = store.cell(cells, 2)
    who = store.title_of(cells)
    when = store.time_of(cells)
    body = tidy_body(store.cell(cells, config.NOTICE_BODY_COL))

    head = " - ".join([b for b in (kind, who) if b]) or "New CDC notice"
    text = "*" + head + "*" + ("\n" + when if when else "")
    if body:
        text += "\n\n" + (body[:3000] + ("..." if len(body) > 3000 else ""))
    return text[:3800]


def send_document(caption: str, attachment, numbers: list | None = None) -> dict:
    """Send a notice's file as a WhatsApp document, with the notice as caption.

    Only the local bridge supports this; the official providers would need a
    publicly reachable URL for the media, which we do not have.
    """
    _check()
    if config.WHATSAPP_PROVIDER != "bridge":
        raise NotConfigured("Attachments are only supported by the local bridge")

    import base64

    data, filename, ctype = attachment
    numbers = numbers if numbers is not None else config.load_whatsapp_numbers()
    results = {"sent": [], "failed": {}}
    payload_b64 = base64.b64encode(data).decode()

    for number in numbers:
        try:
            r = requests.post(
                f"{config.WA_BRIDGE_URL}/send-file",
                json={
                    "to": chat_id(number),
                    "content_b64": payload_b64,
                    "filename": filename,
                    "mimetype": ctype,
                    "caption": caption[:1000],
                },
                timeout=180,
            )
            if r.status_code >= 300:
                raise RuntimeError(f"bridge {r.status_code}: {r.text[:200]}")
            results["sent"].append(number)
            log.info("WhatsApp document %s sent to %s", filename, number)
        except Exception as exc:
            results["failed"][number] = str(exc)[:200]
            log.warning("WhatsApp document to %s failed: %s", number, exc)
    return results


def owner_targets() -> list:
    """Where watcher-health messages go: HEALTH_NOTIFY_TO, else the linked
    account's own "Message yourself" chat -- never the shared group."""
    targets = [t.strip() for t in os.getenv("HEALTH_NOTIFY_TO", "").split(",") if t.strip()]
    if targets or config.WHATSAPP_PROVIDER != "bridge":
        return targets
    try:
        me = (requests.get(f"{config.WA_BRIDGE_URL}/status", timeout=5).json() or {}).get("me") or ""
    except Exception:
        me = ""
    return [me.split(":")[0].split("@")[0]] if me else []


def wait_ready(timeout: int = 60) -> bool:
    """Wait for the bridge to be linked and connected.

    The bridge drops and re-establishes its WhatsApp connection every so often (it
    logs 'closed (408) - reconnecting'). Those windows last seconds, so wait for
    one to pass instead of treating a single failed status check as 'not linked'.
    """
    import time

    if config.WHATSAPP_PROVIDER != "bridge":
        return True
    deadline = time.time() + timeout
    while True:
        try:
            r = requests.get(f"{config.WA_BRIDGE_URL}/status", timeout=5)
            if (r.json() or {}).get("ready"):
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(5)


def send_notices(notices: list, attach_fn=None) -> dict:
    """One message per notice, oldest first so the newest is the latest message.

    Beyond ALERT_MAX_INDIVIDUAL they collapse into one digest: a burst of
    messages is both unreadable and a ban signal on the unofficial bridge.
    """
    import time

    import store

    if not notices:
        return {"sent": [], "failed": {}}
    if len(notices) > config.ALERT_MAX_INDIVIDUAL:
        log.info("%d notices exceeds the per-message cap; sending one digest",
                 len(notices))
        return send(summarise(notices))

    ordered = sorted(notices, key=store.sort_key)
    total = {"sent": [], "failed": {}}
    for i, cells in enumerate(ordered):
        if i:
            time.sleep(config.ALERT_SEND_GAP)
        text = one_line(cells)

        attachment = None
        if attach_fn is not None and config.WHATSAPP_PROVIDER == "bridge":
            try:
                attachment = attach_fn(cells)
            except Exception as exc:
                log.warning("Attachment lookup failed: %s", exc)

        try:
            if attachment:
                result = send_document(text, attachment)
            else:
                result = send(text)
        except Exception as exc:
            # Fall back to the plain message rather than losing the notice.
            log.warning("Sending with attachment failed (%s); sending text only", exc)
            result = send(text)

        total["sent"] += result["sent"]
        total["failed"].update(result["failed"])
    return total
