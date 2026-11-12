"""Daily digest of open forms and company-portal links, posted to WhatsApp.

Once a day (12:00 IST by default, see deploy/systemd/erp-digest.timer) this reads
the recent notices, picks out every Google Form and company-portal link that is
still open, and posts one message to the notice group:

    *1. AIRBUS* - Preference form
    Due: Thu 6 Nov, 1:00 PM (in 3h)
    https://forms.gle/...

Left out: meeting links (PPTs), "didn't get the test link? fill this" query
forms, CDC's dummy test notices, anything past its deadline, and Google Forms
that say they no longer accept responses.

    python digest.py            # build and send
    python digest.py --dry-run  # print the message, send nothing
"""
import argparse
import html
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

import config
import store

log = logging.getLogger("digest")

LOOKBACK_DAYS = int(os.getenv("DIGEST_LOOKBACK_DAYS", "14"))
# a link with no deadline in its notice is listed for this long after posting
NO_DEADLINE_HOURS = float(os.getenv("DIGEST_NO_DEADLINE_HOURS", "24"))

FORM_RE = re.compile(r"(?:https?://)?(?:forms\.gle/[A-Za-z0-9]{17}"
                     r"|docs\.google\.com/forms/d/(?:e/)?[A-Za-z0-9_-]{20,})")
URL_RE = re.compile(r"(?:https?://|\bapp\.joinsuperset\.com/)[^\s<>\"']+")
NOT_PORTALS = ("forms.gle", "docs.google.com", "meet.google", "zoom.", "teams.microsoft",
               "webex.", "google.com/url", "erp.iitkgp", "youtube.", "youtu.be",
               "linkedin.com", ".pdf")
PORTAL_RE = re.compile(r"\b(?:register|registration|apply|sign\s*up)\b[^.]{0,60}?\bportal\b"
                       r"|\bhirepro\b|\bsuperset\b", re.I)
# troubleshooting forms: only for people who did not get a test link etc.
QUERY_FORM_RE = re.compile(r"not\s+(?:yet\s+)?received|facing\s+(?:any\s+)?issues?|query\s+form"
                           r"|did\s+not\s+receive|haven'?t\s+received", re.I)
MAILED_RE = re.compile(r"(?:link|links)[^.]{0,80}?(?:sent|shared|triggered)[^.]{0,40}?e-?mail", re.I)


def _ist_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def _text(cells: list) -> str:
    body = html.unescape(html.unescape(store.cell(cells, config.NOTICE_BODY_COL) or ""))
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"[​-‍⁠﻿]", "", body)


def _clean_url(url: str) -> str:
    import forms
    if url.startswith("app.joinsuperset.com"):
        url = "https://" + url
    return forms._unglue(url)


def _notice_posted(cells: list):
    try:
        return datetime.strptime(store.time_of(cells), "%d-%m-%Y %H:%M")
    except (TypeError, ValueError):
        return None


def _deadline(body: str, around: int):
    """The deadline nearest after the link, else the first one in the notice."""
    import forms
    return forms.parse_deadline(body[max(0, around - 200):]) or forms.parse_deadline(body)


def extract(cells: list) -> list:
    """Every open-able item in one notice: dicts with kind, company, url, due."""
    body = _text(cells)
    company = store.title_of(cells)
    items, seen = [], set()

    for m in FORM_RE.finditer(body):
        url = m.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        if url in seen:
            continue
        seen.add(url)
        before = body[max(0, m.start() - 250):m.start()]
        if QUERY_FORM_RE.search(before):
            continue
        items.append({"kind": "form", "company": company, "url": url,
                      "due": _deadline(body, m.start())})

    for m in URL_RE.finditer(body):
        url = _clean_url(m.group(0))
        if any(h in url.lower() for h in NOT_PORTALS) or url in seen:
            continue
        seen.add(url)
        items.append({"kind": "portal", "company": company, "url": url,
                      "due": _deadline(body, m.start())})

    # a portal registration whose link went to everyone's CDC email
    if not any(i["kind"] == "portal" for i in items):
        hit = PORTAL_RE.search(body)
        if hit and not re.search(r"\bERP\s+portal\b", hit.group(0), re.I) and MAILED_RE.search(body):
            items.append({"kind": "mailed", "company": company, "url": None,
                          "due": _deadline(body, hit.start())})
    return items


def form_status(url: str) -> tuple:
    """(open?, title). A form we cannot read (sign-in only) counts as open."""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/130"})
    except Exception:
        return True, None
    if re.search(r"no longer accepting responses", r.text, re.I):
        return False, None
    m = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
    title = html.unescape(m.group(1)).strip() if m else None
    return True, title


def collect(now: datetime | None = None) -> list:
    import db

    now = now or _ist_now()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("select cells from notices where first_seen > now() - %s * interval '1 day'",
                    (LOOKBACK_DAYS,))
        rows = [r["cells"] for r in cur.fetchall()]
    rows.sort(key=store.sort_key)  # oldest first, so later notices win

    best = {}
    for cells in rows:
        if not store.should_alert(cells):
            continue
        posted = _notice_posted(cells)
        for item in extract(cells):
            due = item["due"]
            if due is None:
                if not posted or now - posted > timedelta(hours=NO_DEADLINE_HOURS):
                    continue
            key = item["url"] or f"mailed:{store.title_of(cells).lower()}"
            item["posted"] = posted
            best[key] = item  # the latest notice carries the extended deadline

    live = []
    for item in best.values():
        if item["due"] and item["due"] <= now:
            continue
        if item["kind"] == "form":
            is_open, title = form_status(item["url"])
            if not is_open:
                continue
            item["title"] = title
        live.append(item)
    live.sort(key=lambda i: (i["due"] or datetime.max, i["company"]))
    return live


def _when(due: datetime, now: datetime) -> str:
    left = due - now
    hours = left.total_seconds() / 3600
    rel = f"in {int(left.total_seconds() // 60)} min" if hours < 1 else \
          f"in {hours:.0f}h" if hours < 24 else f"in {left.days}d"
    clock = due.strftime("%I:%M %p").lstrip("0")
    return f"{due:%a} {due.day} {due:%b}, {clock} ({rel})"


def _label(item: dict) -> str:
    if item["kind"] == "mailed":
        return "Portal registration - link sent to your CDC email"
    if item["kind"] == "portal":
        return "Company portal"
    title = (item.get("title") or "").strip()
    # "AIRBUS | PREFERENCE FORM" -> "Preference form"; keep the form's own words
    title = re.sub(r"^\W*(?:urgent\W*)?(?:cdc\W*)?", "", title, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s*[|:]\s*", title) if p.strip()]
    parts = [p for p in parts if not _same(p, item["company"]) and p.upper() != "CDC"]
    name = parts[-1] if parts else "Google Form"
    return name[:1].upper() + name[1:].lower() if name.isupper() else name


def _same(a: str, b: str) -> bool:
    import forms
    return forms._same_company(a, b)


def build_message(items: list, now: datetime | None = None) -> str:
    now = now or _ist_now()
    stamp = f"{now:%a} {now.day} {now:%b}, " + now.strftime("%I:%M %p").lstrip("0")
    if not items:
        return f"*Open forms & portals - {stamp}*\n\nNothing open right now."
    lines = [f"*Open forms & portals - {stamp}*", ""]
    for n, item in enumerate(items, 1):
        lines.append(f"*{n}. {item['company'].strip()}* - {_label(item)}")
        lines.append("Due: " + (_when(item["due"], now) if item["due"] else "no deadline given"))
        if item["url"]:
            lines.append(item["url"])
        lines.append("")
    lines.append("_Fill only the ones you applied for. Check the original notice for details._")
    return "\n".join(lines).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    now = _ist_now()
    items = collect(now)
    text = build_message(items, now)
    if args.dry_run:
        print(text)
        return 0

    import db
    import whatsapp
    today = now.strftime("%Y-%m-%d")
    if db.get_meta("digest_sent_on") == today:
        log.info("Digest already sent today")
        return 0
    if not whatsapp.wait_ready(120):
        log.error("WhatsApp bridge not ready; digest not sent")
        return 1
    res = whatsapp.send(text)
    if not res["sent"]:
        log.error("Digest not sent: %s", res["failed"])
        return 1
    db.set_meta("digest_sent_on", today)
    log.info("Digest sent: %d item(s)", len(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
