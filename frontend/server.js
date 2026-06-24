const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });
const express = require('express');
const fs = require('fs');
const XLSX = require('xlsx');
const { EventEmitter } = require('events');

const app = express();
const EXCEL_FILE     = path.join(__dirname, '..', 'data', 'pages.xlsx');
const TUTORIALS_FILE = path.join(__dirname, '..', 'tutorials.json');
const PORT = process.env.PORT || 3000;
const activeSessions = new Map();
const activeRuns = new Map();

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
          folderName: item.folderName || '',
          label: item.label || '',
        };
      })
      .filter(Boolean);
  }

  if (Array.isArray(urls)) {
    return urls
      .map(url => (typeof url === 'string' ? url.trim() : ''))
      .filter(Boolean)
      .map(url => ({ url, linkType: normalizeLinkType(linkType), folderName: '', label: '' }));
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
      flat.push({ url: page.url, label: page.label, folderName: folder.name, addedAt: page.addedAt });
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

/**
 * Detect records whose language was incorrectly overwritten by a previous
 * batch run (old code looked up by URL only).  A corrupted record has
 * language:"en" but was never actually completed in English — its gammaUrl
 * still points to the Hebrew presentation.  Fix by resetting language to
 * the language of the most recent "done" history entry.
 */
function migrateCorrruptedLanguageRecords() {
  const list = loadTutorials();
  let changed = false;

  for (const t of list) {
    if (!t.language || t.language === 'he') continue;
    if (!t.gammaUrl) continue;

    const history = Array.isArray(t.history) ? t.history : [];
    const hasDoneInCurrentLang = history.some(
      h => h.status === 'done' && h.language === t.language && h.gammaUrl
    );
    if (hasDoneInCurrentLang) continue;

    const lastDone = history.find(h => h.status === 'done' && h.gammaUrl);
    const originalLang = lastDone?.language || 'he';

    if (originalLang !== t.language) {
      console.log(
        `[migration] Fixing corrupted record "${t.label}" (${t.id}): ` +
        `language "${t.language}" → "${originalLang}"`
      );
      t.language = originalLang;
      t.status = 'done';
      changed = true;
    }
  }

  if (changed) saveTutorials(list);
}

/* ── Extract Gamma / SharePoint URLs from pipeline output ── */
function extractGeneratedUrls(result) {
  if (result?.gamma_url || result?.sharepoint_url || result?.video_url) {
    return {
      gammaUrl:      result.gamma_url || null,
      sharepointUrl: result.sharepoint_url || null,
      videoUrl:      result.video_url || null,
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
    videoUrl:      null,
  };
}

/* ── Handle stopped/cancelled tutorial ── */
function handleStoppedTutorial(url, updates) {
  const list = loadTutorials();
  const lang = updates.language || null;
  const existing = list.find(t => t.url === url && t.language === lang);

  if (!existing) return;

  const wasPreviouslyGenerated = existing.gammaUrl || existing.sharepointUrl || existing.videoUrl;

  if (wasPreviouslyGenerated) {
    const history = Array.isArray(existing.history) ? existing.history : [];
    existing.status = 'done';
    existing.error = null;
    existing.history = [
      historyEntry('stopped', updates),
      ...history,
    ].slice(0, 25);
    existing.lastUpdatedAt = new Date().toISOString();
    saveTutorials(list);
  } else {
    saveTutorials(list.filter(t => !(t.url === url && t.language === lang)));
  }
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
    videoUrl: updates.videoUrl || null,
    error: updates.error || null,
  };
}

function upsertTutorial(url, updates, options = {}) {
  const list = loadTutorials();
  const lang = updates.language || null;
  let existing = list.find(t => t.url === url && t.language === lang);

  // Safeguard: if the found record has a gammaUrl but was never completed in
  // this language, it's a corrupted record.  Restore its original language and
  // treat it as "not found" so a fresh record is created for the new language.
  if (existing && existing.gammaUrl && lang) {
    const history = Array.isArray(existing.history) ? existing.history : [];
    const everDoneInLang = history.some(
      h => h.status === 'done' && h.language === lang && h.gammaUrl
    );
    if (!everDoneInLang) {
      const lastDone = history.find(h => h.status === 'done' && h.gammaUrl);
      if (lastDone && lastDone.language && lastDone.language !== lang) {
        existing.language = lastDone.language;
        existing.status = 'done';
        existing = null; // force creation of a new record for the target language
      }
    }
  }

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
      videoUrl:      null,
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

/* ── Python backend calls ── */
const http = require('http');
const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000';
const PIPELINE_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes
const VIDEO_PIPELINE_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes (recording + render)

function runPipeline(url, language, linkType, signal, sessionId, folderName = '', label = '') {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));

    const parsed = new URL(`${PYTHON_BACKEND_URL}/api/generate/sync`);
    const body = JSON.stringify({
      confluence_url: url,
      language,
      link_type: linkType,
      session_id: sessionId,
      folder_name: folderName,
      part_name: label,
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

/**
 * Stream the video pipeline SSE endpoint, logging each stage and calling
 * onStageEvent(event) for live UI updates. Resolves with the final event data.
 */
function runVideoPipelineStream(url, language, linkType, signal, sessionId, onStageEvent, folderName = '', label = '') {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));

    const parsed = new URL(`${PYTHON_BACKEND_URL}/api/generate-video/stream`);
    const body = JSON.stringify({
      confluence_url: url,
      language,
      link_type: linkType,
      session_id: sessionId,
      folder_name: folderName,
      part_name: label,
    });

    console.log(`[video] ▶ start  session=${sessionId}  url=${url}`);

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
        timeout: VIDEO_PIPELINE_TIMEOUT_MS,
      },
      (res) => {
        if (res.statusCode >= 400) {
          const errChunks = [];
          res.on('data', c => errChunks.push(c));
          res.on('end', () =>
            reject(new Error(`Video pipeline HTTP ${res.statusCode}: ${Buffer.concat(errChunks).toString().slice(0, 300)}`))
          );
          return;
        }

        let sseBuffer = '';
        let finalEvent = null;

        res.on('data', (chunk) => {
          sseBuffer += chunk.toString();
          const lines = sseBuffer.split('\n');
          sseBuffer = lines.pop(); // keep the incomplete trailing line

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const event = JSON.parse(line.slice(6));
              const ts = new Date().toISOString().slice(11, 23);
              console.log(`[video] [${ts}] ${event.stage || '?'} ${event.status} — ${event.detail || ''}`);
              onStageEvent?.(event);
              if (event.stage === 'complete') finalEvent = event;
            } catch { /* ignore malformed lines */ }
          }
        });

        res.on('end', () => {
          if (finalEvent) {
            console.log(`[video] ✓ done  session=${sessionId}  video_url=${finalEvent.video_url || 'none'}`);
            resolve(finalEvent);
          } else {
            reject(new Error('Video stream ended without a complete event'));
          }
        });
      },
    );

    req.on('error', (err) => {
      console.error(`[video] ✗ request error  session=${sessionId}:`, err.message);
      reject(err);
    });
    req.on('timeout', () => {
      req.destroy();
      console.error(`[video] ✗ timed out  session=${sessionId}`);
      reject(new Error('Video pipeline timed out'));
    });

    const onAbort = () => { req.destroy(); reject(new DOMException('Aborted', 'AbortError')); };
    signal?.addEventListener('abort', onAbort, { once: true });
    req.on('close', () => signal?.removeEventListener('abort', onAbort));

    req.write(body);
    req.end();
  });
}

/** Sync (blocking) variant — used by fire-and-forget paths that don't need stage events. */
function runVideoPipeline(url, language, linkType, signal, sessionId, folderName = '', label = '') {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));

    const parsed = new URL(`${PYTHON_BACKEND_URL}/api/generate-video/sync`);
    const body = JSON.stringify({
      confluence_url: url,
      language,
      link_type: linkType,
      session_id: sessionId,
      folder_name: folderName,
      part_name: label,
    });

    console.log(`[video-sync] ▶ start  session=${sessionId}  url=${url}`);

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
        timeout: VIDEO_PIPELINE_TIMEOUT_MS,
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString();
          if (res.statusCode >= 400) {
            console.error(`[video-sync] ✗ HTTP ${res.statusCode}  session=${sessionId}:`, raw.slice(0, 200));
            return reject(new Error(`Video pipeline error: ${res.statusCode} - ${raw}`));
          }
          try {
            const result = JSON.parse(raw);
            console.log(`[video-sync] ✓ done  session=${sessionId}  video_url=${result?.video_url || 'none'}`);
            resolve(result);
          } catch {
            reject(new Error(`Invalid JSON from video pipeline: ${raw.slice(0, 200)}`));
          }
        });
      },
    );

    req.on('error', (err) => {
      console.error(`[video-sync] ✗ request error  session=${sessionId}:`, err.message);
      reject(err);
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('Video pipeline timed out')); });

    const onAbort = () => { req.destroy(); reject(new DOMException('Aborted', 'AbortError')); };
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
const ROUTE_DISCOVERY_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes

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
   Background run execution (decoupled from SSE)
══════════════════════════════════════════════════ */

function subscribeSSE(res, emitter) {
  const handler = payload => {
    if (res.destroyed) return;
    try { res.write(`data: ${JSON.stringify(payload)}\n\n`); } catch {}
  };
  emitter.on('event', handler);
  res.on('close', () => emitter.removeListener('event', handler));
}

async function executeRun(runId, runItems, language) {
  const run = activeRuns.get(runId);
  if (!run) return;

  for (const item of runItems) {
    run.itemStates.set(item.url, { status: 'queued' });
    upsertTutorial(item.url, { status: 'queued', language, error: null }, { appendHistory: false });
  }

  for (const item of runItems) {
    const { url, linkType: itemLinkType } = item;

    if (run.controller.signal.aborted) {
      run.itemStates.set(url, { status: 'stopped', error: 'Generation stopped by user' });
      handleStoppedTutorial(url, { language, sessionId: null, error: 'Generation stopped by user' });
      run.emitter.emit('event', { url, status: 'stopped', folderName: item.folderName, label: item.label, error: 'Generation stopped by user' });
      continue;
    }

    const sessionId = makeSessionId('tutorial-run');
    const sessionController = new AbortController();
    run.sessionIds.add(sessionId);
    activeSessions.set(sessionId, { controller: sessionController, url, linkType: itemLinkType });

    run.controller.signal.addEventListener('abort', () => sessionController.abort(), { once: true });

    run.itemStates.set(url, { status: 'running', sessionId });
    upsertTutorial(url, { status: 'running', language, sessionId, error: null }, { appendHistory: true });
    run.emitter.emit('event', { url, status: 'running', sessionId, linkType: itemLinkType, folderName: item.folderName, label: item.label });

    try {
      const result = await runPipeline(url, language, itemLinkType, sessionController.signal, sessionId, item.folderName || '', item.label || '');
      const { gammaUrl, sharepointUrl, videoUrl } = extractGeneratedUrls(result);
      run.itemStates.set(url, { status: 'done', sessionId });
      upsertTutorial(url, {
        status: 'done',
        language,
        sessionId,
        gammaUrl,
        sharepointUrl,
        videoUrl,
        lastGeneratedAt: new Date().toISOString(),
        error: null,
      }, { appendHistory: true });
      run.emitter.emit('event', { url, status: 'done', sessionId, linkType: itemLinkType, folderName: item.folderName, label: item.label, result });
    } catch (err) {
      if (sessionController.signal.aborted || err.name === 'AbortError') {
        run.itemStates.set(url, { status: 'stopped', sessionId, error: 'Generation stopped by user' });
        handleStoppedTutorial(url, { language, sessionId, error: 'Generation stopped by user' });
        run.emitter.emit('event', { url, status: 'stopped', sessionId, linkType: itemLinkType, folderName: item.folderName, label: item.label, error: 'Generation stopped by user' });
        continue;
      }
      run.itemStates.set(url, { status: 'error', sessionId, error: err.message });
      upsertTutorial(url, { status: 'error', language, sessionId, error: err.message }, { appendHistory: true });
      run.emitter.emit('event', { url, status: 'error', sessionId, linkType: itemLinkType, folderName: item.folderName, label: item.label, error: err.message });
    } finally {
      activeSessions.delete(sessionId);
      run.sessionIds.delete(sessionId);
    }
  }

  run.emitter.emit('event', { status: 'complete' });
  activeRuns.delete(runId);
}

async function executeRegenerate(runId, tutorialUrl, language, linkType) {
  const run = activeRuns.get(runId);
  if (!run) return;

  const sessionId = makeSessionId('tutorial-regenerate');
  const sessionController = new AbortController();
  run.sessionIds.add(sessionId);
  activeSessions.set(sessionId, { controller: sessionController, url: tutorialUrl, linkType });

  run.controller.signal.addEventListener('abort', () => sessionController.abort(), { once: true });

  run.itemStates.set(tutorialUrl, { status: 'running', sessionId });
  upsertTutorial(tutorialUrl, { status: 'running', language, sessionId, error: null }, { appendHistory: true });
  run.emitter.emit('event', { status: 'running', sessionId });

  try {
    const result = await runPipeline(tutorialUrl, language, linkType, sessionController.signal, sessionId);
    const { gammaUrl, sharepointUrl, videoUrl } = extractGeneratedUrls(result);
    run.itemStates.set(tutorialUrl, { status: 'done', sessionId });
    upsertTutorial(tutorialUrl, {
      status: 'done',
      language,
      sessionId,
      gammaUrl,
      sharepointUrl,
      videoUrl,
      lastGeneratedAt: new Date().toISOString(),
      error: null,
    }, { appendHistory: true });
    run.emitter.emit('event', { status: 'done', sessionId, gammaUrl, sharepointUrl, videoUrl });
  } catch (err) {
    if (sessionController.signal.aborted || err.name === 'AbortError') {
      run.itemStates.set(tutorialUrl, { status: 'stopped', sessionId, error: 'Generation stopped by user' });
      handleStoppedTutorial(tutorialUrl, { language, sessionId, error: 'Generation stopped by user' });
      run.emitter.emit('event', { status: 'stopped', sessionId, error: 'Generation stopped by user' });
    } else {
      run.itemStates.set(tutorialUrl, { status: 'error', sessionId, error: err.message });
      upsertTutorial(tutorialUrl, { status: 'error', language, sessionId, error: err.message }, { appendHistory: true });
      run.emitter.emit('event', { status: 'error', sessionId, error: err.message });
    }
  } finally {
    activeSessions.delete(sessionId);
    run.sessionIds.delete(sessionId);
  }

  run.emitter.emit('event', { status: 'complete', sessionId });
  activeRuns.delete(runId);
}

/* ══════════════════════════════════════════════════
   Run selected URLs — background execution + SSE view
══════════════════════════════════════════════════ */
app.post('/api/run', (req, res) => {
  const { urls, items, language = 'he', linkType = 'regular' } = req.body;
  const runItems = normalizeRunItems({ items, urls, linkType });
  if (!runItems.length) return res.status(400).json({ error: 'No URLs provided' });

  const runId = makeSessionId('run');
  const emitter = new EventEmitter();
  const run = {
    runId,
    emitter,
    controller: new AbortController(),
    items: runItems,
    language,
    itemStates: new Map(),
    sessionIds: new Set(),
    startedAt: new Date().toISOString(),
  };
  activeRuns.set(runId, run);

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  res.write(`data: ${JSON.stringify({ status: 'started', runId })}\n\n`);
  subscribeSSE(res, emitter);

  emitter.on('event', payload => {
    if (payload.status === 'complete') {
      if (!res.destroyed) { try { res.end(); } catch {} }
    }
  });

  executeRun(runId, runItems, language);
});

/* ══════════════════════════════════════════════════
   Video generation — separate flow from presentations
══════════════════════════════════════════════════ */
const activeVideoRuns = new Map();

async function executeVideoRun(runId, runItems, language) {
  const run = activeVideoRuns.get(runId);
  if (!run) return;

  for (const item of runItems) {
    if (run.controller.signal.aborted) {
      handleStoppedTutorial(item.url, { language, sessionId: null, error: 'Stopped by user' });
      run.emitter.emit('event', { url: item.url, status: 'stopped', folderName: item.folderName, label: item.label, error: 'Stopped by user' });
      continue;
    }

    const { url, linkType: itemLinkType } = item;
    const sessionId = makeSessionId('video-run');

    run.emitter.emit('event', { url, status: 'running', sessionId, folderName: item.folderName, label: item.label });
    console.log(`[video-run] ${runId} — starting item  url=${url}  session=${sessionId}`);

    try {
      const result = await runVideoPipelineStream(
        url, language, itemLinkType, run.controller.signal, sessionId,
        (stageEvent) => {
          run.emitter.emit('event', {
            url,
            status: 'stage',
            stage: stageEvent.stage,
            stageStatus: stageEvent.status,
            detail: stageEvent.detail,
            folderName: item.folderName,
            label: item.label,
          });
        },
        item.folderName || '',
        item.label || '',
      );

      const videoUrl = result?.video_url || null;

      upsertTutorial(url, {
        status: 'done',
        language,
        sessionId,
        videoUrl,
        lastGeneratedAt: new Date().toISOString(),
        error: null,
      }, { appendHistory: false });

      console.log(`[video-run] ${runId} — done  url=${url}  video_url=${videoUrl || 'none'}`);
      run.emitter.emit('event', { url, status: 'done', sessionId, folderName: item.folderName, label: item.label, video_url: videoUrl });
    } catch (err) {
      if (run.controller.signal.aborted || err.name === 'AbortError') {
        console.log(`[video-run] ${runId} — stopped by user  url=${url}`);
        handleStoppedTutorial(url, { language, sessionId, error: 'Stopped by user' });
        run.emitter.emit('event', { url, status: 'stopped', folderName: item.folderName, label: item.label, error: 'Stopped by user' });
      } else {
        console.error(`[video-run] ${runId} — error  url=${url}:`, err.message);
        run.emitter.emit('event', { url, status: 'error', folderName: item.folderName, label: item.label, error: err.message });
      }
    }
  }

  console.log(`[video-run] ${runId} — all items complete`);
  run.emitter.emit('event', { status: 'complete' });
  activeVideoRuns.delete(runId);
}

app.post('/api/run-video', (req, res) => {
  const { urls, items, language = 'he', linkType = 'regular' } = req.body;
  const runItems = normalizeRunItems({ items, urls, linkType });
  if (!runItems.length) return res.status(400).json({ error: 'No URLs provided' });

  const runId = makeSessionId('video-run');
  const emitter = new EventEmitter();
  const run = {
    runId,
    emitter,
    controller: new AbortController(),
    items: runItems,
    language,
    startedAt: new Date().toISOString(),
  };
  activeVideoRuns.set(runId, run);

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  res.write(`data: ${JSON.stringify({ status: 'started', runId })}\n\n`);
  subscribeSSE(res, emitter);

  emitter.on('event', payload => {
    if (payload.status === 'complete') {
      if (!res.destroyed) { try { res.end(); } catch {} }
    }
  });

  executeVideoRun(runId, runItems, language);
});

app.post('/api/video-runs/:runId/stop', (req, res) => {
  const run = activeVideoRuns.get(req.params.runId);
  if (!run) return res.status(404).json({ error: 'Video run not found or already completed' });
  run.controller.abort();
  res.json({ stopped: true, runId: req.params.runId });
});

/* ── Reconnect to an active run's event stream ── */
app.get('/api/runs/:runId/events', (req, res) => {
  const run = activeRuns.get(req.params.runId);
  if (!run) return res.status(404).json({ error: 'Run not found or already completed' });

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  const snapshot = run.items.map(item => {
    const state = run.itemStates.get(item.url);
    return { url: item.url, linkType: item.linkType, folderName: item.folderName, label: item.label, ...(state || { status: 'pending' }) };
  });
  res.write(`data: ${JSON.stringify({ status: 'snapshot', runId: run.runId, items: snapshot })}\n\n`);

  subscribeSSE(res, run.emitter);

  run.emitter.on('event', payload => {
    if (payload.status === 'complete') {
      if (!res.destroyed) { try { res.end(); } catch {} }
    }
  });
});

/* ── List active runs ── */
app.get('/api/runs/active', (req, res) => {
  const runs = [];
  for (const [runId, run] of activeRuns) {
    const items = run.items.map(item => {
      const state = run.itemStates.get(item.url);
      return { url: item.url, linkType: item.linkType, folderName: item.folderName, label: item.label, ...(state || { status: 'pending' }) };
    });
    runs.push({ runId, language: run.language, startedAt: run.startedAt, items });
  }
  res.json({ runs });
});

/* ── Stop a run ── */
app.post('/api/runs/:runId/stop', (req, res) => {
  const run = activeRuns.get(req.params.runId);
  if (!run) return res.status(404).json({ error: 'Run not found or already completed' });

  run.controller.abort();
  for (const sid of run.sessionIds) {
    const session = activeSessions.get(sid);
    if (session) session.controller.abort();
  }
  res.json({ stopped: true, runId: req.params.runId });
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

/* ── Save all tutorial PDFs + Videos + Excel to SharePoint ── */

function callBackendJson(apiPath, payload) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(`${PYTHON_BACKEND_URL}${apiPath}`);
    const body = JSON.stringify(payload);

    const request = http.request(
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
      (resp) => {
        const chunks = [];
        resp.on('data', (chunk) => chunks.push(chunk));
        resp.on('end', () => {
          const raw = Buffer.concat(chunks).toString();
          let parsedBody = null;
          try { parsedBody = JSON.parse(raw); } catch { /* ignore */ }
          if (resp.statusCode >= 400) {
            const message = parsedBody?.error || raw || `HTTP ${resp.statusCode}`;
            return reject({ statusCode: resp.statusCode, message });
          }
          if (!parsedBody) {
            return reject({ statusCode: 502, message: `Invalid JSON from backend: ${raw.slice(0, 200)}` });
          }
          resolve(parsedBody);
        });
      },
    );

    request.on('error', (err) => reject({ statusCode: 502, message: err.message }));
    request.on('timeout', () => {
      request.destroy();
      reject({ statusCode: 504, message: 'SharePoint export timed out' });
    });

    request.write(body);
    request.end();
  });
}

function exportFolderName(type = 'Presentations', date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  const stamp = `${pad(date.getDate())}-${pad(date.getMonth() + 1)}-${date.getFullYear()}`;
  return `${type} ${stamp}`;
}

function buildExcelBuffer(tutorials, foldersData) {
  const folderMap = new Map();
  for (const folder of (foldersData?.folders || [])) {
    for (const page of folder.pages) {
      folderMap.set(page.url, folder.name);
    }
  }

  const rows = tutorials.map((t) => ({
    Folder: folderMap.get(t.url || t.confluenceUrl) || '',
    Title: t.label || 'Untitled',
    Language: t.language ? t.language.toUpperCase() : '-',
    'Confluence URL': t.confluenceUrl || t.url || '',
    'Gamma URL': t.gammaUrl || '',
    'PDF URL': t.sharepointUrl || '',
    'Video URL': t.videoUrl || '',
  }));

  const ws = XLSX.utils.json_to_sheet(rows);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Tutorials');
  return XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' });
}

app.post('/api/export-sharepoint', async (req, res) => {
  const list = loadTutorials();

  const pdfSeen = new Set();
  const pdfUrls = [];
  const videoSeen = new Set();
  const videoUrls = [];

  for (const t of list) {
    if (t.sharepointUrl && !pdfSeen.has(t.sharepointUrl)) {
      pdfSeen.add(t.sharepointUrl);
      pdfUrls.push(t.sharepointUrl);
    }
    if (t.videoUrl && !videoSeen.has(t.videoUrl)) {
      videoSeen.add(t.videoUrl);
      videoUrls.push(t.videoUrl);
    }
  }

  if (!pdfUrls.length && !videoUrls.length) {
    return res.status(400).json({ error: 'No presentations or videos to export.' });
  }

  const now = new Date();
  const pdfFolder = exportFolderName('Presentations', now);
  const videoFolder = exportFolderName('Videos', now);

  const result = {
    pdfFolderName: null, pdfFolderUrl: null, pdfCount: 0,
    videoFolderName: null, videoFolderUrl: null, videoCount: 0,
    excelUrl: null,
    errors: [],
  };

  // 1. Sync PDFs
  if (pdfUrls.length) {
    try {
      const pdfResult = await callBackendJson('/api/sync-export-folder', {
        prefix: 'Presentations',
        folder_name: pdfFolder,
        current_urls: pdfUrls,
      });
      result.pdfFolderName = pdfFolder;
      result.pdfFolderUrl = pdfResult.folderUrl || null;
      result.pdfCount = pdfResult.uploaded || 0;
    } catch (err) {
      result.errors.push(`PDFs: ${err?.message || 'failed'}`);
    }
  }

  // 2. Sync Videos
  if (videoUrls.length) {
    try {
      const vidResult = await callBackendJson('/api/sync-export-folder', {
        prefix: 'Videos',
        folder_name: videoFolder,
        current_urls: videoUrls,
      });
      result.videoFolderName = videoFolder;
      result.videoFolderUrl = vidResult.folderUrl || null;
      result.videoCount = vidResult.uploaded || 0;
    } catch (err) {
      result.errors.push(`Videos: ${err?.message || 'failed'}`);
    }
  }

  // 3. Upload Excel
  try {
    const foldersData = loadFoldersRaw();
    const excelBuf = buildExcelBuffer(list, foldersData);
    const excelResult = await callBackendJson('/api/upload-excel-to-sharepoint', {
      file_name: 'tutorials-library.xlsx',
      file_base64: excelBuf.toString('base64'),
    });
    result.excelUrl = excelResult.webUrl || null;
  } catch (err) {
    result.errors.push(`Excel: ${err?.message || 'failed'}`);
  }

  if (result.errors.length && !result.pdfCount && !result.videoCount && !result.excelUrl) {
    return res.status(500).json({ error: result.errors.join('; ') });
  }

  res.json(result);
});

app.delete('/api/tutorials/:id', (req, res) => {
  const list = loadTutorials();
  const next = list.filter(t => t.id !== req.params.id);
  if (next.length === list.length) return res.status(404).json({ error: 'Tutorial not found' });

  saveTutorials(next);
  res.json({ deleted: true, tutorials: next });
});

app.post('/api/tutorials/:id/regenerate', (req, res) => {
  const list = loadTutorials();
  const tutorial = list.find(t => t.id === req.params.id);
  if (!tutorial) return res.status(404).json({ error: 'Tutorial not found' });

  const { language = 'he', linkType = 'regular' } = req.body || {};
  const runId = makeSessionId('regen');
  const emitter = new EventEmitter();
  const run = {
    runId,
    emitter,
    controller: new AbortController(),
    items: [{ url: tutorial.url, linkType }],
    language,
    itemStates: new Map(),
    sessionIds: new Set(),
    startedAt: new Date().toISOString(),
  };
  activeRuns.set(runId, run);

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.flushHeaders();

  res.write(`data: ${JSON.stringify({ status: 'started', runId })}\n\n`);
  subscribeSSE(res, emitter);

  emitter.on('event', payload => {
    if (payload.status === 'complete') {
      if (!res.destroyed) { try { res.end(); } catch {} }
    }
  });

  executeRegenerate(runId, tutorial.url, language, linkType);
});

app.get('/generate', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'generate.html'));
});

migrateCorrruptedLanguageRecords();

app.listen(PORT, () => {
  console.log(`Running at http://localhost:${PORT}`);
});
