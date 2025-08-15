const path = require('path');
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');
const cheerio = require('cheerio');

// Safe AbortController for Node 18
let AbortController = global.AbortController;
try {
  if (!AbortController) {
    AbortController = require('abort-controller');
  }
} catch (_) {
  // last resort: dummy that never aborts (won't crash)
  AbortController = class { constructor(){ this.signal = undefined; } };
}

const app = express();
app.set('trust proxy', true);
app.use(cors());

const path = require('path');
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');          // v2
const cheerio = require('cheerio');

// Safe AbortController for Node 18
let AbortController = global.AbortController;
try {
  if (!AbortController) {
    AbortController = require('abort-controller'); // npm i abort-controller
  }
} catch (_) {
  // last resort: dummy that never aborts (won't crash)
  AbortController = class { constructor(){ this.signal = undefined; } };
}

pp.get('/getMidi', async (req, res) => {
  const midiUrl = req.query.url;
  if (!midiUrl) return res.status(400).send("Missing 'url' parameter");

  // Validate URL & scheme
  let u;
  try { u = new URL(midiUrl); } catch { return res.status(400).send("Bad 'url'"); }
  if (!/^https?:$/.test(u.protocol)) return res.status(400).send("Only http(s) URLs allowed");

  try {
    const controller = new AbortController();
    const to = setTimeout(() => {
      try { controller.abort(); } catch (_) {}
    }, 15000);

    const r = await fetch(midiUrl, {
      redirect: 'follow',
      signal: controller.signal,
      headers: {
        'User-Agent': 'awertt-midi-proxy/1.0 (+https://awertt.org)'
      }
    });

    clearTimeout(to);

    if (!r.ok) {
      return res.status(502).send(`Fetch failed: ${r.status} ${r.statusText}`);
    }

    const buf = Buffer.from(await r.arrayBuffer());
    res.type('text/plain; charset=utf-8').send(buf.toString('hex'));
  } catch (err) {
    console.error('getMidi error:', err && err.message ? err.message : err);
    res.status(500).send('Error fetching MIDI: ' + (err && err.message ? err.message : String(err)));
  }
});

// --- Health check ---
app.get('/healthz', (_, res) => res.status(200).send('OK'));

// --- Static website from /public (index.html, etc.) ---
const PUBLIC_DIR = path.join(__dirname, 'public');
app.use(express.static(PUBLIC_DIR, { extensions: ['html'] }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`✅ awertt-app listening on :${PORT}`));
