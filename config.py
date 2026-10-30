"""Configuration loaded from .env (see .env.example)."""
import json
import os
import re
import ssl
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

STATE_DIR = Path(os.getenv("STATE_DIR") or (BASE / "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_FILE = STATE_DIR / "cookies.txt"
SEEN_FILE = STATE_DIR / "seen.json"
LOG_FILE = STATE_DIR / "watcher.log"


def ssl_context() -> "ssl.SSLContext":
    """TLS context for SMTP/IMAP.

    The python.org macOS builds ship with an empty trust store, so
    ssl.create_default_context() fails to verify anything until you run
    "Install Certificates.command". requests sidesteps that by bundling certifi;
    smtplib and imaplib do not, so do the same thing explicitly here.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _req(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise SystemExit(f"Missing required setting {key} in {BASE / '.env'}")
    return val


def parse_answer_map(raw: str | None) -> tuple[dict, str]:
    """Parse the security question -> answer mapping.

    Returns (mapping, problem). This setting is optional and hand-edited, so a
    typo must never block the whole tool -- on failure we return an empty map
    plus a description, and the caller falls back to the default answer.

    Deliberately forgiving: accepts real JSON, Python-ish dicts with single
    quotes, and the plain form people reach for first --
        question = answer; other question = other answer
    """
    raw = (raw or "").strip()
    if not raw or raw in ("{}", "[]", "none", "None"):
        return {}, ""

    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return {str(k): str(v) for k, v in loaded.items()}, ""
    except json.JSONDecodeError:
        pass

    try:
        import ast

        loaded = ast.literal_eval(raw)  # single quotes, trailing commas
        if isinstance(loaded, dict):
            return {str(k): str(v) for k, v in loaded.items()}, ""
    except (ValueError, SyntaxError):
        pass

    body = raw.strip().lstrip("{").rstrip("}")
    entries = [e for e in (p.strip().strip(",").strip() for p in re.split(r"[;\n]+", body)) if e]
    # People often separate pairs with commas instead; only re-split on commas
    # when that actually yields more question/answer pairs.
    if len(entries) == 1 and "," in body and len(re.findall(r"[:=]", body)) > 1:
        entries = [e for e in (p.strip() for p in body.split(",")) if e]

    out, bad = {}, []
    for entry in entries:
        m = re.match(r"^(.*?)\s*[:=]\s*(.*)$", entry)
        if not m:
            bad.append(entry)
            continue
        key = m.group(1).strip().strip("\"'").strip()
        val = m.group(2).strip().strip(",").strip().strip("\"'").strip()
        if key and val:
            out[key] = val
        else:
            bad.append(entry)

    if bad:
        # Report the shape of what failed, never the answers themselves.
        shapes = ", ".join(f"{len(b)} chars, no ':' or '='" for b in bad[:3])
        return out, (
            f"{len(bad)} entr{'y' if len(bad) == 1 else 'ies'} could not be read ({shapes}). "
            "Expected: ERP_SECURITY_ANSWERS=<question words> = <answer>; <question words> = <answer>"
        )
    return out, ""


# --- ERP credentials -------------------------------------------------------
ERP_USER_ID = _req("ERP_USER_ID")
ERP_PASSWORD = _req("ERP_PASSWORD")

# The ERP picks one of your registered security questions at random on each
# login, so a single answer is not enough. ERP_SECURITY_ANSWERS maps a distinctive
# substring of the question to its answer; ERP_SECURITY_ANSWER_DEFAULT is the
# fallback when no mapping matches.
ERP_SECURITY_ANSWER_DEFAULT = _req("ERP_SECURITY_ANSWER_DEFAULT")
ERP_SECURITY_ANSWERS, ERP_SECURITY_ANSWERS_PROBLEM = parse_answer_map(
    os.getenv("ERP_SECURITY_ANSWERS")
)

# --- Mailbox the ERP sends the login OTP to --------------------------------
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = _req("IMAP_USER")


def normalise_app_password(value: str) -> str:
    """Google shows app passwords as 'abcd efgh ijkl mnop'; the spaces are only
    display formatting and IMAP/SMTP logins fail if they are sent. Strip them,
    but only when the result is exactly the 16-character app-password shape, so
    a real password that genuinely contains spaces is left alone."""
    stripped = re.sub(r"\s+", "", value)
    if len(stripped) == 16 and stripped.isalnum() and stripped != value:
        return stripped
    return value


IMAP_PASSWORD = normalise_app_password(_req("IMAP_PASSWORD"))
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip()
OTP_SENDER_HINT = os.getenv("OTP_SENDER_HINT", "erp").strip().lower()
OTP_WAIT_SECONDS = int(os.getenv("OTP_WAIT_SECONDS", "180"))

# --- Where notice alerts get sent ------------------------------------------
# Email alerts. "off" sends no email at all (WhatsApp and auto-apply carry on).
# IMAP is still used to read the ERP login OTP either way.
MAIL_ENABLED = os.getenv("MAIL_ENABLED", "on").strip().lower() not in ("off", "0", "false", "no")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "").strip() or IMAP_USER
SMTP_PASSWORD = normalise_app_password(os.getenv("SMTP_PASSWORD", "").strip()) or IMAP_PASSWORD
MAIL_FROM = os.getenv("MAIL_FROM", "").strip() or SMTP_USER
# Recipients live in a separate, secret-free JSON file so the local web UI can
# rewrite them without ever touching .env (which holds passwords). MAIL_TO in
# .env stays the fallback for a fresh install.
RECIPIENTS_FILE = STATE_DIR / "recipients.json"


def load_recipients() -> list:
    env_list = [a.strip() for a in os.getenv("MAIL_TO", "").split(",") if a.strip()]
    try:
        import db
        if db.enabled():
            chosen = db.load_recipients("email")
            if chosen:
                return chosen
    except Exception as exc:  # never let a DB problem stop alerts
        import logging
        logging.getLogger(__name__).warning("DB recipients unavailable (%s)", exc)
    try:
        if RECIPIENTS_FILE.exists():
            data = json.loads(RECIPIENTS_FILE.read_text())
            chosen = [str(e).strip() for e in data.get("recipients", []) if str(e).strip()]
            if chosen:
                return chosen
    except Exception:
        pass  # a corrupt file must not stop alerts going out
    return env_list or [SMTP_USER]


MAIL_TO = load_recipients()

# --- WhatsApp (optional) ---------------------------------------------------
# "off" (default), "meta" (Cloud API), "twilio", or "bridge".
# "bridge" talks to wa-bridge/ -- unofficial WhatsApp Web automation. It is the
# only option that can post into a GROUP (the official API has no group support)
# and it risks the linked number being banned.
WA_BRIDGE_URL = os.getenv("WA_BRIDGE_URL", "http://127.0.0.1:8766").strip().rstrip("/")
WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "off").strip().lower()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "").strip()
# Meta requires an approved template for business-initiated messages (i.e. any
# alert sent outside a 24h window the recipient opened). Its body needs one
# {{1}} variable, which receives the notice summary.
WHATSAPP_TEMPLATE = os.getenv("WHATSAPP_TEMPLATE", "").strip()
WHATSAPP_TEMPLATE_LANG = os.getenv("WHATSAPP_TEMPLATE_LANG", "en").strip()
# Number of {{n}} variables in the template body. Meta's instantly-available
# "hello_world" demo template has none, and sending a parameter to a template
# that takes none is rejected -- set this to 0 for it.
WHATSAPP_TEMPLATE_VARS = int(os.getenv("WHATSAPP_TEMPLATE_VARS", "1") or 1)
TWILIO_SID = os.getenv("TWILIO_SID", "").strip()
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "").strip()
TWILIO_FROM = os.getenv("TWILIO_FROM", "").strip()


def load_whatsapp_numbers() -> list:
    """Registered numbers/group ids, managed from the local web UI."""
    try:
        import db
        if db.enabled():
            chosen = db.load_recipients("whatsapp")
            if chosen:
                return chosen
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("DB whatsapp targets unavailable (%s)", exc)
    try:
        if RECIPIENTS_FILE.exists():
            data = json.loads(RECIPIENTS_FILE.read_text())
            return [str(n).strip() for n in data.get("whatsapp", []) if str(n).strip()]
    except Exception:
        pass
    return [n.strip() for n in os.getenv("WHATSAPP_TO", "").split(",") if n.strip()]


# --- Notice board ----------------------------------------------------------
NOTICE_ROWS = int(os.getenv("NOTICE_ROWS", "50"))
JQ_QUERY_ID = os.getenv("JQ_QUERY_ID", "37").strip()
# The page that owns the grid. ERPMonitoring.htm only answers for a jqqueryid
# whose page has been rendered in this session -- otherwise it returns a
# truncated stub. Grid 54 lives on Notice.jsp, grid 37 on TPStudent.jsp.
NOTICE_PAGE = os.getenv("NOTICE_PAGE", "Notice.jsp").strip()
# Optional comma-separated labels for the grid columns, used in the email body.
NOTICE_COLUMNS = [c.strip() for c in os.getenv("NOTICE_COLUMNS", "").split(",") if c.strip()]

# Which column holds what, so alerts and the UI can label things sensibly.
# Grid 54 (the CDC notice board) is:
#   0 ID  1 Type  2 Subject  3 Company  4 Notice  5 Ref  6 Notice Time
#   7 Download  8 Upload  9 Deactivate
NOTICE_TITLE_COL = int(os.getenv("NOTICE_TITLE_COL", "0") or 0)
NOTICE_TIME_COL = int(os.getenv("NOTICE_TIME_COL", "-1") or -1)
NOTICE_BODY_COL = int(os.getenv("NOTICE_BODY_COL", "-1") or -1)
NOTICE_HIDE_COLS = [
    int(c) for c in os.getenv("NOTICE_HIDE_COLS", "").split(",") if c.strip().isdigit()
]

# Notice types to store but never alert on, matched case-insensitively against
# the Type column. They still appear in the web UI; they just do not mail or
# message you. Empty means alert on everything.
NOTICE_TYPE_COL = int(os.getenv("NOTICE_TYPE_COL", "1") or 1)
NOTICE_EXCLUDE_TYPES = [
    x.strip().lower() for x in os.getenv("NOTICE_EXCLUDE_TYPES", "").split(",") if x.strip()
]
# Optional extra net: skip a notice whose title or body contains any of these.
NOTICE_EXCLUDE_KEYWORDS = [
    x.strip().lower() for x in os.getenv("NOTICE_EXCLUDE_KEYWORDS", "").split(",") if x.strip()
]

# One alert per notice. If a single poll turns up more than this (a backlog
# after an outage, say), they are collapsed into one digest instead of firing
# dozens of separate messages -- which also looks like spam to WhatsApp.
ALERT_MAX_INDIVIDUAL = int(os.getenv("ALERT_MAX_INDIVIDUAL", "15"))
# Pause between individual WhatsApp sends; bursts are a ban signal.
ALERT_SEND_GAP = float(os.getenv("ALERT_SEND_GAP", "2.5"))

# --- New-company alerts ----------------------------------------------------
# Watches the company list (grid 37). When a new job profile appears that the
# portal itself says you are eligible for, you get an email and a WhatsApp post
# with the deadline. With AUTO_APPLY=on it also applies for you (below).
COMPANY_ALERTS = os.getenv("COMPANY_ALERTS", "on").strip().lower() not in ("off", "0", "false")
# AUTO_APPLY=on submits an application, with the chosen CV, to every listed profile
# the portal itself currently offers you the Apply button for (so: open, eligible,
# not yet applied). Off unless explicitly switched on. No cap on how many.
AUTO_APPLY = os.getenv("AUTO_APPLY", "off").strip().lower() in ("on", "1", "true", "yes")
AUTO_APPLY_CV = int(os.getenv("AUTO_APPLY_CV", "2") or 2)
# A failed attempt is retried on later polls, at most this many times per profile.
AUTO_APPLY_MAX_ATTEMPTS = int(os.getenv("AUTO_APPLY_MAX_ATTEMPTS", "3") or 3)
# Only alert for these form types (the profile page's "Form Type" column).
COMPANY_ALERT_FORM_TYPES = [
    x.strip().upper() for x in os.getenv("COMPANY_ALERT_FORM_TYPES", "PLACEMENT").split(",") if x.strip()
]
# Hours before a CV window closes at which a still-unapplied, eligible profile
# gets a reminder (also one final reminder in the last hour). 0 turns reminders off.
COMPANY_REMINDER_HOURS = int(os.getenv("COMPANY_REMINDER_HOURS", "4") or 0)
# Also post the alerts and reminders to the WhatsApp targets (the CDC group).
# Best effort: a WhatsApp problem never blocks the email or loses the alert.
COMPANY_ALERT_WHATSAPP = os.getenv("COMPANY_ALERT_WHATSAPP", "on").strip().lower() not in ("off", "0", "false")
COMPANY_ALERT_SKIP = [
    x.strip().lower() for x in os.getenv("COMPANY_ALERT_SKIP", "").split(",") if x.strip()
]
COMPANY_ALERT_MIN_CTC = int(os.getenv("COMPANY_ALERT_MIN_CTC", "0") or 0)
# Mention profiles you are NOT eligible for too (off by default: it is noise).
COMPANY_ALERT_INELIGIBLE = os.getenv("COMPANY_ALERT_INELIGIBLE", "0").strip() in ("1", "true", "yes")
# Application-related email goes to the owner only, not the shared recipients
# list. (WhatsApp posts go to the WhatsApp targets, per COMPANY_ALERT_WHATSAPP.)
APPLY_NOTIFY_TO = [a.strip() for a in os.getenv("APPLY_NOTIFY_TO", "").split(",") if a.strip()] or [IMAP_USER]

# Skip the alert mail on the very first run, which would otherwise send you all
# 50 existing notices. Set to 0 if you do want that initial dump.
SEED_SILENTLY = os.getenv("SEED_SILENTLY", "1").strip() not in ("0", "false", "False")
