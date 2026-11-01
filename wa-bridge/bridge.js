/*
 * Local WhatsApp bridge for the ERP notice watcher.
 *
 * UNOFFICIAL: this drives a real WhatsApp account, which is against WhatsApp's
 * terms of service and can get the number banned. It is the only way to post
 * into a group -- the official Cloud API has no group support at all. Use a
 * number you can afford to lose access to.
 *
 * Uses Baileys, which speaks WhatsApp's WebSocket protocol directly. The
 * earlier whatsapp-web.js implementation puppeteered WhatsApp Web and broke
 * against the current web build ("Cannot read properties of undefined
 * (reading 'id')" on every send); driving the protocol avoids that entirely
 * and needs no browser at all.
 *
 *   npm install && node bridge.js
 *
 * Scan the QR once; credentials persist in .baileys_auth. Loopback only.
 */
const express = require('express');
const qrcode = require('qrcode-terminal');
const pino = require('pino');
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
} = require('@whiskeysockets/baileys');

// libsignal (inside Baileys) console.logs whole session objects during normal
// ratchet churn -- including privKey/rootKey buffers. That is noise at best and
// key material written to your terminal and logs at worst, so drop those lines.
const util = require('util');
const _log = console.log.bind(console);
const KEY_DUMP = /privKey|rootKey|chainKey|ephemeralKeyPair|registrationId/;
console.log = function (...args) {
  const first = args[0];
  if (typeof first === 'string' &&
      /^(Closing (open )?session|SessionEntry|Session closed|Removing old closed session)/.test(first)) {
    return;
  }
  // any line that would print session key material, whatever its prefix
  if (args.some(function (a) { return typeof a === 'object' && a !== null && KEY_DUMP.test(util.inspect(a, { depth: 3 })); })) {
    return;
  }
  _log(...args);
};

const PORT = Number(process.env.WA_BRIDGE_PORT || 8766);
const AUTH_DIR = '.baileys_auth';
// Link with an 8-digit code instead of a QR: put your number (with country
// code) in pair-phone.txt, start the bridge, read the code from /status or the
// log, and enter it on your phone: WhatsApp > Linked devices > Link a device >
// Link with phone number instead. The file is removed once linked.
const fs = require('fs');
const PAIR_FILE = 'pair-phone.txt';
let pairingCode = null;

let sock = null;
let lastQr = null;
let ready = false;
let me = null;
let lastError = null;
let starting = false;

// Replies YOU type in your own "Message yourself" chat, so the watcher can ask
// you a question (e.g. a form answer) and read the answer. Nothing from any
// other chat is ever kept. Messages this bridge sent itself are skipped.
const INBOX_MAX = 50;
const inbox = [];
const sentIds = new Set();
function rememberSent(sent) {
  if (sent && sent.key && sent.key.id) {
    sentIds.add(sent.key.id);
    if (sentIds.size > 500) sentIds.delete(sentIds.values().next().value);
  }
}
function isSelfChat(jid) {
  if (!jid || !sock || !sock.user) return false;
  const num = function (j) { return String(j).split('@')[0].split(':')[0]; };
  return num(jid) === num(sock.user.id) || (sock.user.lid && num(jid) === num(sock.user.lid));
}

const logger = pino({ level: process.env.WA_LOG_LEVEL || 'silent' });

async function start() {
  if (starting) return;
  starting = true;
  try {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();
    console.log('WhatsApp protocol version: ' + version.join('.'));

    sock = makeWASocket({ version, auth: state, logger, syncFullHistory: false });
    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('messages.upsert', function (ev) {
      for (const m of (ev && ev.messages) || []) {
        if (!m.key || !m.key.fromMe || sentIds.has(m.key.id) || !isSelfChat(m.key.remoteJid)) continue;
        const msg = m.message || {};
        const text = msg.conversation || (msg.extendedTextMessage && msg.extendedTextMessage.text) || '';
        if (!text) continue;
        inbox.push({ id: m.key.id, text: text, ts: Number(m.messageTimestamp) || Math.floor(Date.now() / 1000) });
        if (inbox.length > INBOX_MAX) inbox.shift();
      }
    });

    let pairRequested = false;
    sock.ev.on('connection.update', async (update) => {
      const connection = update.connection;
      const qr = update.qr;
      const pairPhone = fs.existsSync(PAIR_FILE) ? fs.readFileSync(PAIR_FILE, 'utf8').replace(/\D/g, '') : '';

      if (qr && pairPhone && !state.creds.registered) {
        lastQr = qr;
        ready = false;
        if (!pairRequested) {
          pairRequested = true;
          try {
            pairingCode = await sock.requestPairingCode(pairPhone);
            console.log('PAIRING CODE: ' + pairingCode + '  (WhatsApp > Linked devices > Link with phone number)');
          } catch (e) {
            console.error('Could not get a pairing code: ' + (e && e.message ? e.message : e));
          }
        }
      } else if (qr) {
        lastQr = qr;
        ready = false;
        console.log('\nScan with WhatsApp > Linked devices > Link a device:\n');
        qrcode.generate(qr, { small: true });
        console.log('\n(also at http://127.0.0.1:' + PORT + '/qr)\n');
      }

      if (connection === 'open') {
        ready = true;
        lastQr = null;
        lastError = null;
        me = sock.user && sock.user.id ? sock.user.id : null;
        pairingCode = null;
        if (fs.existsSync(PAIR_FILE)) fs.unlinkSync(PAIR_FILE);
        console.log('WhatsApp bridge ready' + (me ? ' as ' + me : ''));
      }

      if (connection === 'close') {
        ready = false;
        const err = update.lastDisconnect && update.lastDisconnect.error;
        const code = err && err.output ? err.output.statusCode : null;
        const loggedOut = code === DisconnectReason.loggedOut;
        lastError = 'closed (' + (code || 'unknown') + ')';
        console.log('Connection closed: ' + lastError +
          (loggedOut
            ? ' - logged out, delete ' + AUTH_DIR + ' and re-scan'
            : ' - reconnecting...'));
        starting = false;
        // Anything other than an explicit logout is transient; reconnect.
        if (!loggedOut) setTimeout(start, 3000);
      }
    });
  } catch (e) {
    starting = false;
    lastError = String(e && e.message ? e.message : e);
    console.error('Could not start: ' + lastError);
    setTimeout(start, 5000);
  }
}

const app = express();
// Notice PDFs arrive base64-encoded, so allow a larger body than the default 100kb.
app.use(express.json({ limit: '60mb' }));

app.get('/status', function (req, res) {
  res.json({ ready: ready, hasQr: Boolean(lastQr), me: me, error: lastError, pairingCode: pairingCode });
});

app.get('/qr', function (req, res) {
  if (!lastQr) return res.status(404).json({ error: ready ? 'already linked' : 'no QR yet' });
  res.json({ qr: lastQr });
});

// Groups this account belongs to, so the right one can be picked in the UI.
app.get('/chats', async function (req, res) {
  if (!ready) return res.status(503).json({ error: 'not linked yet' });
  try {
    const all = await sock.groupFetchAllParticipating();
    const groups = Object.values(all || {}).map(function (g) {
      return {
        id: g.id,
        name: g.subject || g.id,
        participants: (g.participants || []).length,
      };
    });
    res.json({ groups: groups, contacts: [] });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

app.post('/send', async function (req, res) {
  if (!ready) return res.status(503).json({ error: 'not linked yet - scan the QR' });
  const to = req.body && req.body.to;
  const text = req.body && req.body.text;
  if (!to || !text) return res.status(400).json({ error: 'to and text are required' });
  try {
    const sent = await sock.sendMessage(to, { text: text });
    rememberSent(sent);
    res.json({ ok: true, id: sent && sent.key ? sent.key.id : null });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

// Send a document (a notice PDF) with an optional caption.
app.post('/send-file', async function (req, res) {
  if (!ready) return res.status(503).json({ error: 'not linked yet - scan the QR' });
  const to = req.body && req.body.to;
  const b64 = req.body && req.body.content_b64;
  if (!to || !b64) return res.status(400).json({ error: 'to and content_b64 are required' });
  const fileName = (req.body && req.body.filename) || 'notice.pdf';
  const mimetype = (req.body && req.body.mimetype) || 'application/pdf';
  const caption = (req.body && req.body.caption) || undefined;
  try {
    const buffer = Buffer.from(b64, 'base64');
    const sent = await sock.sendMessage(to, {
      document: buffer,
      fileName: fileName,
      mimetype: mimetype,
      caption: caption,
    });
    rememberSent(sent);
    res.json({ ok: true, id: sent && sent.key ? sent.key.id : null, bytes: buffer.length });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

// Your own-chat replies newer than ?after=<unix seconds>.
app.get('/inbox', function (req, res) {
  const after = Number(req.query.after || 0);
  res.json({ messages: inbox.filter(function (m) { return m.ts > after; }) });
});

app.listen(PORT, '127.0.0.1', function () {
  console.log('Bridge listening on http://127.0.0.1:' + PORT);
  start();
});
