const path = require('path');
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');           // v2.x for require()
const cheerio = require('cheerio');

// AbortController polyfill for Node 18
let AbortController = global.AbortController;
try {
  if (!AbortController) AbortController = require('abort-controller');
} catch {
  AbortController = class { constructor(){ this.signal = undefined; } };
}

const app = express();
app.set('trust proxy', true);
app.use(cors());

// ---------- /getMidi : fetch a remote .mid and return hex ----------
app.get('/getMidi', async (req, res) => {
  const midiUrl = req.query.url;
  if (!midiUrl) return res.status(400).send("Missing 'url' parameter");

  // Validate URL & scheme
  let u;
  try { u = new URL(midiUrl); } catch { return res.status(400).send("Bad 'url'"); }
  if (!/^https?:$/.test(u.protocol)) return res.status(400).send("Only http(s) URLs allowed");

  // Headers (BitMidi tends to like a Referer)
  const headers = {
    'User-Agent': 'awertt-midi-proxy/1.0 (+https://awertt.org)',
    'Accept': '*/*'
  };
  if (u.hostname.endsWith('bitmidi.com')) headers['Referer'] = 'https://bitmidi.com/';

  try {
    const controller = new AbortController();
    const to = setTimeout(() => { try { controller.abort(); } catch {} }, 15000);

    const r = await fetch(midiUrl, {
      redirect: 'follow',
      signal: controller.signal,
      headers
    });

    clearTimeout(to);

    if (!r.ok) {
      console.error(`[getMidi] Upstream not ok ${r.status} ${r.statusText} for ${midiUrl}`);
      return res.status(502).send(`Fetch failed: ${r.status} ${r.statusText}`);
    }

    const buf = Buffer.from(await r.arrayBuffer());
    res.type('text/plain; charset=utf-8').send(buf.toString('hex'));
  } catch (err) {
    console.error('getMidi error:', err?.message || err);
    res.status(500).send('Error fetching MIDI: ' + (err?.message || String(err)));
  }
});

// ---------- /bitmidi/search : scrape BitMidi search → [{title, pageUrl, downloadUrl, kb}] ----------
app.get('/bitmidi/search', async (req, res) => {
  const q = (req.query.q || '').trim();
  const limit = Math.min(parseInt(req.query.limit || '10', 10) || 10, 25);
  if (!q) return res.status(400).json({ error: "Missing 'q' parameter" });

  const fetchOpts = {
    headers: { 'User-Agent': 'awertt-midi-proxy/1.0 (+https://awertt.org)', 'Accept': 'text/html,*/*' },
    redirect: 'follow'
  };

  try {
    // 1) Fetch search page
    const searchUrl = `https://bitmidi.com/search?q=${encodeURIComponent(q)}`;
    const sr = await fetch(searchUrl, fetchOpts);
    if (!sr.ok) return res.status(502).json({ error: `Search failed: ${sr.status} ${sr.statusText}` });
    const $ = cheerio.load(await sr.text());

    // 2) Collect candidate track pages
    const pageLinks = new Set();
    $('a[href]').each((_, a) => {
      const href = $(a).attr('href');
      if (href && /\/[a-z0-9-]+-mid$/i.test(href)) {
        pageLinks.add(new URL(href, 'https://bitmidi.com').toString());
      }
    });

    const pages = Array.from(pageLinks).slice(0, limit);
    const results = [];

    // 3) Visit each page and extract direct .mid link
    for (const pageUrl of pages) {
      try {
        const pr = await fetch(pageUrl, fetchOpts);
        if (!pr.ok) continue;
        const $$ = cheerio.load(await pr.text());

        const title = ($$('h1').first().text() || $$('title').first().text() || '').trim();
        let downloadUrl = null;

        $$('a[href$=".mid"], a[href*=".mid"]').each((_, a) => {
          const href = $$(a).attr('href');
          if (href && /\.mid(\?.*)?$/i.test(href)) {
            downloadUrl = new URL(href, 'https://bitmidi.com').toString();
            return false; // break
          }
        });

        // rough size parse (optional best-effort)
        let kb = null;
        const bodyText = $$('body').text();
        const m = bodyText.match(/([0-9]+(?:\.[0-9]+)?)\s*(KB|MB)\b/i);
        if (m) {
          const v = parseFloat(m[1]);
          kb = /MB/i.test(m[2]) ? Math.round(v * 1024) : Math.round(v);
        }

        if (downloadUrl) {
          results.push({ title, pageUrl, downloadUrl, kb });
        }
      } catch (err) {
        console.error('search page parse error:', pageUrl, err?.message || err);
      }
    }

    res.json({ query: q, count: results.length, results });
  } catch (err) {
    console.error('bitmidi/search error:', err?.message || err);
    res.status(500).json({ error: err?.message || String(err) });
  }
});

// ---------- health & static ----------
app.get('/healthz', (_, res) => res.status(200).send('OK'));

const PUBLIC_DIR = path.join(__dirname, 'public');
app.use(express.static(PUBLIC_DIR, { extensions: ['html'] }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`✅ awertt-app listening on :${PORT}`));
