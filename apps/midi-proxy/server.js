const path = require('path');
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');        // using v2 for require()

const app = express();
app.set('trust proxy', true);
app.use(cors());

// --- API: /getMidi?url=<MIDI_URL> ---
app.get('/getMidi', async (req, res) => {
  const midiUrl = req.query.url;
  if (!midiUrl) return res.status(400).send("Missing 'url' parameter");

  try {
    // follow redirects; small timeout so bad hosts don't hang
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 15000);

    const r = await fetch(midiUrl, { redirect: 'follow', signal: controller.signal });
    clearTimeout(t);

    if (!r.ok) return res.status(502).send(`Fetch failed: ${r.status} ${r.statusText}`);

    const buf = Buffer.from(await r.arrayBuffer());
    // Return plain text hex (what your Roblox script expects)
    res.type('text/plain; charset=utf-8').send(buf.toString('hex'));
  } catch (err) {
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
