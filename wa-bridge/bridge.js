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
const _log = console.log.bind(console);
console.log = function (...args) {
  const first = args[0];
  if (typeof first === 'string' &&
      /^(Closing (open )?session|SessionEntry|Session closed|Removing old closed session)/.test(first)) {
    return;
  }
  _log(...args);
};

const PORT = Number(process.env.WA_BRIDGE_PORT || 8766);
const AUTH_DIR = '.baileys_auth';

let sock = null;
let lastQr = null;
let ready = false;
let me = null;
let lastError = null;
let starting = false;

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

    sock.ev.on('connection.update', (update) => {
      const connection = update.connection;
      const qr = update.qr;

      if (qr) {
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
  res.json({ ready: ready, hasQr: Boolean(lastQr), me: me, error: lastError });
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
    res.json({ ok: true, id: sent && sent.key ? sent.key.id : null, bytes: buffer.length });
  } catch (e) {
    res.status(500).json({ error: String(e && e.message ? e.message : e) });
  }
});

app.listen(PORT, '127.0.0.1', function () {
  console.log('Bridge listening on http://127.0.0.1:' + PORT);
  start();
});
