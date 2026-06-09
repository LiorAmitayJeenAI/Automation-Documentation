require('dotenv').config();
const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();
const URLS_FILE = path.join(__dirname, 'urls.json');
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static('public'));

function loadUrls() {
  if (!fs.existsSync(URLS_FILE)) return [];
  return JSON.parse(fs.readFileSync(URLS_FILE, 'utf8'));
}

function saveUrls(urls) {
  fs.writeFileSync(URLS_FILE, JSON.stringify(urls, null, 2));
}

async function runOnLangflow(url, language, linkType) {
  const baseUrl = linkType === 'admin'
    ? process.env.ADMIN_URL
    : process.env.REGULAR_URL;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10 * 60 * 1000); // 10 min

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

// Run selected URLs through Langflow, streaming results via SSE
app.post('/api/run', async (req, res) => {
  const { urls, language = 'he', linkType = 'regular' } = req.body;
  if (!urls || !urls.length) return res.status(400).json({ error: 'No URLs provided' });

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  for (const url of urls) {
    res.write(`data: ${JSON.stringify({ url, status: 'running' })}\n\n`);
    try {
      const result = await runOnLangflow(url, language, linkType);
      res.write(`data: ${JSON.stringify({ url, status: 'done', result })}\n\n`);
    } catch (err) {
      res.write(`data: ${JSON.stringify({ url, status: 'error', error: err.message })}\n\n`);
    }
  }

  res.write(`data: ${JSON.stringify({ status: 'complete' })}\n\n`);
  res.end();
});

app.listen(PORT, () => {
  console.log(`Running at http://localhost:${PORT}`);
});
