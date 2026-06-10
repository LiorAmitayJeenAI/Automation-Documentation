require('dotenv').config();
const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();
const URLS_FILE      = path.join(__dirname, 'urls.json');
const TUTORIALS_FILE = path.join(__dirname, 'tutorials.json');
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static('public'));

/* ── Persistence helpers ── */
function loadUrls() {
  if (!fs.existsSync(URLS_FILE)) return [];
  try { return JSON.parse(fs.readFileSync(URLS_FILE, 'utf8')); } catch { return []; }
}
function saveUrls(urls) {
  fs.writeFileSync(URLS_FILE, JSON.stringify(urls, null, 2));
}

function loadTutorials() {
  if (!fs.existsSync(TUTORIALS_FILE)) return [];
  try { return JSON.parse(fs.readFileSync(TUTORIALS_FILE, 'utf8')); } catch { return []; }
}
function saveTutorials(t) {
  fs.writeFileSync(TUTORIALS_FILE, JSON.stringify(t, null, 2));
}

function makeId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

/* ── Extract Gamma / SharePoint URLs from Langflow output ── */
function extractGeneratedUrls(result) {
  let text = '';
  try {
    text = result?.outputs?.[0]?.outputs?.[0]?.results?.message?.text || JSON.stringify(result);
  } catch { text = String(result); }

  const gammaMatch = text.match(/https:\/\/gamma\.app\/[^\s"'<>)]+/);
  const spMatch    = text.match(/https:\/\/[^\s"'<>)]*sharepoint\.com\/[^\s"'<>)]+/);

  return {
    gammaUrl:      gammaMatch ? gammaMatch[0].replace(/[.,;]+$/, '') : null,
    sharepointUrl: spMatch    ? spMatch[0].replace(/[.,;]+$/, '')    : null,
  };
}

/* ── Create or update a tutorial record by URL ── */
function upsertTutorial(url, updates) {
  const list = loadTutorials();
  const existing = list.find(t => t.url === url);
  if (existing) {
    Object.assign(existing, updates, { lastUpdatedAt: new Date().toISOString() });
  } else {
    const urlsList = loadUrls();
    const entry = urlsList.find(u => u.url === url);
    list.unshift({
      id:            makeId(),
      url,
      label:         entry?.label || '',
      status:        'pending',
      gammaUrl:      null,
      sharepointUrl: null,
      createdAt:     new Date().toISOString(),
      lastUpdatedAt: new Date().toISOString(),
      error:         null,
      ...updates,
    });
  }
  saveTutorials(list);
}

/* ── Langflow call ── */
async function runOnLangflow(url, language, linkType) {
  const baseUrl = linkType === 'admin'
    ? process.env.ADMIN_URL
    : process.env.REGULAR_URL;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10 * 60 * 1000);

  const res = await fetch(process.env.LANGFLOW_API_URL, {
    method: 'POST',
    signal: controller.signal,
    headers: {
      'x-api-key': process.env.LANGFLOW_API_KEY,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      input_value: url,
      output_type: 'chat',
      input_type: 'chat',
      tweaks: { language, linkType, baseUrl },
    }),
  });
  clearTimeout(timeout);
  if (!res.ok) throw new Error(`Langflow error: ${res.status} ${res.statusText}`);
  return await res.json();
}

/* ══════════════════════════════════════════════════
   URL endpoints
══════════════════════════════════════════════════ */
app.get('/api/urls', (req, res) => {
  res.json(loadUrls());
});

app.post('/api/urls', (req, res) => {
  const { url, label } = req.body;
  if (!url || !url.trim()) return res.status(400).json({ error: 'URL is required' });
  const urls = loadUrls();
  urls.push({ url: url.trim(), label: (label || '').trim(), addedAt: new Date().toISOString() });
  saveUrls(urls);
  res.json(urls);
});

app.delete('/api/urls/:index', (req, res) => {
  const index = parseInt(req.params.index);
  const urls = loadUrls();
  if (index < 0 || index >= urls.length) return res.status(400).json({ error: 'Invalid index' });
  urls.splice(index, 1);
  saveUrls(urls);
  res.json(urls);
});

/* ══════════════════════════════════════════════════
   Run selected URLs — SSE stream
══════════════════════════════════════════════════ */
app.post('/api/run', async (req, res) => {
  const { urls, language = 'he', linkType = 'regular' } = req.body;
  if (!urls || !urls.length) return res.status(400).json({ error: 'No URLs provided' });

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  for (const url of urls) {
    upsertTutorial(url, { status: 'running', error: null });
    res.write(`data: ${JSON.stringify({ url, status: 'running' })}\n\n`);
    try {
      const result = await runOnLangflow(url, language, linkType);
      const { gammaUrl, sharepointUrl } = extractGeneratedUrls(result);
      upsertTutorial(url, { status: 'done', gammaUrl, sharepointUrl, error: null });
      res.write(`data: ${JSON.stringify({ url, status: 'done', result })}\n\n`);
    } catch (err) {
      upsertTutorial(url, { status: 'error', error: err.message });
      res.write(`data: ${JSON.stringify({ url, status: 'error', error: err.message })}\n\n`);
    }
  }

  res.write(`data: ${JSON.stringify({ status: 'complete' })}\n\n`);
  res.end();
});

/* ══════════════════════════════════════════════════
   Tutorials endpoints
══════════════════════════════════════════════════ */
app.get('/api/tutorials', (req, res) => {
  res.json(loadTutorials());
});

app.post('/api/tutorials/:id/regenerate', async (req, res) => {
  const list = loadTutorials();
  const tutorial = list.find(t => t.id === req.params.id);
  if (!tutorial) return res.status(404).json({ error: 'Tutorial not found' });

  const { language = 'he', linkType = 'regular' } = req.body || {};

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  upsertTutorial(tutorial.url, { status: 'running', error: null });
  res.write(`data: ${JSON.stringify({ status: 'running' })}\n\n`);

  try {
    const result = await runOnLangflow(tutorial.url, language, linkType);
    const { gammaUrl, sharepointUrl } = extractGeneratedUrls(result);
    upsertTutorial(tutorial.url, { status: 'done', gammaUrl, sharepointUrl, error: null });
    res.write(`data: ${JSON.stringify({ status: 'done', gammaUrl, sharepointUrl })}\n\n`);
  } catch (err) {
    upsertTutorial(tutorial.url, { status: 'error', error: err.message });
    res.write(`data: ${JSON.stringify({ status: 'error', error: err.message })}\n\n`);
  }

  res.write(`data: ${JSON.stringify({ status: 'complete' })}\n\n`);
  res.end();
});

app.listen(PORT, () => {
  console.log(`Running at http://localhost:${PORT}`);
});
