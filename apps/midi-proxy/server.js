const path = require('path');
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');
const cheerio = require('cheerio');

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

// --- BitMidi search → list of { title, pageUrl, downloadUrl, kb } ---
app.get('/bitmidi/search', async (req, res) => {
  const q = (req.query.q || '').trim();
  const limit = Math.min(parseInt(req.query.limit || '10', 10) || 10, 25); // cap 25
  if (!q) return res.status(400).json({ error: "Missing 'q' parameter" });

  // polite defaults
  const UA = 'awertt-midi-proxy/1.0 (+https://awertt.org) Node-fetch';
  const fetchOpts = { headers: { 'User-Agent': UA }, redirect: 'follow', timeout: 15000 };

  try {
    // 1) search page
    const searchUrl = `https://bitmidi.com/search?q=${encodeURIComponent(q)}`;
    const sr = await fetch(searchUrl, fetchOpts);
    if (!sr.ok) return res.status(502).json({ error: `Search failed: ${sr.status}` });
    const $ = cheerio.load(await sr.text());

    // 2) collect candidate result page links (best-effort selectors)
    const pageHrefs = new Set();
    $('a[href]').each((_, a) => {
      const href = $(a).attr('href');
      if (!href) return;
      // BitMidi track pages typically look like "/some-song-mid" or similar
      if (/\/[a-z0-9-]+-mid$/i.test(href) || (/^\/.+/.test(href) && !href.endsWith('.mid'))) {
        pageHrefs.add(new URL(href, 'https://bitmidi.com').toString());
      }
    });

    const pages = Array.from(pageHrefs).slice(0, limit);

    // 3) visit each result page to extract the .mid link
    const results = [];
    for (const pageUrl of pages) {
      try {
        const pr = await fetch(pageUrl, fetchOpts);
        if (!pr.ok) continue;
        const $$ = cheerio.load(await pr.text());

        // Heuristics to find the direct .mid link & title
        let downloadUrl = null;
        let title = ($$('h1').first().text() || $$('title').first().text() || '').trim();

        // common pattern: <a href="https://bitmidi.com/uploads/.../file.mid">
        $$('a[href$=".mid"], a[href*=".mid"]').each((_, a) => {
          const href = $$(a).attr('href');
          if (href && /\.mid(\?.*)?$/i.test(href)) {
            downloadUrl = new URL(href, 'https://bitmidi.com').toString();
            return false;
          }
        });

        // Try to parse an approximate file size if shown on page
        let kb = null;
        const sizeText = $$('body').text();
        const m = sizeText.match(/([0-9]+(?:\.[0-9]+)?)\s*(KB|MB)\b/i);
        if (m) {
          const val = parseFloat(m[1]);
          kb = /MB/i.test(m[2]) ? Math.round(val * 1024) : Math.round(val);
        }

        if (downloadUrl) {
          results.push({ title, pageUrl, downloadUrl, kb });
        }
      } catch (_) {
        // skip that item if it errors
      }
    }

    res.json({ query: q, count: results.length, results });
  } catch (err) {
    res.status(500).json({ error: err?.message || String(err) });
  }
});

// --- Health check ---
app.get('/healthz', (_, res) => res.status(200).send('OK'));

// --- Static website from /public (index.html, etc.) ---
const PUBLIC_DIR = path.join(__dirname, 'public');
app.use(express.static(PUBLIC_DIR, { extensions: ['html'] }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`✅ awertt-app listening on :${PORT}`));
