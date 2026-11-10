"""Tracks which notices have already been mailed."""
import hashlib
import json
import logging
import re
import time

import config

log = logging.getLogger(__name__)

SEP = chr(31)  # unit separator: safe to join cells on


def fingerprint(cells: list[str]) -> str:
    joined = SEP.join(c.strip() for c in cells)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def load() -> dict:
    if not config.SEEN_FILE.exists():
        return {"seen": [], "seeded": False}
    try:
        data = json.loads(config.SEEN_FILE.read_text())
        data.setdefault("seen", [])
        data.setdefault("seeded", False)
        return data
    except Exception as exc:
        log.warning("Could not read %s (%s); starting fresh", config.SEEN_FILE, exc)
        return {"seen": [], "seeded": False}


def save(state: dict):
    # Keep the file from growing without bound; the board only shows ~50 rows.
    state["seen"] = state["seen"][-1000:]
    tmp = config.SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(config.SEEN_FILE)


NOTICES_FILE = config.STATE_DIR / "last_notices.json"


def save_notices(rows: list):
    """Snapshot what the board looked like, so the UI can render it instantly
    without touching the ERP (and without risking an OTP login on page load)."""
    try:
        tmp = NOTICES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "fetched_at": time.time(),
            "rows": rows,
        }, indent=1))
        tmp.replace(NOTICES_FILE)
    except Exception as exc:
        log.warning("Could not save notice snapshot: %s", exc)


def load_notices() -> dict:
    try:
        if NOTICES_FILE.exists():
            return json.loads(NOTICES_FILE.read_text())
    except Exception as exc:
        log.warning("Could not read notice snapshot: %s", exc)
    return {"fetched_at": None, "rows": []}


def cell(cells: list, idx: int, default: str = "") -> str:
    return str(cells[idx]).strip() if 0 <= idx < len(cells) else default


def title_of(cells: list) -> str:
    return cell(cells, config.NOTICE_TITLE_COL) or cell(cells, 0) or "Notice"


def time_of(cells: list) -> str:
    return cell(cells, config.NOTICE_TIME_COL)


def sort_key(cells: list):
    """Newest first. The grid's own order is not reliable across grids, so sort
    on the notice timestamp (DD-MM-YYYY HH:MM) when there is one."""
    from datetime import datetime
    raw = time_of(cells)
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    from datetime import datetime as _dt
    return _dt.min


# CDC staff sometimes post dummy notices while testing the ERP
# ("Test Companey", "Please ignore this message. It is for testing").
_TEST_NOTICE_RE = re.compile(
    r"\btest\s*compan|\b(?:please|pls|kindly)\s+ignore\b|\bignore\s+this\b"
    r"|\bfor\s+testing\s+purposes?\b|^\s*test(?:ing)?\s*(?:notice|purpose)?\s*\d*\s*$",
    re.I,
)


def is_test_notice(cells: list) -> bool:
    title = title_of(cells)
    body = re.sub(r"<[^>]+>", " ", cell(cells, config.NOTICE_BODY_COL))
    return bool(_TEST_NOTICE_RE.search(title) or _TEST_NOTICE_RE.search(body[:300]))


def should_alert(cells: list) -> bool:
    """Whether this notice is worth notifying about.

    Excluded notices are still stored and shown in the UI; they simply do not
    generate an email or WhatsApp message.
    """
    if is_test_notice(cells):
        return False

    kind = cell(cells, config.NOTICE_TYPE_COL).lower()
    if kind and any(x in kind for x in config.NOTICE_EXCLUDE_TYPES):
        return False

    if config.NOTICE_EXCLUDE_KEYWORDS:
        haystack = " ".join([
            title_of(cells),
            cell(cells, config.NOTICE_BODY_COL),
            cell(cells, 2),
        ]).lower()
        if any(k in haystack for k in config.NOTICE_EXCLUDE_KEYWORDS):
            return False
    return True


def split_alertable(rows: list) -> tuple:
    """(to_alert, skipped) for a batch of notices."""
    keep, drop = [], []
    for cells in rows:
        (keep if should_alert(cells) else drop).append(cells)
    return keep, drop
