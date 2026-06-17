let foldersData = { folders: [], lastSyncedAt: null };
let selected = new Set(); // stores "folderId:pageIndex" keys
let language = 'he';
let currentRunId = null;
let isRunning = false;
let collapsedFolders = new Set();
let knownFolderIds = new Set();
let rowIds = {};

// Video generation state (independent from presentation run)
let isVideoRunning = false;
let currentVideoRunId = null;
let videoRowIds = {};

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('addUrlBtn').addEventListener('click', addUrl);
  document.getElementById('runBtn').addEventListener('click', runSelected);
  document.getElementById('runVideoBtn').addEventListener('click', runVideoSelected);
  document.getElementById('stopBtn').addEventListener('click', stopGeneration);
  document.getElementById('stopVideoBtn').addEventListener('click', stopVideoGeneration);
  document.getElementById('selectAllBtn').addEventListener('click', toggleSelectAll);
  document.getElementById('newUrlBtn').addEventListener('click', openAddModal);
  document.getElementById('urlInput').addEventListener('keydown', e => { if (e.key === 'Enter') addUrl(); });
  document.getElementById('labelInput').addEventListener('keydown', e => { if (e.key === 'Enter') addUrl(); });

  document.getElementById('sourceSearch').addEventListener('input', filterSources);

  const modal = document.getElementById('addPageModal');
  modal.addEventListener('click', e => { if (e.target === modal) closeAddModal(); });

  bindSegment('langSegment', value => { language = value; });
  loadFolders();
  checkActiveRuns();
});

function bindSegment(id, onChange) {
  const segment = document.getElementById(id);
  segment.querySelectorAll('button').forEach(button => {
    button.addEventListener('click', () => {
      segment.querySelectorAll('button').forEach(item => item.classList.remove('active'));
      button.classList.add('active');
      onChange(button.dataset.value);
    });
  });
}

function pageKey(folderId, pageIndex) {
  return `${folderId}:${pageIndex}`;
}

function getAllPageKeys() {
  const keys = [];
  for (const folder of foldersData.folders) {
    folder.pages.forEach((_, i) => keys.push(pageKey(folder.id, i)));
  }
  return keys;
}

function getTotalPages() {
  return foldersData.folders.reduce((sum, f) => sum + f.pages.length, 0);
}

/* ── Data loading ── */
async function loadFolders() {
  try {
    const res = await fetch('/api/folders');
    foldersData = await res.json();
    syncCollapsedFolders(foldersData.folders);
  } catch {
    foldersData = { folders: [], lastSyncedAt: null };
  }
  const allKeys = new Set(getAllPageKeys());
  selected = new Set([...selected].filter(k => allKeys.has(k)));
  renderFolders();
  renderFolderSelect();
}

function syncCollapsedFolders(folders = []) {
  for (const folder of folders) {
    if (!knownFolderIds.has(folder.id)) {
      knownFolderIds.add(folder.id);
      collapsedFolders.add(folder.id);
    }
  }
}

function renderFolderSelect() {
  const select = document.getElementById('folderSelect');
  const options = foldersData.folders.map(f =>
    `<option value="${esc(f.id)}">${esc(f.name)}</option>`
  ).join('');

  select.innerHTML = options + '<option value="__new__">+ New Folder</option>';

  select.onchange = () => {
    const newFolderRow = document.getElementById('newFolderRow');
    newFolderRow.style.display = select.value === '__new__' ? '' : 'none';
    if (select.value === '__new__') document.getElementById('newFolderInput').focus();
  };
}

/* ── Render folder tree ── */
function renderFolders() {
  const container = document.getElementById('folderList');
  const summary = document.getElementById('sourceSummary');
  const selectAllBtn = document.getElementById('selectAllBtn');
  const total = getTotalPages();

  summary.textContent = `${total} sources · ${selected.size} selected`;
  selectAllBtn.textContent = selected.size === total && total ? 'Clear selection' : 'Select all';

  if (!foldersData.folders.length) {
    container.innerHTML = `
      <div class="empty-state">
        <strong>No source URLs yet</strong>
        Import from Confluence or add pages manually to start generating tutorials.
      </div>`;
    syncRunButton();
    return;
  }

  container.innerHTML = foldersData.folders.map(folder => {
    const isCollapsed = collapsedFolders.has(folder.id);
    const folderPageKeys = folder.pages.map((_, i) => pageKey(folder.id, i));
    const selectedInFolder = folderPageKeys.filter(k => selected.has(k)).length;
    const allSelected = selectedInFolder === folder.pages.length && folder.pages.length > 0;

    return `
      <div class="folder-group${isCollapsed ? ' collapsed' : ''}">
        <div class="folder-header" onclick="toggleFolder('${esc(folder.id)}')">
          <div class="folder-header-left">
            <svg class="folder-chevron" width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <svg class="folder-icon" width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-6l-2-2H5a2 2 0 0 0-2 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <span class="folder-name">${esc(folder.name)}</span>
            <span class="folder-count">${folder.pages.length} page${folder.pages.length !== 1 ? 's' : ''}</span>
          </div>
          <div class="folder-actions" onclick="event.stopPropagation()">
            <button class="btn btn-ghost btn-sm" onclick="toggleFolderSelection('${esc(folder.id)}')" title="${allSelected ? 'Deselect all' : 'Select all'}">
              ${allSelected ? 'Deselect' : 'Select All'}
            </button>
            <button class="btn btn-ghost btn-sm" onclick="runFolder('${esc(folder.id)}')" title="Run all pages in this folder">
              <svg width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z" fill="currentColor"/></svg>
              Run
            </button>
            ${!folder.confluencePageId && folder.id !== 'folder-uncategorized' ? `<button class="btn btn-ghost btn-sm" onclick="deleteFolder('${esc(folder.id)}')" title="Delete folder">
              <svg width="13" height="13" fill="none" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
            </button>` : ''}
          </div>
        </div>
        <div class="folder-pages">
          ${folder.pages.length === 0 ? '<div class="empty-folder">No pages in this folder</div>' :
            folder.pages.map((page, i) => {
              const key = pageKey(folder.id, i);
              const checked = selected.has(key);
              return `
                <article class="url-item">
                  <button class="check ${checked ? 'checked' : ''}" onclick="togglePage('${key}')" aria-label="Select ${esc(page.label)}"></button>
                  <div class="url-copy">
                    <strong>${esc(page.label || `Page ${i + 1}`)}</strong>
                    <span title="${esc(page.url)}">${esc(page.url)}</span>
                  </div>
                  <button class="icon-btn" onclick="deletePage('${esc(folder.id)}', ${i})" title="Remove page">
                    <svg width="15" height="15" fill="none" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                  </button>
                </article>`;
            }).join('')}
        </div>
      </div>`;
  }).join('');

  syncRunButton();
}

/* ── Search / Filter ── */
function filterSources() {
  const query = document.getElementById('sourceSearch').value.toLowerCase().trim();
  const groups = document.querySelectorAll('.folder-group');

  groups.forEach(group => {
    const items = group.querySelectorAll('.url-item');
    let visibleCount = 0;

    items.forEach(item => {
      const text = item.textContent.toLowerCase();
      const match = !query || text.includes(query);
      item.style.display = match ? '' : 'none';
      if (match) visibleCount++;
    });

    group.style.display = visibleCount || !query ? '' : 'none';
  });
}

/* ── Interactions ── */
function toggleFolder(folderId) {
  if (collapsedFolders.has(folderId)) {
    collapsedFolders.delete(folderId);
  } else {
    collapsedFolders.add(folderId);
  }
  renderFolders();
}

function togglePage(key) {
  selected.has(key) ? selected.delete(key) : selected.add(key);
  renderFolders();
}

function toggleFolderSelection(folderId) {
  const folder = foldersData.folders.find(f => f.id === folderId);
  if (!folder) return;
  const keys = folder.pages.map((_, i) => pageKey(folderId, i));
  const allSelected = keys.every(k => selected.has(k));

  if (allSelected) {
    keys.forEach(k => selected.delete(k));
  } else {
    keys.forEach(k => selected.add(k));
  }
  renderFolders();
}

function toggleSelectAll() {
  const allKeys = getAllPageKeys();
  if (selected.size === allKeys.length && allKeys.length) {
    selected.clear();
  } else {
    allKeys.forEach(k => selected.add(k));
  }
  renderFolders();
}

function syncRunButton() {
  document.getElementById('runBtn').disabled = selected.size === 0 || isRunning;
  document.getElementById('stopBtn').classList.toggle('hidden', !isRunning);
  document.getElementById('runVideoBtn').disabled = selected.size === 0 || isVideoRunning;
  document.getElementById('stopVideoBtn').classList.toggle('hidden', !isVideoRunning);
}

/* ── Modal ── */
function openAddModal() {
  document.getElementById('addPageModal').classList.add('open');
  document.getElementById('urlInput').focus();
}

function closeAddModal() {
  document.getElementById('addPageModal').classList.remove('open');
}

/* ── Add / Delete ── */
async function addUrl() {
  const urlInput = document.getElementById('urlInput');
  const labelInput = document.getElementById('labelInput');
  const folderSelect = document.getElementById('folderSelect');
  const newFolderInput = document.getElementById('newFolderInput');

  const url = urlInput.value.trim();
  const label = labelInput.value.trim();
  let folderId = folderSelect.value;

  if (!url) { urlInput.focus(); return; }

  let newFolderName = null;
  if (folderId === '__new__') {
    newFolderName = newFolderInput.value.trim();
    if (!newFolderName) { newFolderInput.focus(); return; }
    folderId = `folder-${newFolderName.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  }

  await fetch(`/api/folders/${encodeURIComponent(folderId)}/urls`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, label, newFolderName }),
  });

  urlInput.value = '';
  labelInput.value = '';
  newFolderInput.value = '';
  document.getElementById('newFolderRow').style.display = 'none';
  closeAddModal();
  await loadFolders();
}

async function deletePage(folderId, pageIndex) {
  const key = pageKey(folderId, pageIndex);
  selected.delete(key);

  await fetch(`/api/folders/${encodeURIComponent(folderId)}/urls/${pageIndex}`, { method: 'DELETE' });
  await loadFolders();
}

async function deleteFolder(folderId) {
  const folder = foldersData.folders.find(f => f.id === folderId);
  if (!folder) return;
  if (folder.pages.length && !confirm(`Delete folder "${folder.name}" and its ${folder.pages.length} pages?`)) return;

  folder.pages.forEach((_, i) => selected.delete(pageKey(folderId, i)));
  await fetch(`/api/folders/${encodeURIComponent(folderId)}`, { method: 'DELETE' });
  await loadFolders();
}

/* ── Check for active runs on page load ── */
async function checkActiveRuns() {
  try {
    const res = await fetch('/api/runs/active');
    const data = await res.json();
    if (!data.runs || !data.runs.length) return;

    const run = data.runs[0];
    currentRunId = run.runId;
    isRunning = true;
    rowIds = {};

    const results = document.getElementById('results');
    const notice = document.getElementById('successNotice');
    notice.classList.remove('visible');
    results.classList.add('visible');
    results.innerHTML = '';

    run.items.forEach((item, index) => {
      const id = `result-${index}`;
      rowIds[item.url] = id;

      let className = 'running';
      let msg = 'Pending...';
      if (item.status === 'done') { className = 'done'; msg = 'Completed'; }
      else if (item.status === 'error') { className = 'error'; msg = item.error || 'Failed'; }
      else if (item.status === 'stopped') { className = 'error'; msg = 'Stopped'; }
      else if (item.status === 'running') { msg = 'Processing...'; }

      results.insertAdjacentHTML('beforeend', `
        <div id="${id}" class="result-row ${className}" data-session-id="${esc(item.sessionId || '')}">
          <span class="result-dot"></span>
          <span class="result-url" title="${esc(item.url)}">${esc(item.url)}</span>
          <span class="result-msg">${msg}</span>
          <button class="session-stop" type="button" onclick="stopSession('${id}')" ${item.status === 'running' ? '' : 'disabled'}>Stop</button>
        </div>`);
    });

    syncRunButton();
    connectToRunEvents(run.runId);
  } catch {}
}

/* ── Connect to an active run's SSE event stream ── */
function connectToRunEvents(runId) {
  const controller = new AbortController();

  fetch(`/api/runs/${encodeURIComponent(runId)}/events`, { signal: controller.signal })
    .then(response => {
      const reader = response.body.getReader();
      let buffer = '';

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) {
            finishRun();
            return;
          }
          buffer = parseServerSentEvents(buffer, value, handleRunMessage);
          pump();
        }).catch(() => {
          finishRun();
        });
      }

      pump();
    })
    .catch(() => {
      finishRun();
    });
}

function handleRunMessage(message) {
  if (message.status === 'snapshot') return;

  if (message.status === 'complete') {
    finishRun();
    return;
  }

  if (message.status === 'started') return;

  const row = document.getElementById(rowIds[message.url]);
  if (!row) return;

  if (message.sessionId) {
    row.dataset.sessionId = message.sessionId;
    const stopBtn = row.querySelector('.session-stop');
    if (stopBtn && message.status === 'running') stopBtn.disabled = false;
  }

  if (message.status === 'running') {
    row.className = 'result-row running';
    row.querySelector('.result-msg').textContent = 'Processing...';
  }

  if (message.status === 'done') {
    row.className = 'result-row done';
    row.querySelector('.result-msg').textContent = 'Completed';
    disableSessionStop(row);
  }

  if (message.status === 'error') {
    row.className = 'result-row error';
    row.querySelector('.result-msg').textContent = message.error || 'Failed';
    disableSessionStop(row);
  }

  if (message.status === 'stopped') {
    row.className = 'result-row error';
    row.querySelector('.result-msg').textContent = 'Stopped';
    disableSessionStop(row);
  }
}

function finishRun() {
  if (!isRunning) return;
  const notice = document.getElementById('successNotice');
  const hasSuccess = document.querySelectorAll('.result-row.done').length > 0;
  if (hasSuccess) notice.classList.add('visible');
  isRunning = false;
  currentRunId = null;
  syncRunButton();
}

/* ── Run generation ── */
function getLinkTypeForFolder(folderName) {
  return String(folderName).toLowerCase().includes('admin') ? 'admin' : 'regular';
}

function getSelectedItems() {
  const items = [];
  for (const key of selected) {
    const [folderId, indexStr] = key.split(':');
    const folder = foldersData.folders.find(f => f.id === folderId);
    const page = folder?.pages[parseInt(indexStr)];
    if (folder && page) {
      items.push({
        url: page.url,
        folderId: folder.id,
        folderName: folder.name,
        label: page.label || '',
        linkType: getLinkTypeForFolder(folder.name),
      });
    }
  }
  return items;
}

function getSelectedLinkTypes(items) {
  return [...new Set(items.map(item => item.linkType))];
}

function runFolder(folderId) {
  const folder = foldersData.folders.find(f => f.id === folderId);
  if (!folder || !folder.pages.length) return;

  const folderKeys = folder.pages.map((_, i) => pageKey(folderId, i));
  const selectedInFolder = folderKeys.filter(k => selected.has(k));

  if (selectedInFolder.length === 0) {
    folderKeys.forEach(k => selected.add(k));
  } else {
    selected = new Set(selectedInFolder);
  }

  renderFolders();
  runSelected();
}

async function runSelected() {
  const toRun = getSelectedItems();
  if (!toRun.length || isRunning) return;

  const results = document.getElementById('results');
  const notice = document.getElementById('successNotice');
  rowIds = {};

  notice.classList.remove('visible');
  results.classList.add('visible');
  results.innerHTML = '';

  toRun.forEach((item, index) => {
    const url = item.url;
    const id = `result-${index}`;
    rowIds[url] = id;
    results.insertAdjacentHTML('beforeend', `
      <div id="${id}" class="result-row running">
        <span class="result-dot"></span>
        <span class="result-url" title="${esc(url)}">${esc(url)}</span>
        <span class="result-msg">Pending...</span>
        <button class="session-stop" type="button" onclick="stopSession('${id}')" disabled>Stop</button>
      </div>`);
  });

  isRunning = true;
  syncRunButton();

  if (document.getElementById('updateRoutesCheck').checked) {
    const discoverProceeded = await runRouteDiscovery(getSelectedLinkTypes(toRun));
    if (!discoverProceeded) {
      isRunning = false;
      syncRunButton();
      return;
    }
  }

  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: toRun, language }),
    });

    const reader = response.body.getReader();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = parseServerSentEvents(buffer, value, message => {
        if (message.status === 'started' && message.runId) {
          currentRunId = message.runId;
          return;
        }
        handleRunMessage(message);
      });
    }

    finishRun();
  } catch (error) {
    if (currentRunId) return;

    results.insertAdjacentHTML('beforeend', `
      <div class="result-row error">
        <span class="result-dot"></span>
        <span class="result-url">Generation failed</span>
        <span class="result-msg">${esc(error.message)}</span>
      </div>`);
    isRunning = false;
    currentRunId = null;
    syncRunButton();
  }
}

async function runRouteDiscovery(linkTypes) {
  const results = document.getElementById('results');
  const rowId = 'route-discovery-row';
  const typeLabel = linkTypes.length > 1 ? 'regular and admin' : linkTypes[0];
  results.insertAdjacentHTML('afterbegin', `
    <div id="${rowId}" class="result-row running">
      <span class="result-dot"></span>
      <span class="result-url">Updating product routes</span>
      <span class="result-msg">Crawling ${esc(typeLabel)} routes...</span>
    </div>`);

  const row = document.getElementById(rowId);

  try {
    const res = await fetch('/api/discover-routes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ linkTypes }),
    });

    const data = await res.json();

    if (!res.ok) {
      row.className = 'result-row error';
      row.querySelector('.result-msg').textContent = data.error || 'Route discovery failed';
      return true;
    }

    const added = Array.isArray(data.added) ? data.added.length : 0;
    row.className = 'result-row done';
    row.querySelector('.result-msg').textContent =
      added > 0 ? `Found ${added} new route${added !== 1 ? 's' : ''}` : 'Routes up to date';
    return true;
  } catch (error) {
    row.className = 'result-row error';
    row.querySelector('.result-msg').textContent = error.message || 'Route discovery failed';
    return true;
  }
}

async function stopGeneration() {
  if (!currentRunId) return;

  try {
    await fetch(`/api/runs/${encodeURIComponent(currentRunId)}/stop`, { method: 'POST' });
  } catch {}
}

async function stopSession(rowId) {
  const row = document.getElementById(rowId);
  const sessionId = row?.dataset.sessionId;
  if (!row || !sessionId) return;

  const stopBtn = row.querySelector('.session-stop');
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.textContent = 'Stopping...';
  }

  try {
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/stop`, { method: 'POST' });
  } catch {
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.textContent = 'Stop';
    }
  }
}

function disableSessionStop(row) {
  const stopBtn = row.querySelector('.session-stop');
  if (!stopBtn) return;
  stopBtn.disabled = true;
  stopBtn.textContent = 'Stop';
}

/* ══════════════════════════════════════════════════
   Video generation (separate flow)
══════════════════════════════════════════════════ */

async function runVideoSelected() {
  const toRun = getSelectedItems();
  if (!toRun.length || isVideoRunning) return;

  const results = document.getElementById('results');
  const notice = document.getElementById('successNotice');
  videoRowIds = {};

  notice.classList.remove('visible');
  results.classList.add('visible');

  // Append video rows after any existing rows (presentation run may be visible)
  toRun.forEach((item, index) => {
    const url = item.url;
    const id = `video-result-${index}`;
    videoRowIds[url] = id;
    results.insertAdjacentHTML('beforeend', `
      <div id="${id}" class="result-row running video-row">
        <span class="result-dot"></span>
        <span class="result-label video-tag">Video</span>
        <span class="result-url" title="${esc(url)}">${esc(url)}</span>
        <span class="result-msg">Pending...</span>
      </div>`);
  });

  isVideoRunning = true;
  syncRunButton();

  try {
    const response = await fetch('/api/run-video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: toRun, language }),
    });

    const reader = response.body.getReader();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = parseServerSentEvents(buffer, value, message => {
        if (message.status === 'started' && message.runId) {
          currentVideoRunId = message.runId;
          return;
        }
        handleVideoRunMessage(message);
      });
    }

    finishVideoRun();
  } catch (error) {
    results.insertAdjacentHTML('beforeend', `
      <div class="result-row error">
        <span class="result-dot"></span>
        <span class="result-label video-tag">Video</span>
        <span class="result-url">Video generation failed</span>
        <span class="result-msg">${esc(error.message)}</span>
      </div>`);
    isVideoRunning = false;
    currentVideoRunId = null;
    syncRunButton();
  }
}

const VIDEO_STAGE_LABELS = {
  confluence: 'Fetching Confluence…',
  script:     'Generating video script…',
  record:     'Recording browser session…',
  render:     'Rendering MP4…',
  upload:     'Uploading to SharePoint…',
  complete:   'Done',
};

function handleVideoRunMessage(message) {
  if (message.status === 'snapshot' || message.status === 'started') return;

  if (message.status === 'complete') {
    finishVideoRun();
    return;
  }

  const row = document.getElementById(videoRowIds[message.url]);
  if (!row) return;
  const msgEl = row.querySelector('.result-msg');

  if (message.status === 'running') {
    row.className = 'result-row running video-row';
    msgEl.textContent = 'Starting…';
  }

  if (message.status === 'stage') {
    row.className = 'result-row running video-row';
    const label = VIDEO_STAGE_LABELS[message.stage] || message.detail || 'Processing…';
    const detail = message.detail && message.detail !== label ? ` — ${message.detail}` : '';
    msgEl.textContent = label + detail;
    console.log(`[video] stage=${message.stage} (${message.stageStatus}) ${message.detail || ''}`);
  }

  if (message.status === 'done') {
    row.className = 'result-row done video-row';
    msgEl.textContent = message.video_url ? 'Done — Video ready' : 'Done';
    if (message.video_url) {
      row.insertAdjacentHTML('beforeend',
        `<a class="text-link" href="${esc(message.video_url)}" target="_blank" rel="noopener">Open Video</a>`
      );
    }
  }

  if (message.status === 'error') {
    row.className = 'result-row error video-row';
    msgEl.textContent = message.error || 'Failed';
    console.error('[video] error:', message.error);
  }
}

function finishVideoRun() {
  if (!isVideoRunning) return;
  const notice = document.getElementById('successNotice');
  const hasSuccess = document.querySelectorAll('.video-row.done').length > 0;
  if (hasSuccess) notice.classList.add('visible');
  isVideoRunning = false;
  currentVideoRunId = null;
  syncRunButton();
}

async function stopVideoGeneration() {
  if (!currentVideoRunId) return;
  try {
    await fetch(`/api/video-runs/${encodeURIComponent(currentVideoRunId)}/stop`, { method: 'POST' });
  } catch {}
}
