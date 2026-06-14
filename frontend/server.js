const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });
const express = require('express');
const fs = require('fs');
const XLSX = require('xlsx');

const app = express();
const EXCEL_FILE     = path.join(__dirname, '..', 'data', 'pages.xlsx');
const TUTORIALS_FILE = path.join(__dirname, '..', 'tutorials.json');
const PORT = process.env.PORT || 3000;
const activeSessions = new Map();

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

/* ── Helpers ── */
function extractPageIdFromUrl(url) {
  const match = url.match(/\/pages\/(\d+)/);
  return match ? match[1] : null;
}

function makeId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

function makeSessionId(prefix = 'tutorial-run') {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeLinkType(value) {
  return value === 'admin' ? 'admin' : 'regular';
}

function getLinkTypeForFolder(folderName = '') {
  return String(folderName).toLowerCase().includes('admin') ? 'admin' : 'regular';
}

function normalizeRunItems({ items, urls, linkType }) {
  if (Array.isArray(items)) {
    return items
      .map(item => {
        const url = (item?.url || '').trim();
        if (!url) return null;
        const derivedLinkType = item.folderName
          ? getLinkTypeForFolder(item.folderName)
          : item.linkType;
        return {
          url,
          linkType: normalizeLinkType(derivedLinkType),
        };
      })
      .filter(Boolean);
  }

  if (Array.isArray(urls)) {
    return urls
      .map(url => (typeof url === 'string' ? url.trim() : ''))
      .filter(Boolean)
      .map(url => ({ url, linkType: normalizeLinkType(linkType) }));
  }

  return [];
}

/* ── Persistence (Excel-backed) ── */
function loadFoldersRaw() {
  if (!fs.existsSync(EXCEL_FILE)) return { folders: [] };
  try {
    const wb = XLSX.readFile(EXCEL_FILE);
    const ws = wb.Sheets[wb.SheetNames[0]];
    const rows = XLSX.utils.sheet_to_json(ws);

    const folderMap = new Map();
    for (const row of rows) {
      const folderName = (row['file name'] || '').trim();
      const label = (row['part name'] || '').trim();
      const url = (row['url'] || '').trim();
      if (!folderName || !url) continue;

      if (!folderMap.has(folderName)) {
        folderMap.set(folderName, {
          id: `folder-${folderName.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`,
          name: folderName,
          confluencePageId: null,
          confluenceUrl: null,
          pages: [],
        });
      }
      folderMap.get(folderName).pages.push({
        url,
        label,
        pageId: extractPageIdFromUrl(url),
        addedAt: new Date().toISOString(),
      });
    }

    return { folders: [...folderMap.values()] };
  } catch (err) {
    console.error('Error reading Excel:', err.message);
    return { folders: [] };
  }
}

function saveFolders(data) {
  const rows = [];
  for (const folder of data.folders) {
    for (const page of folder.pages) {
      rows.push({
        'file name': folder.name,
        'part name': page.label || '',
        'url': page.url,
      });
    }
  }

  const ws = XLSX.utils.json_to_sheet(rows);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Pages');

  const dir = path.dirname(EXCEL_FILE);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  XLSX.writeFile(wb, EXCEL_FILE);
}

function loadUrlsFlat() {
  const data = loadFoldersRaw();
  const flat = [];
  for (const folder of data.folders) {
    for (const page of folder.pages) {
      flat.push({ url: page.url, label: page.label, addedAt: page.addedAt });
    }
  }
  return flat;
}

function loadTutorials() {
  if (!fs.existsSync(TUTORIALS_FILE)) return [];
  try { return JSON.parse(fs.readFileSync(TUTORIALS_FILE, 'utf8')); } catch { return []; }
}
function saveTutorials(t) {
  fs.writeFileSync(TUTORIALS_FILE, JSON.stringify(t, null, 2));
}

/* ── Extract Gamma / SharePoint URLs from pipeline output ── */
function extractGeneratedUrls(result) {
  if (result?.gamma_url || result?.sharepoint_url) {
    return {
      gammaUrl:      result.gamma_url || null,
      sharepointUrl: result.sharepoint_url || null,
    };
  }

  let text = '';
  try {
    text = typeof result === 'string' ? result : JSON.stringify(result);
  } catch { text = String(result); }

  const gammaMatch = text.match(/https:\/\/gamma\.app\/[^\s"'<>)]+/);
  const spMatch    = text.match(/https:\/\/[^\s"'<>)]*sharepoint\.com\/[^\s"'<>)]+/);

  return {
    gammaUrl:      gammaMatch ? gammaMatch[0].replace(/[.,;]+$/, '') : null,
    sharepointUrl: spMatch    ? spMatch[0].replace(/[.,;]+$/, '')    : null,
  };
}

/* ── Create or update a tutorial record by URL ── */
function historyEntry(status, updates) {
  return {
    status,
    timestamp: new Date().toISOString(),
    language: updates.language || null,
    sessionId: updates.sessionId || null,
    gammaUrl: updates.gammaUrl || null,
    sharepointUrl: updates.sharepointUrl || null,
    error: updates.error || null,
  };
}

function upsertTutorial(url, updates, options = {}) {
  const list = loadTutorials();
  const existing = list.find(t => t.url === url);
  const now = new Date().toISOString();
  if (existing) {
    const history = Array.isArray(existing.history) ? existing.history : [];
    Object.assign(existing, updates, { lastUpdatedAt: now });
    if (options.appendHistory) {
      existing.history = [historyEntry(updates.status || existing.status, updates), ...history].slice(0, 25);
    }
  } else {
    const flat = loadUrlsFlat();
    const entry = flat.find(u => u.url === url);
    const record = {
      id:            makeId(),
      url,
      confluenceUrl: url,
      label:         entry?.label || '',
      status:        'pending',
      language:      updates.language || null,
      sessionId:     updates.sessionId || null,
      gammaUrl:      null,
      sharepointUrl: null,
      createdAt:     now,
      lastUpdatedAt: now,
      lastGeneratedAt: null,
      error:         null,
      ...updates,
      history:       [],
    };
    if (options.appendHistory) {
      record.history = [historyEntry(updates.status || record.status, updates)];
    }
    list.unshift(record);
  }
  saveTutorials(list);
}

/* ── Python backend call ── */
const http = require('http');
const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';
const PIPELINE_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes

function runPipeline(url, language, linkType, signal, sessionId) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));

    const parsed = new URL(`${PYTHON_BACKEND_URL}/api/generate/sync`);
    const body = JSON.stringify({
      confluence_url: url,
      language,
      link_type: linkType,
      session_id: sessionId,
    });

    const req = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port,
        path: parsed.pathname,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
        },
        timeout: PIPELINE_TIMEOUT_MS,
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString();
          if (res.statusCode >= 400) {
            return reject(new Error(`Pipeline error: ${res.statusCode} ${res.statusMessage} - ${raw}`));
          }
          try {
            resolve(JSON.parse(raw));
          } catch {
            reject(new Error(`Invalid JSON from pipeline: ${raw.slice(0, 200)}`));
          }
        });
      },
    );

    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Pipeline request timed out'));
    });

    const onAbort = () => {
      req.destroy();
      reject(new DOMException('Aborted', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
    req.on('close', () => signal?.removeEventListener('abort', onAbort));

    req.write(body);
    req.end();
  });
}

/* ══════════════════════════════════════════════════
   Folder endpoints
══════════════════════════════════════════════════ */
app.get('/api/folders', (req, res) => {
  res.json(loadFoldersRaw());
});

app.post('/api/folders', (req, res) => {
  const { name } = req.body;
  if (!name || !name.trim()) return res.status(400).json({ error: 'Folder name is required' });

  const data = loadFoldersRaw();
  const folder = {
    id: `folder-${makeId()}`,
    name: name.trim(),
    confluencePageId: null,
    confluenceUrl: null,
    pages: [],
  };
  data.folders.push(folder);
  saveFolders(data);
  res.json(data);
});

app.delete('/api/folders/:folderId', (req, res) => {
  const data = loadFoldersRaw();
  const idx = data.folders.findIndex(f => f.id === req.params.folderId);
  if (idx === -1) return res.status(404).json({ error: 'Folder not found' });
  data.folders.splice(idx, 1);
  saveFolders(data);
  res.json(data);
});

app.post('/api/folders/:folderId/urls', (req, res) => {
  const { url, label, newFolderName } = req.body;
  if (!url || !url.trim()) return res.status(400).json({ error: 'URL is required' });

  const data = loadFoldersRaw();
  let folder = data.folders.find(f => f.id === req.params.folderId);

  if (!folder && newFolderName) {
    folder = {
      id: req.params.folderId,
      name: newFolderName.trim(),
      confluencePageId: null,
      confluenceUrl: null,
      pages: [],
    };
    data.folders.push(folder);
  }

  if (!folder) return res.status(404).json({ error: 'Folder not found' });

  folder.pages.push({
    url: url.trim(),
    label: (label || '').trim(),
    pageId: extractPageIdFromUrl(url),
    addedAt: new Date().toISOString(),
  });
  saveFolders(data);
  res.json(data);
});

app.delete('/api/folders/:folderId/urls/:pageIndex', (req, res) => {
  const data = loadFoldersRaw();
  const folder = data.folders.find(f => f.id === req.params.folderId);
  if (!folder) return res.status(404).json({ error: 'Folder not found' });

  const index = parseInt(req.params.pageIndex);
  if (index < 0 || index >= folder.pages.length) return res.status(400).json({ error: 'Invalid page index' });

  folder.pages.splice(index, 1);
  saveFolders(data);
  res.json(data);
});


/* ══════════════════════════════════════════════════
   Discover product routes (Playwright crawl)
══════════════════════════════════════════════════ */
const ROUTE_DISCOVERY_TIMEOUT_MS = 3 * 60 * 1000; // 3 minutes

function discoverRoutes(linkType) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(`${PYTHON_BACKEND_URL}/api/discover-routes`);
    const body = JSON.stringify({ link_type: linkType });

    const req = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port,
        path: parsed.pathname,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
        },
        timeout: ROUTE_DISCOVERY_TIMEOUT_MS,
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString();
          if (res.statusCode >= 400) {
            return reject(new Error(`Route discovery error: ${res.statusCode} - ${raw}`));
          }
          try {
            resolve(JSON.parse(raw));
          } catch {
            reject(new Error(`Invalid JSON from route discovery: ${raw.slice(0, 200)}`));
          }
        });
      },
    );

    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Route discovery request timed out'));
    });

    req.write(body);
    req.end();
  });
}

app.post('/api/discover-routes', async (req, res) => {
  const requestedTypes = Array.isArray(req.body?.linkTypes) && req.body.linkTypes.length
    ? req.body.linkTypes
    : [req.body?.linkType || 'regular'];
  const linkTypes = [...new Set(requestedTypes.map(normalizeLinkType))];

  try {
    const results = [];
    for (const linkType of linkTypes) {
      const result = await discoverRoutes(linkType);
      results.push({ linkType, ...result });
    }

    const added = results.flatMap(result =>
      Array.isArray(result.added)
        ? result.added.map(route => ({ ...route, linkType: result.linkType }))
        : []
    );
    const latest = results[results.length - 1] || {};
    res.json({
      added,
      total: latest.total || 0,
      discovered: results.reduce((sum, result) => sum + (result.discovered || 0), 0),
      results,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/* ══════════════════════════════════════════════════
   Run selected URLs — SSE stream
══════════════════════════════════════════════════ */
app.post('/api/run', async (req, res) => {
  const { urls, items, language = 'he', linkType = 'regular' } = req.body;
  const runItems = normalizeRunItems({ items, urls, linkType });
  if (!runItems.length) return res.status(400).json({ error: 'No URLs provided' });

  const runId = makeSessionId('run');
  const runController = new AbortController();
  const runSessionIds = new Set();

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  res.on('close', () => {
    if (!runController.signal.aborted) {
      runController.abort();
      for (const sid of runSessionIds) {
        const session = activeSessions.get(sid);
        if (session) session.controller.abort();
      }
    }
  });

  const writeEvent = payload => {
    if (runController.signal.aborted || res.destroyed) return false;
    try {
      res.write(`data: ${JSON.stringify(payload)}\n\n`);
      return true;
    } catch { return false; }
  };

  for (const item of runItems) {
    const { url, linkType: itemLinkType } = item;

    if (runController.signal.aborted) {
      upsertTutorial(url, {
        status: 'pending',
        language,
        error: 'Generation stopped by user',
      }, { appendHistory: true });
      writeEvent({ url, status: 'stopped', error: 'Generation stopped by user' });
      continue;
    }

    const sessionId = makeSessionId('tutorial-run');
    const sessionController = new AbortController();
    runSessionIds.add(sessionId);
    activeSessions.set(sessionId, { controller: sessionController, url, linkType: itemLinkType });

    runController.signal.addEventListener('abort', () => sessionController.abort(), { once: true });

    upsertTutorial(url, { status: 'running', language, sessionId, error: null }, { appendHistory: true });
    writeEvent({ url, status: 'running', sessionId, linkType: itemLinkType });
    try {
      const result = await runPipeline(url, language, itemLinkType, sessionController.signal, sessionId);
      const { gammaUrl, sharepointUrl } = extractGeneratedUrls(result);
      upsertTutorial(url, {
        status: 'done',
        language,
        sessionId,
        gammaUrl,
        sharepointUrl,
        lastGeneratedAt: new Date().toISOString(),
        error: null,
      }, { appendHistory: true });
      writeEvent({ url, status: 'done', sessionId, linkType: itemLinkType, result });
    } catch (err) {
      if (sessionController.signal.aborted || err.name === 'AbortError') {
        upsertTutorial(url, {
          status: 'pending',
          language,
          sessionId,
          error: 'Generation stopped by user',
        }, { appendHistory: true });
        writeEvent({ url, status: 'stopped', sessionId, linkType: itemLinkType, error: 'Generation stopped by user' });
        continue;
      }
      upsertTutorial(url, { status: 'error', language, sessionId, error: err.message }, { appendHistory: true });
      writeEvent({ url, status: 'error', sessionId, linkType: itemLinkType, error: err.message });
    } finally {
      activeSessions.delete(sessionId);
      runSessionIds.delete(sessionId);
    }
  }

  writeEvent({ status: 'complete' });
  if (!res.destroyed) {
    try { res.end(); } catch {}
  }
});

app.post('/api/sessions/:sessionId/stop', (req, res) => {
  const session = activeSessions.get(req.params.sessionId);
  if (!session) {
    return res.status(404).json({ error: 'Session is not running' });
  }

  session.controller.abort();
  res.json({ stopped: true, sessionId: req.params.sessionId });
});

/* ══════════════════════════════════════════════════
   Tutorials endpoints
══════════════════════════════════════════════════ */
app.get('/api/tutorials', (req, res) => {
  res.json(loadTutorials());
});

app.delete('/api/tutorials/:id', (req, res) => {
  const list = loadTutorials();
  const next = list.filter(t => t.id !== req.params.id);
  if (next.length === list.length) return res.status(404).json({ error: 'Tutorial not found' });

  saveTutorials(next);
  res.json({ deleted: true, tutorials: next });
});

app.post('/api/tutorials/:id/regenerate', async (req, res) => {
  const list = loadTutorials();
  const tutorial = list.find(t => t.id === req.params.id);
  if (!tutorial) return res.status(404).json({ error: 'Tutorial not found' });

  const { language = 'he', linkType = 'regular' } = req.body || {};
  const sessionId = makeSessionId('tutorial-regenerate');

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  upsertTutorial(tutorial.url, { status: 'running', language, sessionId, error: null }, { appendHistory: true });
  res.write(`data: ${JSON.stringify({ status: 'running', sessionId })}\n\n`);

  try {
    const result = await runPipeline(tutorial.url, language, linkType, undefined, sessionId);
    const { gammaUrl, sharepointUrl } = extractGeneratedUrls(result);
    upsertTutorial(tutorial.url, {
      status: 'done',
      language,
      sessionId,
      gammaUrl,
      sharepointUrl,
      lastGeneratedAt: new Date().toISOString(),
      error: null,
    }, { appendHistory: true });
    res.write(`data: ${JSON.stringify({ status: 'done', sessionId, gammaUrl, sharepointUrl })}\n\n`);
  } catch (err) {
    upsertTutorial(tutorial.url, { status: 'error', language, sessionId, error: err.message }, { appendHistory: true });
    res.write(`data: ${JSON.stringify({ status: 'error', sessionId, error: err.message })}\n\n`);
  }

  res.write(`data: ${JSON.stringify({ status: 'complete', sessionId })}\n\n`);
  res.end();
});

app.get('/generate', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'generate.html'));
});

app.listen(PORT, () => {
  console.log(`Running at http://localhost:${PORT}`);
});
