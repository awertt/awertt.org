// apps/midi-proxy/server.js

const path = require('path');
const express = require('express');
const cors = require('cors');
const fetch = require('node-fetch');           // v2.x (require-compatible)
const cheerio = require('cheerio');

// AbortController polyfill for Node 18
let AbortController = global.AbortController;
try { if (!AbortController) AbortController = require('abort-controller'); }
catch { AbortController = class { constructor(){ this.signal = undefined; } }; }

const app = express();
app.set('trust proxy', true);
app.use(cors());

// ---------- /getMidi ----------
app.get('/getMidi', async (req, res) => {
  const midiUrl = req.query.url;
  if (!midiUrl) return res.status(400).send("Missing 'url' parameter");

  // validate URL & scheme
  let u;
  try { u = new URL(midiUrl); } catch { return res.status(400).send("Bad 'url'"); }
  if (!/^https?:$/.test(u.protocol)) return res.status(400).send("Only http(s) URLs allowed");

  const headers = {
    'User-Agent': 'awertt-midi-proxy/1.0 (+https://awertt.org)',
    'Accept': '*/*'
  };
  if (u.hostname.endsWith('bitmidi.com')) headers['Referer'] = 'https://bitmidi.com/';

  try {
    const controller = new AbortController();
    const to = setTimeout(() => { try { controller.abort(); } catch {} }, 15000);

    const r = await fetch(midiUrl, { redirect: 'follow', signal: controller.signal, headers });
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

// ---------- /bitmidi/search ----------
app.get('/bitmidi/search', async (req, res) => {
  const q = (req.query.q || '').trim();
  if (!q) return res.status(400).json({ error: "Missing 'q' parameter" });

  // Tunables (overridable via querystring)
  const maxPages    = Math.min(parseInt(req.query.maxPages   || '50', 10) || 50, 200);    // how many paginated search pages at most
  const maxResults  = Math.min(parseInt(req.query.maxResults || '2000',10) || 2000, 10000);
  const concurrency = Math.min(parseInt(req.query.concurrency|| '6', 10) || 6, 24);
  const timeoutMs   = Math.min(parseInt(req.query.timeoutMs  || '15000',10) || 15000, 30000);

  const headers = {
    'User-Agent': 'awertt-midi-proxy/1.0 (+https://awertt.org)',
    'Accept': 'text/html,*/*'
  };

  const abs = (href, base='https://bitmidi.com') => new URL(href, base).toString();

  function controllerWithTimeout(ms) {
    const controller = new AbortController();
    const t = setTimeout(() => { try { controller.abort(); } catch {} }, ms);
    return { controller, clear: () => clearTimeout(t) };
  }

  async function fetchHTML(url) {
    const { controller, clear } = controllerWithTimeout(timeoutMs);
    try {
      const r = await fetch(url, { headers, redirect: 'follow', signal: controller.signal });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return await r.text();
    } finally {
      clear();
    }
  }

  // Extract song detail page URLs from a search results HTML page
  function extractSongPages(html) {
    const $ = cheerio.load(html);
    const set = new Set();
    $('a[href]').each((_, a) => {
      const href = $(a).attr('href');
      if (href && /\/[a-z0-9-]+-mid$/i.test(href)) {
        set.add(abs(href));
      }
    });
    return Array.from(set);
  }

  async function parseSongPage(url) {
    try {
      const html = await fetchHTML(url);
      const $ = cheerio.load(html);

      const title = ($('h1').first().text() || $('title').first().text() || '').trim();

      let downloadUrl = null;
      $('a[href$=".mid"], a[href*=".mid"]').each((_, a) => {
        const href = $(a).attr('href');
        if (href && /\.mid(\?.*)?$/i.test(href)) {
          downloadUrl = abs(href);
          return false;
        }
      });

      let kb = null;
      const bodyText = $('body').text();
      const m = bodyText.match(/([0-9]+(?:\.[0-9]+)?)\s*(KB|MB)\b/i);
      if (m) {
        const v = parseFloat(m[1]);
        kb = /MB/i.test(m[2]) ? Math.round(v * 1024) : Math.round(v);
      }

      if (downloadUrl) {
        return {
          title,
          url: downloadUrl,   // preferred key for Roblox client
          downloadUrl,        // backward-compat
          pageUrl: url,
          kb
        };
      }
    } catch (err) {
      console.error('search song parse error:', url, err?.message || err);
    }
    return null;
  }

  try {
    // Crawl paginated search pages deterministically: page=0,1,2...
    const allSongPages = new Set();
    let pagesVisited = 0;

    for (let page = 0; page < maxPages; page++) {
      const searchUrl = `https://bitmidi.com/search?q=${encodeURIComponent(q)}&page=${page}`;
      let html;

      try {
        html = await fetchHTML(searchUrl);
      } catch (e) {
        // stop on fetch errors (network, 404, etc.)
        if (page === 0) {
          // Some queries show page=0 implicitly with no page param; try without &page for first request
          try {
            const altUrl = `https://bitmidi.com/search?q=${encodeURIComponent(q)}`;
            html = await fetchHTML(altUrl);
          } catch (e2) {
            console.error('fetch search page failed:', searchUrl, e2?.message || e2);
            break;
          }
        } else {
          console.error('fetch search page failed:', searchUrl, e?.message || e);
          break;
        }
      }

      const songPages = extractSongPages(html);
      pagesVisited++;

      if (songPages.length === 0) {
        // No results on this page → end of pagination
        break;
      }

      for (const sp of songPages) {
        if (allSongPages.size >= maxResults) break;
        allSongPages.add(sp);
      }

      if (allSongPages.size >= maxResults) break;
    }

    // Fetch all song pages (limited concurrency)
    const songList = Array.from(allSongPages).slice(0, maxResults);
    const out = [];
    let cursor = 0;

    async function worker() {
      while (cursor < songList.length) {
        const i = cursor++;
        const item = await parseSongPage(songList[i]);
        if (item) out.push(item);
      }
    }
    await Promise.all(Array.from({ length: Math.min(concurrency, songList.length || 1) }, worker));

    // Sort results by title for stable ordering
    out.sort((a, b) => String(a.title || '').localeCompare(String(b.title || '')));

    res.json({
      query: q,
      pagesVisited,
      count: out.length,
      results: out
    });
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
