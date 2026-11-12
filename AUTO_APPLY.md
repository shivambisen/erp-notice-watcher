# Auto-Applier

Applies to every company profile on the ERP that you are eligible for, using the
CV you choose, and posts each result to WhatsApp:

```
*Applied with CV2: AMERICAN EXPRESS - Data Science and Analytics*
CTC 30 LPA | PLACEMENT | closes 29-10-2026 10:00 IST (19h left)
Confirmed on the portal.
```

## Turn it on

It is part of the watcher, so finish the [README setup](README.md#setup-about-10-minutes)
first. Then make sure `.env` has:

```ini
DATABASE_URL=postgresql://...   # required: remembers what was applied
AUTO_APPLY=on
AUTO_APPLY_CV=2                  # 1, 2 or 3 - upload that CV on the ERP first
```

Before anything goes live, check what it would do. This is read-only:

```bash
./venv/bin/python watch.py --companies
```

```
3 profile(s) listed

  AMERICAN EXPRESS   Data Science and Analytics  PLACEMENT  eligible   recorded=-   window->29-10-2026 10:00
  GOLDMAN SACHS      Analyst                     PLACEMENT  ineligible recorded=-   window->31-10-2026 23:59   (Company requirement has CGPA cut off greater than yours.)
  ZOMATO             SDE                         PLACEMENT  applied    recorded=applied window->01-11-2026 12:00
```

`eligible` means it will apply on the next check. The other verdicts are
`applied`, `ineligible`, `closed`, `wait` (window not open yet) and `skipped` (one
of your skip rules matched).

If that looks right, it applies on the next scheduled check, or run
`./venv/bin/python watch.py` to check now.

## How it decides

**The portal decides, not this code.** For each listed profile it opens the
profile page the same way you would:

| The page shows | Meaning | What it does |
|---|---|---|
| `Apply with CV1/2/3` buttons | open, eligible, not applied yet | **applies** with `AUTO_APPLY_CV` |
| `Cancel apply` | you already applied | nothing |
| text such as *"CGPA cut off greater than yours"* | not eligible | nothing |
| window closed / no buttons | not open | nothing |

There is no CGPA or department logic to go stale. If the ERP wouldn't let you
click Apply, it won't either.

## Safety

- **It confirms on the portal.** After applying it re-opens the page and only
  reports *Applied* once the ERP itself shows *applied*.
- **It never applies twice.** Every attempt is claimed in the database first, so
  even two machines running at once can't double-apply.
- **It retries, then hands over.** A failed attempt is retried on the next checks
  (`AUTO_APPLY_MAX_ATTEMPTS`, default 3). After that it posts
  *"please apply on the ERP yourself"* to WhatsApp.
- **It only clicks what the page offers.** The request is built from the page's
  own Apply button, and it refuses if that button's roll number or script isn't
  what it expects.
- **You can undo it.** Use **Cancel apply** on the ERP before the window closes.
- **It reminds you about anything it couldn't apply to.** You get a reminder 4
  hours and 1 hour before the CV deadline (`COMPANY_REMINDER_HOURS`).

## Choosing what it applies to

| Setting | Example | Effect |
|---|---|---|
| `AUTO_APPLY_CV` | `2` | CV used for every application |
| `COMPANY_ALERT_SKIP` | `tcs,infosys` | never apply where the company name contains these |
| `COMPANY_ALERT_MIN_CTC` | `12` | skip profiles below this CTC |
| `COMPANY_ALERT_FORM_TYPES` | `PLACEMENT` | only these form types; empty = all |
| `COMPANY_ALERT_WHATSAPP` | `on` | post results and reminders to WhatsApp |
| `AUTO_APPLY` | `off` | stop applying, keep alerts and reminders |

Changes apply from the next check. No restart is needed.

## Troubleshooting

| You see | Fix |
|---|---|
| `Company alerts need the database` in the log | set `DATABASE_URL` |
| *AUTO-APPLY FAILED* | usually the ERP session expired mid-run. It retries next check by itself |
| Nothing applied but `--companies` says eligible | the first run only records what's listed. The next run applies |
| Login fails | run `./configure.sh` again (password or security answers) |

Logs: `tail -f state/watcher.log` (search for `Applied`).

## Outside forms (Google Forms)

Companies often follow up with *"all students who applied must fill this form"*.
For a company you applied to, [forms.py](forms.py) fills that Google Form for you:

1. **It reads the questions** from the form page.
2. **It answers from `profile.txt`**, your own answers, written once (copy
   [profile.example.txt](profile.example.txt)). Role choices follow your
   `Role priority` line. Claude Haiku, running on your own Claude login, only
   decides which line answers which question. **What gets submitted is always
   your text, never something the AI wrote.**
3. **It submits when every required question is answered**, then posts
   *Form submitted* with all the answers to your own WhatsApp chat.
4. **It asks you whatever's missing** in your own chat. Reply
   `F12 1: your answer; 2: b`; letters pick an option. Your answers are added to
   `profile.txt` for next time.
5. **It hands over what it can't do:** file uploads, grids, closed forms, an
   expired Google session. You get the link and the deadline.

One-time setup, all on your Mac:

| Step | Command |
|---|---|
| Your answers | fill `profile.txt` from `profile.example.txt`, then `scp` it to the server's project folder |
| Claude login, no API key | `claude setup-token`, then `./deploy/set-claude-token.sh` |
| Google session (CDC forms record your verified kgpian email) | export cookies with the Chrome extension *Get cookies.txt LOCALLY*, then `./deploy/upload-google-session.sh <file>` |

Read-only test on any form: `./venv/bin/python forms.py --check <form link>`.
Catch up on forms from the last 3 days: `./venv/bin/python forms.py --scan`.

The Google session file lets whoever has it act as your Google account. It is
stored readable only by you, and the upload script deletes your local copy.
If it expires, the filler tells you, and you export it again.

| Setting | Default | |
|---|---|---|
| `FORMS_AUTOFILL` | on | `off` stops the form filler |
| `FORMS_AUTO_SUBMIT` | on | `off` always asks you before submitting |
| `FORMS_ALL_COMPANIES` | off | also fill forms for companies you didn't apply to |
| `PORTAL_REMIND_HOURS` | 3 | how long before a portal deadline the reminder comes |

### Company portals (HirePro, Superset, careers sites)

Portals start with a sign-up, a password and often an OTP, so they are never
filled automatically. When a notice for a company you applied to says
*register on the portal* or *apply on the company portal*, you get a separate
WhatsApp alert with the company, the deadline and the link (or "sent to your
email" when CDC mailed it). A reminder follows a few hours before the deadline.
Reply `P7 done` to stop it. Once you're signed in, Claude in your browser can
fill the rest of the application from `profile.txt`.

## Daily digest of open forms and portals

Every day at **12:00 IST** the notice group gets one message listing every Google
Form and company-portal link from recent notices that is still open, with its
deadline:

```
*Open forms & portals - Thu 12 Nov, 12:00 PM*

*1. Goldman Sachs* - Company portal
Due: Thu 12 Nov, 6:00 PM (in 6h)
https://...
```

It leaves out PPT meeting links, "didn't get the test link?" query forms, CDC's
test notices, anything past its deadline, and Google Forms that no longer accept
responses. A link whose notice gives no deadline is listed for 24 hours.

Preview without sending: `./venv/bin/python digest.py --dry-run`.

| Setting | Default | |
|---|---|---|
| `DIGEST_LOOKBACK_DAYS` | 14 | how far back to read notices |
| `DIGEST_NO_DEADLINE_HOURS` | 24 | how long a link with no deadline is listed |

To change the time, edit `OnCalendar` in `deploy/systemd/erp-digest.timer` (UTC).
