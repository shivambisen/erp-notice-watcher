# ERP Notice Watcher — detailed notes

The quick start is in the main [README](../README.md); auto-apply has its own
[guide](../AUTO_APPLY.md). This file is the long-form reference.

Polls the IITKGP ERP Training & Placement notice board and emails you whenever a
new notice appears. When the ERP session dies it logs back in on its own —
including reading the emailed OTP out of your mailbox over IMAP.

## How it stays logged in

The expensive part of this is the login, because it needs an OTP. So the watcher
avoids it:

1. Every poll first tries the notice board with the **cached cookies** in
   `state/cookies.txt`.
2. Only when that comes back as a redirect to the login page does it run a full
   login: security question → `getEmilOTP.htm` → wait for the OTP mail → `auth.htm`
   → re-enter the Training & Placement module.
3. Polling every 10 minutes also keeps the ERP session warm, so in practice a real
   OTP login happens rarely rather than on every check.

If it logged in every poll you'd get an OTP mail every 10 minutes. It doesn't.

## Setup

```bash
./setup.sh
```

Then fill in the mail settings:

```bash
./configure.sh
```

It prompts for the three missing values and writes them into `.env` (password
input is hidden, so it never reaches your terminal or shell history). To edit by
hand instead, these are the blanks:

| Setting | What to put |
|---|---|
| `IMAP_USER` | the mailbox the ERP sends your OTP to |
| `IMAP_PASSWORD` | an **app password**, not your normal password |
| `MAIL_TO` | where you want notice alerts delivered |

For a Google account (`@kgpian.iitkgp.ac.in` or Gmail), the app password comes
from <https://myaccount.google.com/apppasswords> — 2-Step Verification has to be
on first. If the OTP lands somewhere other than Gmail, change `IMAP_HOST`.

### Verify, in order

```bash
./venv/bin/python watch.py --test-mail
```

Confirms SMTP works. Then:

```bash
./venv/bin/python watch.py --dump
```

This is the first real login — it will send an OTP to your mailbox, read it back
automatically, and print the notice board as numbered fields:

```
--- row 1 ---
  [1] 1
  [2] Shortlist for Goldman Sachs released
  ...
```

The watcher polls **grid 54** on `Notice.jsp` — the CDC Notice Board, ~838
notices. (Grid 37 on `TPStudent.jsp` is the company/job listing and is usually
empty; `JQ_QUERY_ID` switches between them.)

Grid 54 returns 10 columns, mapped in `.env`:

```
0 ID   1 Type   2 Subject   3 Company   4 Notice
5 Ref  6 Notice Time        7 Download  8 Upload   9 Deactivate
```

`NOTICE_TITLE_COL=3` makes the company the heading, `NOTICE_TIME_COL=6` drives
newest-first sorting, and `NOTICE_HIDE_COLS=0,5,8,9` drops the noise columns
from alerts and the UI. Ordering is computed from the timestamp rather than the
grid's own row order, because the two grids disagree — 54 returns newest-first,
37 oldest-first.

The old `NOTICE_COLUMNS` for grid 37 was:

```
Company, Details, PPT, Role, Description, CTC, Currency, More Info,
Application Status, Resume Upload Start, Resume Upload End,
Interview/Selection, Contract, Link
```

Grid `37` is the placement **company/job board** — new companies, PPT dates,
application windows — not a free-text announcement feed.

### Managing who gets the alerts

```bash
./venv/bin/python serve.py
```

Opens a small page at <http://127.0.0.1:8765> with three sections:

- **Recipients** — add/remove addresses, send a test mail
- **Notice board** — paginated list, 20 per page, newest first; click a notice
  to read it in the pane beside the list (on a narrow window the reader replaces
  the list, with a Back button)
- **Watcher status** — last run, session cached, whether the job is loaded

The board is rendered from `state/last_notices.json`, the snapshot the watcher
saves on every poll, so opening the page never touches the ERP. **Refresh**
re-fetches using the stored session. If that session has expired the page says
so and does nothing further — only the explicit **Log in now** button may start
an OTP login, so browsing the page can never surprise you with an OTP mail.

It binds to loopback only and writes `state/recipients.json` — it never reads or
writes `.env`, so no password passes through the web layer. The watcher reads
that file on each run, so changes apply from the next check with no restart.
`MAIL_TO` in `.env` stays the fallback if the file is missing or unreadable.

### WhatsApp alerts (optional)

Three providers: the local `bridge` (unofficial, the only one that can post to
groups), or the official Meta / Twilio APIs.

**The constraint to understand first:** WhatsApp does not allow arbitrary
business-initiated messages. You may send free-form text only inside a 24-hour
window that the recipient opened by messaging you first. A notice alert can
arrive at any hour, so it needs a **pre-approved message template** — a Meta
review step, typically a day or so, that no code can skip.

#### WhatsApp groups via the local bridge (unofficial)

**The official WhatsApp API cannot post to groups — at all.** Group messaging is
not part of the Business platform, so no amount of Meta access enables it. The
only way is to automate a real logged-in account, which is what `wa-bridge/`
does using Baileys.

**This breaks WhatsApp's terms of service and the linked number can be banned.**
That is a permanent loss of that WhatsApp account, not a warning. Use a number
you can afford to lose.

```bash
cd wa-bridge && ./start.sh
```

Scan the QR it prints (WhatsApp → Linked devices → Link a device). The session
is saved in `wa-bridge/.baileys_auth`, so this is a one-time step; the bridge just
needs to be running whenever a notice fires. Then in the UI's Settings tab press
**Find my groups**, click your group to add it, and Save.

Set `WHATSAPP_PROVIDER=bridge` in `.env` (already set). Targets can be phone
numbers (`+919876543210`) or group ids (`…@g.us`) in the same list.

The bridge uses **Baileys**, which speaks WhatsApp's WebSocket protocol
directly — no browser, no Chromium. An earlier attempt with `whatsapp-web.js`
puppeteered WhatsApp Web and failed on every send with "Cannot read properties
of undefined (reading 'id')", because that library lags behind WhatsApp Web's
current build. Driving the protocol sidesteps that whole class of breakage.

It listens on loopback only, reconnects automatically on a dropped connection,
and only asks for a new QR if you actually log the device out.

#### Seeing a real message today (no template approval)

Template approval is only needed for messages sent *outside* a 24-hour window.
If you open that window yourself, free-form text works immediately — which is
enough to watch a real message arrive. Fastest route, about five minutes:

1. Sign up at twilio.com (free trial), then Messaging → Try it out →
   **Send a WhatsApp message**. It shows a sandbox number and a join phrase.
2. From your own WhatsApp, send `join <your-phrase>` to that number. That opens
   the 24-hour window.
3. In `.env`: `WHATSAPP_PROVIDER=twilio`, plus `TWILIO_SID`, `TWILIO_TOKEN`, and
   `TWILIO_FROM` set to the sandbox number.
4. In the UI's Settings tab add your own number, Save, then **Send on WhatsApp**.

The Meta route works the same way — create a Business app, add the WhatsApp
product, verify your number as a test recipient, then message the test number
once to open the window. Meta's instantly-available `hello_world` template takes
no variables, so pair it with `WHATSAPP_TEMPLATE_VARS=0`.

For alerts that arrive at 3am — i.e. real use — you still need an approved
template, because the 24-hour window will have closed.

#### Full setup

Meta WhatsApp Cloud API (free tier):

1. developers.facebook.com → create a Business app → add the WhatsApp product.
2. Copy the temporary token and Phone number ID into `WHATSAPP_TOKEN` /
   `WHATSAPP_PHONE_ID` (swap in a permanent System User token before relying
   on it — the temporary one expires in 24 hours).
3. Create a message template with a body containing one `{{1}}` variable, e.g.
   `New ERP placement notice: {{1}}`. Once approved, put its name in
   `WHATSAPP_TEMPLATE`.
4. Set `WHATSAPP_PROVIDER=meta`, then add numbers in the web UI's Settings tab
   in international format (`+919876543210`).

For Twilio instead, set `WHATSAPP_PROVIDER=twilio` with `TWILIO_SID`,
`TWILIO_TOKEN` and `TWILIO_FROM`. Twilio's sandbox is quick for testing but
each recipient must join the sandbox first.

WhatsApp is best-effort: if the provider errors, the email still goes out and
the notice is still marked as seen, so an outage can't cause a duplicate storm
later. "Send test mail" also fires a WhatsApp test when configured.

**Nothing is dropped while the bridge is down.** The bridge periodically loses
and re-establishes its WhatsApp connection (`closed (408) - reconnecting` in
`wa-bridge/logs/bridge.out.log`), and it crashes outright if the disk is full
(`ENOSPC` in `bridge.err.log`). So before sending, the watcher waits up to 60s for
the bridge to be connected. Each notice is then delivered on its own -- one failure
never abandons the rest of a batch -- and any that cannot be delivered are parked
in the `wa_outbox` table and retried at the start of every later poll (oldest
first, up to 10 per poll, and dropped after 48h because a day-old alert is noise).
The same queue carries the "Applied with CV2" and company/deadline posts. Look for
`queued for retry` and `WhatsApp retry: N delivered` in `state/watcher.log`.

### Filtering what alerts

`NOTICE_EXCLUDE_TYPES` lists notice types that are stored and shown in the UI
but never emailed or messaged. It matches the Type column case-insensitively:

```
NOTICE_EXCLUDE_TYPES=INTERNSHIP
```

On the current board that is 736 of 848 notices, so only the ~112 PLACEMENT ones
alert. `NOTICE_EXCLUDE_KEYWORDS` is an optional extra net matched against the
title, subject and body. A notice with no type set always alerts, so a new
category cannot be silently swallowed.

### Company watcher and auto-apply

Every poll also reads the company list (grid 37, owned by `TPStudent.jsp`) and
each profile's own page. **The portal decides eligibility** and prints its
verdict there: `Apply with CV1/2/3` buttons mean open + eligible + not applied;
`Cancel apply` means you already applied; plain text such as *"Company
requirement has CGPA cut off greater than yours."* means you are not eligible.
There is no CGPA or department logic here to keep in sync.

**`AUTO_APPLY=on` applies for you.** For every listed profile the portal is
currently offering you the button for -- any number, no cap -- it submits an
application with `AUTO_APPLY_CV` (default 2), then re-reads the profile page and
only reports success once the **portal itself shows it as applied**. The result
goes to your email and the WhatsApp group. It never applies unless the portal is
offering the button at that moment, and the request is built from that page's own
button (the code refuses if the page's script or roll number is not what it
expects). Undo any application with "Cancel apply" on the ERP before the window
closes.

Safety properties worth knowing:

- Every attempt is **claimed in the database first** (one atomic update), so two
  machines polling together cannot both apply.
- Success means "the portal now shows applied", not "the request returned 200".
  The portal answers an apply with a redirect back to the profile page; that is
  a normal reply, not an error.
- A failed attempt is retried on later polls, at most `AUTO_APPLY_MAX_ATTEMPTS`
  (3) times, with a failure alert each time and a final "apply it yourself".
- If a run dies mid-apply, the next poll re-reads the page: already applied is
  recorded and reported, never applied twice.
- A wrong "failed" is corrected automatically once the portal shows applied.
- Profiles listed before the watcher was first set up are applied to on the poll
  after it starts (the first run only records them).
- With no new eligible profile, a reminder is sent 4h and 1h before a window
  closes if you still have not applied (only possible when auto-apply is off or
  has run out of attempts).

`python3 watch.py --companies` is a read-only dry run: each listed profile, the
portal's verdict, and what the watcher has recorded. Skip rules
(`COMPANY_ALERT_SKIP`, form types, min CTC) apply to auto-apply too.

| Setting | Default | Meaning |
|---|---|---|
| `AUTO_APPLY` | off | `on` submits applications for you  |
| `AUTO_APPLY_CV` | 2 | which CV to apply with |
| `AUTO_APPLY_MAX_ATTEMPTS` | 3 | automatic tries per profile before it tells you to apply yourself |
| `COMPANY_ALERTS` | on | `off` disables the company check |
| `COMPANY_ALERT_FORM_TYPES` | (all) | only alert for these form types; empty means every type |
| `COMPANY_REMINDER_HOURS` | 4 | an eligible profile you have not applied to gets a reminder this many hours before its CV window closes, and again in the last hour (`0` = off) |
| `COMPANY_ALERT_WHATSAPP` | on | also post the alerts and reminders to the WhatsApp targets (the CDC group); best effort, email is unaffected |
| `COMPANY_ALERT_SKIP` | - | skip companies whose name contains these words |
| `COMPANY_ALERT_MIN_CTC` | 0 | skip profiles below this CTC |
| `COMPANY_ALERT_INELIGIBLE` | 0 | also mention profiles you are not eligible for |
| `APPLY_NOTIFY_TO` | `IMAP_USER` | who receives these emails (owner only; the WhatsApp post goes to the group) |

State is in the `company_profiles` table; a profile is announced once even if two
machines poll together, and a failed mail un-claims it so the alert is retried.

### Notice attachments

Notices that carry a file (170 of ~861 on the current board) are delivered with
it: the PDF is attached to the email and sent to WhatsApp as a document with the
notice text as its caption.

The Download column is a JavaScript call, not a link -- `TPNotice("2026-2027","923")`
-- and the file lives at `AdmFilePDF.htm?type=NOTICE&year=<year>&id=<id>`. Those
ids are parsed by `parse_attachments()`, kept deliberately **separate** from
`parse_rows()`: the parsed cells feed the fingerprint each notice is deduplicated
by, so folding attachment data into them would make all stored notices look new
and re-alert the entire board.

A missing or oversized file (>15 MB) never blocks the notice -- it goes out as
text. Each file is downloaded once per poll and reused for both channels.
WhatsApp documents require the local bridge; the official providers would need a
publicly reachable media URL.

### One alert per notice

Each new notice is its own email and its own WhatsApp message, sent oldest
first so the newest ends up as the most recent message. Two settings guard
against a backlog turning into a flood:

| Setting | Default | Effect |
|---|---|---|
| `ALERT_MAX_INDIVIDUAL` | 15 | above this, one poll's notices collapse into a single digest |
| `ALERT_SEND_GAP` | 2.5 | seconds between WhatsApp sends; bursts look like spam and risk a ban |

The cap matters: when the board was stuck for two days it caught up all at once.
Without it that would have been dozens of separate messages in one minute.

### Run it on a schedule

```bash
./install-launchd.sh 1800
```

Every 1800 seconds (30 minutes). The **first scheduled run records the notices already on the
board without mailing them**, so you only hear about genuinely new ones. Set
`SEED_SILENTLY=0` first if you'd rather get the current board once up front.

## Day-to-day

```bash
tail -f state/watcher.log                              # what it's doing
launchctl kickstart -k gui/$UID/com.shivam.erpnoticewatcher   # check right now
launchctl list | grep erpnotice                        # is it loaded
launchctl bootout gui/$UID/com.shivam.erpnoticewatcher # stop it
```

`watch.py --check-config` prints every setting with secrets masked and flags
anything malformed. Run it first whenever something misbehaves.

Other modes: `watch.py --loop 600` (foreground polling, no launchd),
`watch.py --login` (force a fresh OTP login), `watch.py -v` (debug logging).

If a poll fails, you get **one** warning mail and then a 6-hour cooldown, so a
weekend ERP outage can't flood your inbox.

## Things that will eventually break it

- **Your security question rotates.** The ERP picks one of your registered
  questions at random. If only one answer is configured and it asks a
  different one, the login fails. Add all three to `ERP_SECURITY_ANSWERS` in
  `.env` as `{"question substring": "answer"}` — matching is case-insensitive.
- **Password change.** Update `ERP_PASSWORD` in `.env`.
- **The ERP changes its grid.** `JQ_QUERY_ID=54` is the notice-board query id. If
  the board is rebuilt, re-copy the request from DevTools and update it.

### Gotchas worth knowing

- **App passwords must not keep their spaces.** Google displays them as
  `abcd efgh ijkl mnop`; the spaces are formatting, and Gmail's IMAP rejects them.
  `config.py` strips them automatically when the result is exactly the 16-char
  app-password shape, and leaves real passwords containing spaces alone.
- **An empty board is normal.** The grid returns `<rows><page>1</page></rows>`
  when nothing is listed. The watcher seeds itself on an empty board so the first
  notice that ever appears is treated as new and mailed, rather than being
  swallowed as part of the initial snapshot.
- **OTP freshness uses IMAP `UIDNEXT`, not timestamps.** Gmail omits
  `INTERNALDATE` when it is fetched alongside `RFC822`, so a timestamp check
  silently does nothing and a previous run's already-consumed OTP gets replayed
  (the ERP then reports "Email OTP mismatch" while every other step looks fine).
  The mailbox's next-UID is recorded before the OTP is requested, and only mail
  at or above that UID is accepted.
- **`CERTIFICATE_VERIFY_FAILED` on mail.** The python.org macOS builds ship an
  empty CA trust store, so `smtplib`/`imaplib` cannot verify anything until you
  run `Install Certificates.command`. This project avoids that entirely:
  `config.ssl_context()` loads the certifi bundle explicitly, the same way
  `requests` does. Nothing to run by hand.
- **`ERP_SECURITY_ANSWERS` is optional and never fatal.** A malformed value logs a
  warning and falls back to `ERP_SECURITY_ANSWER_DEFAULT` rather than stopping the
  tool. It accepts JSON, single-quoted dicts, or the plain
  `question = answer; other question = other answer` form.

## Storage

By default everything lives in JSON files under `state/`. Setting `DATABASE_URL`
moves it to Postgres instead:

| Table | Holds |
|---|---|
| `notices` | one row per notice, keyed by a content fingerprint; `alerted` marks what has already been sent |
| `recipients` | email addresses and WhatsApp targets |
| `meta` | the `seeded` flag and other scalars |

The JSON files are still written as a mirror, and **every database call falls
back to them on failure** — a Neon outage degrades to local state rather than
stopping alerts. New-notice detection is two statements regardless of board
size (fetch known fingerprints, batch-insert the rest); doing it per row took
~4 minutes for 838 notices against a remote database, versus ~14 seconds now.

## Security notes

- `.env` is `chmod 600` and gitignored; `state/cookies.txt` (a live ERP session)
  is written `600` too. The whole directory is `chmod 700`.
- Use an app password for mail, never your account password — an app password is
  scoped to mail and revocable on its own.
