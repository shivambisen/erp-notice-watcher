#!/usr/bin/env python3
"""Fills the outside forms that notices ask for (Google Forms for now).

A notice like "all students who applied for Cars24 must fill this form" links a
Google Form. For every such form, for a company you applied to:

  1. read the form's questions straight from the page (no browser needed);
  2. answer each one from profile.txt -- your own written answers. Role choices
     come from your "Role priority" line. Claude (Haiku, on your own login) only
     decides WHICH profile line answers a question; the value submitted is always
     your text, never something the model wrote;
  3. if every required question is answered, submit it (with your Google
     session, since CDC forms record a verified email) and confirm;
  4. otherwise ask you the missing questions in your own WhatsApp chat. Reply
     "F<id> 1: answer; 2: b" and it fills the rest and submits. Your replies are
     added to profile.txt so the same question is answered next time.

Forms it cannot do (file upload, grids, sign-in expired, closed) are handed to
you with the link and deadline.

  python forms.py --scan           find open forms in recent notices and act on them
  python forms.py --inbox          read your WhatsApp replies (runs every 2 minutes)
  python forms.py --check URL      read a form and show how it would be answered;
                                   submits nothing
"""
import argparse
import html
import json
import logging
import os
import re
import subprocess
import sys
from http.cookiejar import MozillaCookieJar

import requests

import config
import db

log = logging.getLogger("forms")

PROFILE_FILE = config.BASE / "profile.txt"
COOKIES_FILE = config.STATE_DIR / "google_cookies.txt"
LEARNED_HEADER = "# Learned from your WhatsApp replies"

FORMS_ENABLED = os.getenv("FORMS_AUTOFILL", "on").strip().lower() not in ("off", "0", "false", "no")
# on: submit by itself when every required answer came from your profile.
FORMS_AUTO_SUBMIT = os.getenv("FORMS_AUTO_SUBMIT", "on").strip().lower() not in ("off", "0", "false", "no")
# Also handle forms for companies you have NOT applied to (off: noise).
FORMS_ALL_COMPANIES = os.getenv("FORMS_ALL_COMPANIES", "off").strip().lower() in ("on", "1", "true", "yes")
CLAUDE_BIN = os.getenv("FORMS_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.getenv("FORMS_CLAUDE_MODEL", "haiku")

# Google Forms question types (FB_PUBLIC_LOAD_DATA_)
SHORT, PARAGRAPH, CHOICE, DROPDOWN, CHECKBOX, SCALE, TITLE, GRID, PAGE, DATE, TIME = range(11)
UPLOAD = 13
NO_INPUT = {TITLE, PAGE, 11, 12}  # text, page break, image, video

# A full browser user-agent makes forms.gle answer with a JavaScript redirect
# instead of an HTTP one; this short form gets the plain 302.
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/130"}

LINK_RE = re.compile(r"https?://(?:forms\.gle/[A-Za-z0-9]{17}"
                     r"|docs\.google\.com/forms/d/(?:e/)?[A-Za-z0-9_-]{20,}(?:/viewform)?)")


class Handoff(Exception):
    """This form needs a human: the message says why."""


# --- profile ---------------------------------------------------------------
def load_profile() -> dict:
    """{label: answer} from profile.txt, in file order. Blank answers are skipped."""
    out = {}
    try:
        lines = PROFILE_FILE.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if line.strip().startswith("#") or ":" not in line:
            continue
        label, _, value = line.partition(":")
        if label.strip() and value.strip():
            out[label.strip()] = value.strip()
    return out


def learn(label: str, answer: str):
    """Append an answer you gave on WhatsApp so the same question is answered
    next time. Later lines win over earlier ones with the same label."""
    label = " ".join(label.replace(":", " ").split())[:120]
    text = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else LEARNED_HEADER + "\n"
    if LEARNED_HEADER not in text:
        text = text.rstrip("\n") + "\n\n" + LEARNED_HEADER + "\n"
    PROFILE_FILE.write_text(text.rstrip("\n") + f"\n{label}: {answer}\n")


def role_priority(profile: dict) -> list:
    raw = next((v for k, v in profile.items() if k.lower().startswith("role priority")), "")
    return [r.strip() for r in raw.split(">") if r.strip()]


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def _role_in(role: str, option: str) -> bool:
    """Does an option name this role? Compares word stems, so "Software
    Engineering" matches "Associate Software Engineer"."""
    words = _norm(role).split()
    have = [w[:6] for w in _norm(option).split()]
    return bool(words) and all(w[:6] in have for w in words)


# --- reading a form ----------------------------------------------------------
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    if COOKIES_FILE.exists():
        jar = MozillaCookieJar(str(COOKIES_FILE))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies.update(jar)
        except Exception as exc:
            log.warning("Could not read %s: %s", COOKIES_FILE.name, exc)
    return s


def load_form(url: str, s: requests.Session) -> dict:
    r = s.get(url, timeout=30)
    if "accounts.google.com" in r.url:
        raise Handoff("the form needs a Google sign-in and the saved session is missing or expired")
    if re.search(r"no longer accepting responses", r.text, re.I):
        raise Handoff("the form is closed")
    m = re.search(r"FB_PUBLIC_LOAD_DATA_ = (.*?);</script>", r.text, re.S)
    if not m:
        raise Handoff(f"could not read the form (HTTP {r.status_code})")
    data = json.loads(m.group(1))
    fbzx = re.search(r'name="fbzx" value="(-?\d+)"', r.text)
    info = data[1]
    settings = info[10] if len(info) > 10 and info[10] else []
    questions, pages = [], 0
    for item in info[1] or []:
        qtype = item[3]
        if qtype == PAGE:
            pages += 1
        if qtype in NO_INPUT or not item[4]:
            continue
        for sub in item[4]:  # a grid has one entry per row; everything else has one
            questions.append({
                "entry": sub[0],
                "title": (item[1] or "").strip() + (f" [{sub[3][0]}]" if qtype == GRID and sub[3] else ""),
                "help": (item[2] or "").strip()[:300],
                "type": qtype,
                "options": [o[0] for o in (sub[1] or []) if o and o[0]],
                "required": bool(sub[2]),
            })
    return {
        "url": url,
        "action": re.sub(r"/viewform.*$", "", r.url.split("?")[0]) + "/formResponse",
        "title": info[8] or (data[3] if len(data) > 3 else "") or "form",
        "fbzx": fbzx.group(1) if fbzx else (str(data[14]) if len(data) > 14 else ""),
        "pages": pages,
        # 1 = no email, 2 = verified (Google sign-in), 3 = responder types it
        "email_mode": settings[6] if len(settings) > 6 else 1,
        "questions": questions,
    }


# --- answering ---------------------------------------------------------------
def _ask_claude(form: dict, profile: dict) -> dict:
    """{entry: {"label": profile label or None, "option": exact option or None}}."""
    qs = [{"id": q["entry"], "question": q["title"], "help": q["help"][:150],
           "options": q["options"] or None} for q in form["questions"]]
    prompt = (
        "Match each form question to the ONE profile line that answers it.\n"
        "Rules: return the profile label exactly as written, or null if no line clearly answers it. "
        "Never guess. For a question with options, also return the option (copied exactly) that "
        "the profile line's answer means, or null if none clearly matches.\n\n"
        f"PROFILE (label: answer):\n" + "\n".join(f"{k}: {v}" for k, v in profile.items())
        + "\n\nQUESTIONS:\n" + json.dumps(qs, ensure_ascii=False)
    )
    schema = {"type": "object", "properties": {"answers": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "label": {"type": ["string", "null"]},
                                         "option": {"type": ["string", "null"]}},
        "required": ["id", "label", "option"]}}}, "required": ["answers"]}
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", CLAUDE_MODEL, "--tools", "", "--output-format", "json",
           "--no-session-persistence", "--json-schema", json.dumps(schema),
           "--system-prompt", "You map application-form questions to a student's saved profile lines. "
                              "Answer only through the structured output."]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(config.STATE_DIR))
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:200]}")
    out = json.loads(p.stdout)
    res = out.get("structured_output") or json.loads(out.get("result") or "{}")
    return {a["id"]: a for a in res.get("answers", [])}


def _rule_match(q: dict, profile: dict):
    """Fallback without Claude: the profile label sharing the most words."""
    qw = set(_norm(q["title"]).split()) - {"your", "the", "of", "in", "please", "enter", "id", "no"}
    best, score = None, 0.0
    for label in profile:
        lw = set(_norm(label).split()) - {"your", "the", "of", "id", "no"}
        if qw and lw:
            s = len(qw & lw) / len(qw | lw)
            if s > score:
                best, score = label, s
    return best if score >= 0.5 else None


def answer(form: dict, profile: dict) -> tuple[dict, list]:
    """Returns ({entry: value or [values]}, [unanswered questions]). Every value is
    taken from profile.txt; a choice must be one of the form's own options."""
    priority = role_priority(profile)
    try:
        hints = _ask_claude(form, profile) if form["questions"] else {}
    except Exception as exc:
        log.warning("Claude unavailable, using keyword matching: %s", exc)
        hints = {}
    answers, missing = {}, []
    for q in form["questions"]:
        hint = hints.get(q["entry"]) or {}
        label = hint.get("label") if hint.get("label") in profile else _rule_match(q, profile)
        value = profile.get(label) if label else None
        opts = q["options"]
        picked = None
        if q["type"] in (CHOICE, DROPDOWN, SCALE):
            # a role question: your priority list decides, not the model
            ranked = [o for r in priority for o in opts if _role_in(r, o)]
            if ranked and not value:
                picked = ranked[0]
            elif value:
                cand = hint.get("option") if hint.get("option") in opts else None
                exact = [o for o in opts if _norm(o) == _norm(value)]
                # accept the model's option only if it really is what your answer says
                if exact:
                    picked = exact[0]
                elif cand and (_norm(cand) in _norm(value) or _norm(value) in _norm(cand)):
                    picked = cand
            if picked:
                answers[q["entry"]] = picked
        elif q["type"] == CHECKBOX:
            chosen = [o for o in opts if value and _norm(o) in {_norm(v) for v in value.split(",")}]
            if chosen:
                answers[q["entry"]] = chosen
        elif q["type"] in (SHORT, PARAGRAPH, DATE, TIME) and value:
            answers[q["entry"]] = value
        if q["entry"] not in answers and q["required"]:
            if q["type"] in (UPLOAD, GRID):
                raise Handoff(f"it has a question this filler cannot answer ({q['title'][:60]})")
            missing.append(q)
    return answers, missing


# --- submitting ---------------------------------------------------------------
def _email(profile: dict) -> str:
    for k, v in profile.items():
        if "cdc" in k.lower() and "mail" in k.lower():
            return v
    return next((v for k, v in profile.items() if "mail" in k.lower()), "")


def submit(form: dict, answers: dict, profile: dict, s: requests.Session):
    if form["email_mode"] == 2 and not COOKIES_FILE.exists():
        raise Handoff("it records a verified Google email, and no Google session is saved yet "
                      "(see AUTO_APPLY.md: Outside forms)")
    data = []
    types = {q["entry"]: q["type"] for q in form["questions"]}
    for entry, value in answers.items():
        if types.get(entry) == DATE:
            m = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", value) or None
            if not m:
                continue
            data += [(f"entry.{entry}_day", m.group(1)), (f"entry.{entry}_month", m.group(2)),
                     (f"entry.{entry}_year", m.group(3))]
        elif isinstance(value, list):
            data += [(f"entry.{entry}", v) for v in value]
        else:
            data.append((f"entry.{entry}", value))
    if form["email_mode"] == 3:
        data.append(("emailAddress", _email(profile)))
    data += [("fvv", "1"), ("pageHistory", ",".join(str(i) for i in range(form["pages"] + 1))),
             ("fbzx", form["fbzx"]), ("partialResponse", json.dumps([None, None, form["fbzx"]]))]
    r = s.post(form["action"], data=data, timeout=30, headers={"Referer": form["url"]})
    if "accounts.google.com" in r.url:
        raise Handoff("the saved Google session has expired - export the cookies again")
    done = r.status_code == 200 and re.search(
        r"freebirdFormviewerViewResponseConfirmationMessage|Your response has been recorded"
        r"|Submit another response|Edit your response", r.text)
    if not done:
        raise RuntimeError(f"Google did not confirm the submission (HTTP {r.status_code})")


# --- WhatsApp ------------------------------------------------------------------
def _tell(text: str):
    import whatsapp

    try:
        to = whatsapp.owner_targets()
        if to and whatsapp.wait_ready(30) and whatsapp.send(text, to)["sent"]:
            return
        raise RuntimeError("not delivered")
    except Exception as exc:
        log.warning("WhatsApp message failed (%s); queued", exc)
        to = whatsapp.owner_targets()
        if to:
            db.wa_enqueue("text", {"text": text, "to": to})


def _letters(opts: list) -> str:
    return "  ".join(f"{chr(97 + i)}) {o}" for i, o in enumerate(opts[:26]))


def _summary(form: dict, answers: dict) -> str:
    titles = {q["entry"]: q["title"] for q in form["questions"]}
    return "\n".join(f"- {titles.get(e, e)[:50]}: {', '.join(v) if isinstance(v, list) else v}"
                     for e, v in answers.items())


# --- the flow --------------------------------------------------------------------
def handle(task: dict, extra: dict | None = None):
    """Try to fill and submit one form task. extra: {entry: value} from your reply."""
    if not db.form_claim(task["id"], ("new", "asked", "failed")):
        return
    head = f"{task['company'] or 'CDC'} - {task['title'] or 'form'}"
    try:
        s = session()
        form = load_form(task["url"], s)
        profile = load_profile()
        answers, missing = answer(form, profile)
        answers.update(extra or {})
        missing = [q for q in missing if q["entry"] not in answers]
        if missing or not FORMS_AUTO_SUBMIT:
            ask = missing or [q for q in form["questions"] if q["required"]]
            lines = [f"*Form needs your answers: {head}*",
                     f"Deadline: {task['deadline']}" if task["deadline"] else "", form["title"], ""]
            for i, q in enumerate(ask, 1):
                lines.append(f"{i}. {q['title']}" + (f"\n   {_letters(q['options'])}" if q["options"] else ""))
            if answers:
                lines += ["", "Already filled from your profile:", _summary(form, answers)]
            lines += ["", f"Reply here:  F{task['id']} 1: your answer; 2: b",
                      "(letters pick an option; answers are remembered for next time)", task["url"]]
            db.form_set(task["id"], "asked", {"ask": [q["entry"] for q in ask],
                                             "ask_titles": [q["title"] for q in ask], "filled": answers})
            _tell("\n".join(x for x in lines if x is not None))
            log.info("Form %s: asked you %d question(s)", task["id"], len(ask))
            return
        submit(form, answers, profile, s)
        db.form_set(task["id"], "submitted", {"answers": answers})
        _tell(f"*Form submitted: {head}*\n{_summary(form, answers)}\n\nConfirmed by Google. {task['url']}")
        log.info("Form %s submitted", task["id"])
    except Handoff as exc:
        db.form_set(task["id"], "handoff", {"reason": str(exc)})
        _tell(f"*Fill this form yourself: {head}*\nWhy: {exc}\n"
              + (f"Deadline: {task['deadline']}\n" if task["deadline"] else "") + task["url"])
        log.info("Form %s handed to you: %s", task["id"], exc)
    except Exception as exc:
        db.form_set(task["id"], "failed", {"error": str(exc)[:300]})
        log.warning("Form %s failed: %s", task["id"], exc)
        if task.get("attempts", 0) >= 2:
            db.form_set(task["id"], "handoff", {"reason": str(exc)[:300]})
            _tell(f"*Could not submit a form: {head}*\n{str(exc)[:200]}\nPlease fill it yourself: {task['url']}")


def _applied_companies() -> list:
    return [p["company"] for p in db.load_profiles().values()
            if (p.get("status") or "").startswith("applied") and p.get("company")]


_STOP = {"pvt", "ltd", "private", "limited", "india", "services", "solutions", "technologies",
         "the", "and", "llp", "inc", "co", "company", "group", "global"}


def _same_company(a: str, b: str) -> bool:
    wa = set(_norm(a).split()) - _STOP
    wb = set(_norm(b).split()) - _STOP
    return bool(wa and wb and (wa <= wb or wb <= wa or _norm(a).split()[0] == _norm(b).split()[0]))


def from_notice(cells: list) -> list:
    """Queue the forms in one notice. Returns the new task ids."""
    import store

    body = store.cell(cells, config.NOTICE_BODY_COL) or ""
    links = list(dict.fromkeys(LINK_RE.findall(body)))
    if not links:
        return []
    company = store.title_of(cells)
    if not FORMS_ALL_COMPANIES and not any(_same_company(company, c) for c in _applied_companies()):
        log.info("Form in notice for %s skipped: you have not applied there", company)
        return []
    dl = re.search(r"(?:Deadline\s*:?|by)\s*([^.\n]{4,60}?(?:AM|PM)[^.\n]{0,30})", body, re.I)
    ids = []
    for url in links:
        tid = db.form_add(url, company, store.cell(cells, 2) or "", dl.group(1).strip() if dl else "")
        if tid:
            ids.append(tid)
    return ids


# "register on the portal", "apply on the company portal", "HirePro". Signing up
# needs your password or OTP, so these are never filled automatically: you get
# an alert, then a reminder before the deadline until you reply "P<id> done".
PORTAL_RE = re.compile(r"\b(?:register|registration|apply)\b[^.]{0,60}?\bportal\b|hirepro", re.I)
PORTAL_SKIP_HOSTS = ("forms.gle", "docs.google.com/forms", "meet.google", "zoom.", "teams.microsoft",
                     "webex.", "google.com/url")
PORTAL_REMIND_HOURS = float(os.getenv("PORTAL_REMIND_HOURS", "3"))
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def _ist_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def parse_deadline(text: str):
    """The first "3rd November 2026" in text, with a time within ~40 chars of it
    ("4:00 PM, 3rd November" or "3rd November 2026, 11:59 PM"). IST, naive."""
    from datetime import datetime

    for m in re.finditer(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", text):
        month = _MONTHS.get(m.group(2).lower())
        if not month:
            continue
        near = text[max(0, m.start() - 40):m.end() + 40]
        t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M", near, re.I)
        hour, minute = 23, 59
        if t:
            hour = int(t.group(1)) % 12 + (12 if t.group(3).upper() == "P" else 0)
            minute = int(t.group(2) or 0)
        try:
            return datetime(int(m.group(3)), month, int(m.group(1)), hour, minute)
        except ValueError:
            continue
    return None


def _unglue(url: str) -> str:
    """Notices glue the next sentence onto links ("…/CgMDiOHYPlease note")."""
    url = re.split(r"[\u200b-\u200d\u2060\ufeff]", url)[0]
    url = re.sub(r"(?:[A-Z]{2,}:|(?:Please|Note|Students|Deadline|Kindly|Interested|Engineer|All|The|CDC|Registration)[A-Za-z]*:?)$", "", url)
    url = re.sub(r"/\d{1,2}\.$", "/", url)  # a glued "4." list number
    return url.rstrip(".,)")


def portal_from_notice(cells: list):
    """Alert once about a portal registration in this notice. Returns the task id."""
    import store

    body = html.unescape(store.cell(cells, config.NOTICE_BODY_COL) or "")
    hit = PORTAL_RE.search(body)
    if not hit or re.search(r"\bERP\s+portal\b", hit.group(0), re.I):
        return None
    company = store.title_of(cells)
    if not FORMS_ALL_COMPANIES and not any(_same_company(company, c) for c in _applied_companies()):
        return None
    links = [_unglue(u) for u in re.findall(r"https?://[^\s<>\"']+", body)
             if not any(h in u for h in PORTAL_SKIP_HOSTS)]
    due = parse_deadline(body[hit.start():]) or parse_deadline(body)
    # one alert per portal link, or per company + deadline when the link was only mailed
    key = links[0] if links else f"portal:{_norm(company)}:{due:%Y-%m-%d %H:%M}" if due else f"portal:{_norm(company)}:{store.cell(cells, 0)}"
    tid = db.form_add(key, company, store.cell(cells, 2) or "", f"{due:%d %b %Y, %I:%M %p}" if due else "")
    if not tid:
        return None
    db.form_set(tid, "portal", {"due": due.isoformat() if due else None, "link": links[0] if links else None})
    if due and due < _ist_now():
        db.form_set(tid, "portal_done", {"note": "deadline already passed"})
        return tid
    _tell(f"⚠️ *Portal sign-up needed: {company}*\n"
          f"Deadline: {f'{due:%d %b, %I:%M %p}' if due else 'see notice'}\n"
          f"Link: {links[0] if links else 'sent to your email (check inbox and spam)'}\n\n"
          f"Sign up or log in, then ask Claude to fill the rest.\n"
          f"Reply *P{tid} done* here once finished"
          + (f", otherwise I'll remind you {PORTAL_REMIND_HOURS:g}h before the deadline." if due else "."))
    return tid


def portal_reminders():
    """Remind once, PORTAL_REMIND_HOURS before the deadline, about open portals."""
    from datetime import datetime, timedelta

    now = _ist_now()
    for t in db.form_list(("portal",)):
        d = t["detail"] or {}
        if not d.get("due") or d.get("reminded"):
            continue
        due = datetime.fromisoformat(d["due"])
        if due < now:
            db.form_set(t["id"], "portal_done", {"note": "deadline passed"})
        elif due - now <= timedelta(hours=PORTAL_REMIND_HOURS):
            left = int((due - now).total_seconds() // 60)
            _tell(f"⏰ *{t['company']} portal closes in {left // 60}h {left % 60}m* ({due:%I:%M %p}).\n"
                  f"Link: {d.get('link') or 'in your email'}\nReply *P{t['id']} done* if already finished.")
            db.form_set(t["id"], "portal", {"reminded": True})


def process_notices(rows: list):
    """Called by the watcher with the poll's new notices. Never raises."""
    if not (FORMS_ENABLED and db.enabled()):
        return
    try:
        db.init()
        for cells in rows:
            for tid in from_notice(cells):
                handle(db.form_get(tid))
            portal_from_notice(cells)
    except Exception as exc:
        log.warning("Form handling failed: %s", exc)


def read_inbox():
    """Apply your WhatsApp replies ("F12 1: answer; 2: b") and finish those forms."""
    import whatsapp

    if config.WHATSAPP_PROVIDER != "bridge":
        return
    start = after = float(db.get_meta("forms_inbox_after", 0) or 0)
    try:
        msgs = requests.get(f"{config.WA_BRIDGE_URL}/inbox", params={"after": after}, timeout=10).json()["messages"]
    except Exception as exc:
        log.info("Inbox unavailable: %s", exc)
        return
    for m in msgs:
        after = max(after, m["ts"])
        done = re.match(r"\s*P(\d+)\s+done\b", m["text"], re.I)
        if done:
            task = db.form_get(int(done.group(1)))
            if task and task["status"] == "portal":
                db.form_set(task["id"], "portal_done", {"note": "you marked it done"})
                _tell(f"✓ {task['company']} portal marked done.")
            continue
        hit = re.match(r"\s*F(\d+)\b[\s:.-]*(.*)", m["text"], re.S | re.I)
        if not hit:
            continue
        task = db.form_get(int(hit.group(1)))
        if not task or task["status"] != "asked":
            continue
        detail = task["detail"] or {}
        asked, titles = detail.get("ask", []), detail.get("ask_titles", [])
        form = load_form(task["url"], session())
        byentry = {q["entry"]: q for q in form["questions"]}
        extra = {}
        for part in re.split(r"[;\n]+", hit.group(2)):
            pm = re.match(r"\s*(\d+)\s*[:.)-]\s*(.+)", part)
            if not pm or not (1 <= int(pm.group(1)) <= len(asked)):
                continue
            entry = asked[int(pm.group(1)) - 1]
            q, val = byentry.get(entry), pm.group(2).strip()
            if q and q["options"] and re.fullmatch(r"[a-zA-Z]", val) and ord(val.lower()) - 97 < len(q["options"]):
                val = q["options"][ord(val.lower()) - 97]
            extra[entry] = val
            # remember plain facts; a pick between roles is per company, not a fact
            if q and not (q["options"] and role_priority(load_profile())):
                learn(titles[int(pm.group(1)) - 1], val)
        if extra:
            handle(task, extra={**(detail.get("filled") or {}), **extra})
    if after != start:
        db.set_meta("forms_inbox_after", after)


def scan(days: int = 3):
    """Act on forms in recent notices (for catching up, or after first setup)."""
    import store
    from datetime import datetime, timedelta

    cutoff = datetime.now() - timedelta(days=days)
    rows = []
    for row in db.load_notices(300):
        cells = row if isinstance(row, list) else row.get("cells", row)
        try:
            when = datetime.strptime(store.time_of(cells)[:16], "%d-%m-%Y %H:%M")
        except ValueError:
            continue
        if when >= cutoff:
            rows.append(cells)
    process_notices(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--inbox", action="store_true")
    ap.add_argument("--check", metavar="URL")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.check:
        form = load_form(args.check, session())
        print(f"{form['title']}  (email: {['', 'none', 'verified Google sign-in', 'typed'][form['email_mode']]})")
        answers, missing = answer(form, load_profile())
        for q in form["questions"]:
            v = answers.get(q["entry"])
            print(f"  {'*' if q['required'] else ' '} {q['title'][:55]:55} -> {v if v else 'ASK YOU'}")
        print(f"\n{len(answers)} answered, {len(missing)} required question(s) would be asked on WhatsApp")
        return 0
    if args.inbox:
        read_inbox()
        portal_reminders()
    if args.scan:
        scan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
