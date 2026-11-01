"""Postgres storage (Neon). Optional: without DATABASE_URL the app keeps using
the JSON files in state/, so a database outage can never stop alerts going out.

Tables
    notices     one row per notice, keyed by content fingerprint. Doubles as the
                "already seen" set (alerted=true) and as the UI's snapshot.
    recipients  email addresses and WhatsApp targets.
    meta        small scalars (seeded flag, last fetch time).
"""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # psycopg not installed -> JSON fallback
    psycopg = None

# Load .env here too: db may be imported before config (which also loads it),
# and reading DATABASE_URL from a not-yet-populated environment silently falls
# back to a local socket instead of Neon.
try:
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "15"))

SCHEMA = """
create table if not exists notices (
    fingerprint     text primary key,
    cells           jsonb       not null,
    title           text,
    notice_time     timestamptz,
    notice_time_raw text,
    first_seen      timestamptz not null default now(),
    alerted         boolean     not null default false
);
create index if not exists notices_time_idx on notices (notice_time desc nulls last);

create table if not exists recipients (
    id         bigserial primary key,
    kind       text not null check (kind in ('email', 'whatsapp')),
    value      text not null,
    created_at timestamptz not null default now(),
    unique (kind, value)
);

create table if not exists company_profiles (
    year       text        not null,
    com_id     text        not null,
    jnf_id     text        not null,
    company    text,
    profile    text,
    ctc        text,
    form_type  text,
    status     text        not null,
    reason     text,
    first_seen timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (year, com_id, jnf_id)
);

alter table company_profiles add column if not exists reminders int not null default 0;
alter table company_profiles add column if not exists attempts int not null default 0;

create table if not exists wa_outbox (
    id         bigserial primary key,
    kind       text        not null check (kind in ('notice', 'text')),
    payload    jsonb       not null,
    created_at timestamptz not null default now(),
    attempts   int         not null default 0,
    last_error text,
    claimed_at timestamptz,
    sent_at    timestamptz
);
create index if not exists wa_outbox_pending_idx on wa_outbox (id) where sent_at is null;

create table if not exists form_tasks (
    id          bigserial primary key,
    url         text        not null unique,
    company     text,
    title       text,
    deadline    text,
    status      text        not null default 'new',
    detail      jsonb       not null default '{}',
    attempts    int         not null default 0,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists meta (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);
"""


def enabled() -> bool:
    return bool(_url()) and psycopg is not None


def connect():
    return psycopg.connect(_url(), connect_timeout=CONNECT_TIMEOUT, row_factory=dict_row)


def init():
    """Create tables if needed. Safe to call repeatedly."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()


def _parse_time(raw: str):
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# --- meta -----------------------------------------------------------------
def get_meta(key: str, default=None):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select value from meta where key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_meta(key: str, value):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into meta (key, value, updated_at) values (%s, %s, now())
                   on conflict (key) do update
                   set value = excluded.value, updated_at = now()""",
                (key, json.dumps(value)),
            )
        conn.commit()


# --- recipients -----------------------------------------------------------
def load_recipients(kind: str) -> list:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select value from recipients where kind = %s order by created_at, id", (kind,)
        )
        return [r["value"] for r in cur.fetchall()]


def save_recipients(emails: list, whatsapp: list):
    """Replace the stored lists. Done in one transaction so a failure halfway
    cannot leave the app with nobody to notify."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from recipients")
            for kind, values in (("email", emails), ("whatsapp", whatsapp)):
                for value in values:
                    cur.execute(
                        """insert into recipients (kind, value) values (%s, %s)
                           on conflict (kind, value) do nothing""",
                        (kind, value),
                    )
        conn.commit()


# --- notices --------------------------------------------------------------
def sync_notices(rows: list, fingerprint, title_of, time_of) -> list:
    """Upsert the board. Returns the rows that were not already stored.

    One round trip to find which fingerprints are known, then a single batched
    insert for the rest. Doing this per-row took ~4 minutes against Neon for 838
    notices; this is two statements regardless of board size.

    Existing rows are left untouched and never re-reported, so a notice edited
    upstream does not alert twice.
    """
    if not rows:
        return []

    pairs = [(fingerprint(cells), cells) for cells in rows]
    by_fp = {fp: cells for fp, cells in pairs}

    # One atomic statement: the INSERT itself decides what is new, so two
    # machines polling the same database cannot both decide the same notice is
    # fresh and alert twice. A SELECT-then-INSERT had exactly that race.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into notices
                       (fingerprint, cells, title, notice_time, notice_time_raw)
                   select * from unnest(
                       %s::text[], %s::jsonb[], %s::text[],
                       %s::timestamptz[], %s::text[])
                   on conflict (fingerprint) do nothing
                   returning fingerprint""",
                (
                    [fp for fp, _ in pairs],
                    [json.dumps(cells) for _, cells in pairs],
                    [title_of(cells) for _, cells in pairs],
                    [_parse_time(time_of(cells)) for _, cells in pairs],
                    [time_of(cells) for _, cells in pairs],
                ),
            )
            inserted = [r["fingerprint"] for r in cur.fetchall()]
        conn.commit()

    return [by_fp[fp] for fp in inserted if fp in by_fp]


def mark_alerted(rows: list, fingerprint):
    if not rows:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update notices set alerted = true where fingerprint = any(%s)",
                ([fingerprint(c) for c in rows],),
            )
        conn.commit()


def unalerted_count() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select count(*) as n from notices where not alerted")
        return cur.fetchone()["n"]


def load_notices(limit: int = 2000) -> list:
    """Stored board, newest first."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """select cells from notices
               order by notice_time desc nulls last, first_seen desc
               limit %s""",
            (limit,),
        )
        return [r["cells"] for r in cur.fetchall()]


def counts() -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """select count(*) as total,
                      count(*) filter (where alerted) as alerted,
                      max(notice_time) as newest
               from notices"""
        )
        return dict(cur.fetchone())


# --- company profiles -----------------------------------------------------
def load_profiles() -> dict:
    """{(year, com_id, jnf_id): row} for every company profile ever seen."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select * from company_profiles")
        return {(r["year"], r["com_id"], r["jnf_id"]): r for r in cur.fetchall()}


def claim_profile(key, company, profile, ctc, form_type, status, reason="") -> bool:
    """Record a profile the first time it is seen. True only for the caller that
    inserted it, so two machines polling together cannot both announce it."""
    year, com_id, jnf_id = key
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into company_profiles
                       (year, com_id, jnf_id, company, profile, ctc, form_type, status, reason)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (year, com_id, jnf_id) do nothing
                   returning 1""",
                (year, com_id, jnf_id, company, profile, ctc, form_type, status, reason),
            )
            won = cur.fetchone() is not None
        conn.commit()
    return won


def release_profile(key):
    """Forget a profile so the next poll announces it again (used when the alert
    could not be sent)."""
    year, com_id, jnf_id = key
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from company_profiles where year=%s and com_id=%s and jnf_id=%s",
                        (year, com_id, jnf_id))
        conn.commit()


def claim_reminder(key, level: int) -> bool:
    """Take the right to send reminder `level` (1 = closing soon, 2 = last hour).
    True for exactly one caller, so two machines never both send it."""
    year, com_id, jnf_id = key
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """update company_profiles set reminders = %s, updated_at = now()
                   where year=%s and com_id=%s and jnf_id=%s and reminders < %s
                   returning 1""",
                (level, year, com_id, jnf_id, level))
            won = cur.fetchone() is not None
        conn.commit()
    return won


def release_reminder(key, level: int):
    """Undo claim_reminder when the mail could not be sent, so it is retried."""
    year, com_id, jnf_id = key
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """update company_profiles set reminders = %s
                   where year=%s and com_id=%s and jnf_id=%s and reminders = %s""",
                (level - 1, year, com_id, jnf_id, level))
        conn.commit()


def set_profile_status(key, status: str, reason: str = ""):
    year, com_id, jnf_id = key
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """update company_profiles set status=%s, reason=%s, updated_at=now()
                   where year=%s and com_id=%s and jnf_id=%s""",
                (status, reason, year, com_id, jnf_id))
        conn.commit()


def claim_apply(key, max_attempts: int) -> int:
    """Atomically take the right to make ONE application attempt on this profile.

    Returns the attempt number (1, 2, ...) for the single caller that wins, or 0.
    Only profiles that are still waiting (eligible / seeded / a failed earlier try)
    can be claimed, and only while attempts remain. A claim left in 'applying' by a
    crashed run becomes claimable again after 10 minutes. The check-and-set is a
    single UPDATE, so two machines polling together can never both apply.
    """
    year, com_id, jnf_id = key
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """update company_profiles
                      set status = 'applying', attempts = attempts + 1, updated_at = now()
                    where year=%s and com_id=%s and jnf_id=%s and attempts < %s
                      and (status in ('eligible', 'seeded', 'apply_failed')
                           or (status = 'applying' and updated_at < now() - interval '10 minutes'))
                returning attempts""",
                (year, com_id, jnf_id, max_attempts))
            row = cur.fetchone()
        conn.commit()
    return int(row["attempts"]) if row else 0


# --- WhatsApp retry queue -------------------------------------------------
# A WhatsApp message that could not be delivered (bridge down, reconnecting, disk
# full...) is parked here and retried on later polls instead of being dropped.
def wa_enqueue(kind: str, payload) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("insert into wa_outbox (kind, payload) values (%s, %s) returning id",
                        (kind, json.dumps(payload)))
            new_id = cur.fetchone()["id"]
        conn.commit()
    return new_id


def wa_claim(limit: int = 10, max_age_hours: int = 48) -> list:
    """Take up to `limit` pending messages, oldest first. Messages older than
    max_age_hours are dropped (a day-old alert is noise, not news). A claim left by
    a crashed run is reusable after 10 minutes; SKIP LOCKED stops two machines
    taking the same message."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""update wa_outbox set sent_at = now(), last_error = 'expired'
                            where sent_at is null and created_at < now() - make_interval(hours => %s)""",
                        (max_age_hours,))
            cur.execute(
                """update wa_outbox set claimed_at = now(), attempts = attempts + 1
                    where id in (select id from wa_outbox
                                  where sent_at is null
                                    and (claimed_at is null or claimed_at < now() - interval '10 minutes')
                                  order by id limit %s for update skip locked)
                returning id, kind, payload, attempts, created_at""", (limit,))
            rows = cur.fetchall()
        conn.commit()
    return sorted(rows, key=lambda r: r["id"])


def wa_done(row_id: int):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("update wa_outbox set sent_at = now(), last_error = null where id = %s", (row_id,))
        conn.commit()


def wa_release(row_id: int, error: str):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("update wa_outbox set claimed_at = null, last_error = %s where id = %s",
                        (error[:300], row_id))
        conn.commit()


def wa_pending() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select count(*) as n from wa_outbox where sent_at is null")
        return cur.fetchone()["n"]


# --- Outside application forms ---------------------------------------------
# One row per form link found in a notice. status: new -> submitted, or asked
# (waiting for your WhatsApp reply), handoff (you fill it), failed.
def form_add(url: str, company: str, title: str, deadline: str) -> int | None:
    """Record a form link once. Returns its id, or None if already known."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""insert into form_tasks (url, company, title, deadline)
                           values (%s, %s, %s, %s) on conflict (url) do nothing returning id""",
                        (url, company, title, deadline))
            row = cur.fetchone()
        conn.commit()
    return row["id"] if row else None


def form_get(task_id: int):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select * from form_tasks where id = %s", (task_id,))
        return cur.fetchone()


def form_claim(task_id: int, from_status: tuple) -> bool:
    """Atomically move a task to 'working' so two runs never submit it twice."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""update form_tasks set status = 'working', attempts = attempts + 1,
                                  updated_at = now()
                            where id = %s and (status = any(%s)
                                  or (status = 'working' and updated_at < now() - interval '15 minutes'))
                        returning id""", (task_id, list(from_status)))
            ok = cur.fetchone() is not None
        conn.commit()
    return ok


def form_set(task_id: int, status: str, detail: dict | None = None):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""update form_tasks set status = %s, updated_at = now(),
                                  detail = case when %s::jsonb is null then detail else detail || %s::jsonb end
                            where id = %s""",
                        (status, json.dumps(detail) if detail is not None else None,
                         json.dumps(detail) if detail is not None else None, task_id))
        conn.commit()


def form_list(statuses: tuple, limit: int = 50) -> list:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select * from form_tasks where status = any(%s) order by id desc limit %s",
                    (list(statuses), limit))
        return cur.fetchall()
