# ERP Notice Watcher + Auto-Applier (IIT Kharagpur)

Watches the IIT Kharagpur ERP for you and acts on it:

- **New CDC notices** get posted to a WhatsApp group (or a person), one clean
  message per notice, with the PDF attached if the notice has one.
- **New companies** get applied to automatically with the CV you choose, and only
  where the portal says you are eligible. Each application is confirmed on the
  portal, then posted to WhatsApp. See **[AUTO_APPLY.md](AUTO_APPLY.md)**.
- **Login is handled for you.** It answers your security question and reads the
  login OTP from your mailbox, so it never needs you.

It runs on a Mac and checks every 30 minutes. A Linux server works too (see
[docs/DETAILS.md](docs/DETAILS.md)).

## What you need

| | Why | Cost |
|---|---|---|
| macOS with Python 3.10+ and Node.js 20+ (`brew install python node`) | runs the watcher and the WhatsApp bridge | free |
| Your ERP roll number, password and the 3 security answers | to log in | — |
| A Google **app password** for the mailbox your ERP OTP goes to | to read the OTP (read-only) | free |
| A Postgres database, e.g. [Neon](https://neon.tech) | remembers what was sent and applied | free tier |
| A WhatsApp account | sends the messages | — |

## Setup (about 10 minutes)

```bash
git clone https://github.com/shivambisen/erp-notice-watcher.git
```

```bash
cd erp-notice-watcher && ./setup.sh
```

```bash
./configure.sh
```

`setup.sh` installs everything and creates `.env`. `configure.sh` asks for your
details (passwords are typed hidden) and saves them into `.env`.

**Link WhatsApp.** Start the bridge, then scan the QR it prints from your phone
(WhatsApp → Settings → Linked devices → Link a device):

```bash
cd wa-bridge && ./start.sh
```

Once it prints `WhatsApp bridge ready`, press Ctrl+C. Then install it as a background service:

```bash
./install-launchd.sh
```

**Pick the WhatsApp group.** Skip this if you already gave a group id in
`configure.sh`.

```bash
cd .. && ./venv/bin/python serve.py
```

Open <http://127.0.0.1:8765>, go to **Settings → Find my groups**, click your
group, then **Save**.

**Test it.** This logs in once (watch for the OTP being read automatically) and
lists every open company with the portal's verdict. It changes nothing:

```bash
./venv/bin/python watch.py --companies
```

**Turn it on.** This checks every 30 minutes, in the background, and survives
reboots:

```bash
./install-launchd.sh 1800
```

On the first run it only records what is already on the notice board, so you
won't get a flood of old notices. Companies already open are applied to on the
next run.

## Day to day

```bash
tail -f state/watcher.log
```

That shows what it is doing. The other useful commands:

| Command | Does |
|---|---|
| `./venv/bin/python watch.py --check-config` | prints every setting (secrets masked) and flags mistakes. Run this first if anything is off |
| `./venv/bin/python watch.py --companies` | dry run: what it would apply to, and why |
| `./venv/bin/python watch.py` | one check right now |
| `./venv/bin/python serve.py` | web UI: notice board, recipients, status (localhost only) |
| `launchctl bootout gui/$UID/com.shivam.erpnoticewatcher` | stop the schedule |

## Settings you might change

All of these live in `.env`. The full list with comments is in
[.env.example](.env.example).

| Setting | Default | |
|---|---|---|
| `AUTO_APPLY` | `on` | `off` = only alert, never apply |
| `AUTO_APPLY_CV` | `2` | which CV to apply with |
| `COMPANY_ALERT_SKIP` | — | never apply to these companies (comma separated name words) |
| `COMPANY_ALERT_MIN_CTC` | `0` | skip profiles below this CTC |
| `COMPANY_ALERT_FORM_TYPES` | all | e.g. `PLACEMENT` to ignore internships |
| `NOTICE_EXCLUDE_TYPES` | — | e.g. `INTERNSHIP` to not forward those notices |
| `WHATSAPP_TO` | — | group id (`…@g.us`) or `+91…` number, comma separated |
| `MAIL_ENABLED` | `off` | `on` also sends email alerts (needs `MAIL_TO`) |

## Things to know

- **The WhatsApp bridge is unofficial.** WhatsApp's official API can't post to
  groups, so the bridge drives a linked device the same way WhatsApp Web does.
  That is against WhatsApp's terms, and the number could in theory be banned.
  Use a number you can afford to lose.
- **Nothing is lost if WhatsApp is down.** Messages that fail are queued and
  retried on every check for 48 hours.
- **Your secrets stay on your machine.** `.env` is `chmod 600` and gitignored,
  and the web UI only listens on localhost.
- **If login starts failing,** you probably changed your ERP password, or the
  ERP asked a security question you didn't map. Run `./configure.sh` again.

How it all works under the hood: [docs/DETAILS.md](docs/DETAILS.md).
