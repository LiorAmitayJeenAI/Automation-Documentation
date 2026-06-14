let tutorials = [];
let foldersData = { folders: [] };
let urlToTutorial = new Map();
let searchTerm = '';
let activeFilter = 'all';
let collapsedFolders = new Set();
let pollTimer = null;

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('searchInput').addEventListener('input', event => {
    searchTerm = event.target.value.trim().toLowerCase();
    renderLibrary();
  });

  document.getElementById('languageFilter').addEventListener('change', event => {
    activeFilter = event.target.value;
    renderLibrary();
  });

  document.getElementById('exportExcelBtn').addEventListener('click', exportToExcel);

  loadTutorials();
});

function exportToExcel() {
  const rows = getVisibleRows();
  if (!rows.length) return;

  const data = rows.map(({ folderName, page, tutorial }) => ({
    Folder: folderName,
    Title: page.label || 'Untitled',
    Language: tutorial.language ? tutorial.language.toUpperCase() : '-',
    'Confluence URL': page.url || '',
    'Gamma URL': tutorial.gammaUrl || '',
    'PDF URL': tutorial.sharepointUrl || '',
  }));

  const ws = XLSX.utils.json_to_sheet(data);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Tutorials');

  const date = new Date().toISOString().slice(0, 10);
  XLSX.writeFile(wb, `tutorials-library-${date}.xlsx`);
}

async function loadTutorials() {
  try {
    const [tutorialsRes, foldersRes] = await Promise.all([
      fetch('/api/tutorials'),
      fetch('/api/folders'),
    ]);
    tutorials = await tutorialsRes.json();
    foldersData = await foldersRes.json();

    urlToTutorial = new Map();
    for (const t of tutorials) {
      urlToTutorial.set(getSourceUrl(t), t);
    }
  } catch {
    tutorials = [];
  }
  renderLibrary();
  schedulePollIfNeeded();
}

function schedulePollIfNeeded() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }

  const hasActive = tutorials.some(t => {
    const s = normalizeStatus(t.status);
    return s === 'queued' || s === 'processing';
  });

  if (hasActive) {
    pollTimer = setTimeout(() => loadTutorials(), 4000);
  }
}

function rowMatchesFilters(page, tutorial, folder) {
  if (!tutorial) return false;

  const langMatches = activeFilter === 'all'
    || (tutorial.language || '').toLowerCase() === activeFilter;

  const searchable = [
    page.label,
    page.url,
    folder.name,
    tutorial.gammaUrl || '',
    tutorial.sharepointUrl || '',
  ].join(' ').toLowerCase();
  const searchMatches = !searchTerm || searchable.includes(searchTerm);

  return langMatches && searchMatches;
}

function getVisibleRows() {
  const rows = [];
  for (const folder of foldersData.folders) {
    for (const page of folder.pages) {
      const tutorial = urlToTutorial.get(page.url);
      if (rowMatchesFilters(page, tutorial, folder)) {
        rows.push({ folderName: folder.name, page, tutorial });
      }
    }
  }
  return rows;
}

function renderLibrary() {
  const container = document.getElementById('libraryFolders');
  const summary = document.getElementById('resultsSummary');

  const totalPages = foldersData.folders.reduce((s, f) => s + f.pages.length, 0);
  const generated = tutorials.filter(t => normalizeStatus(t.status) === 'up-to-date').length;

  if (!foldersData.folders.length) {
    container.innerHTML = `
      <div class="empty-state">
        <strong>No tutorials found</strong>
        Go to Generate Tutorials to create your first tutorial.
      </div>`;
    summary.textContent = '0 tutorials';
    updateExportButton(0);
    return;
  }

  let visiblePages = 0;

  container.innerHTML = foldersData.folders.map(folder => {
    const isCollapsed = collapsedFolders.has(folder.id);

    const pageRows = folder.pages.map(page => {
      const tutorial = urlToTutorial.get(page.url);
      return { page, tutorial };
    }).filter(({ page, tutorial }) => rowMatchesFilters(page, tutorial, folder));

    if (!pageRows.length) return '';
    visiblePages += pageRows.length;

    return `
      <div class="folder-group${isCollapsed ? ' collapsed' : ''}">
        <div class="folder-header" onclick="toggleLibFolder('${esc(folder.id)}')">
          <div class="folder-header-left">
            <svg class="folder-chevron" width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <svg class="folder-icon" width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-6l-2-2H5a2 2 0 0 0-2 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <span class="folder-name">${esc(folder.name)}</span>
            <span class="folder-count">${pageRows.length} tutorial${pageRows.length !== 1 ? 's' : ''}</span>
          </div>
        </div>
        <div class="folder-pages">
          ${pageRows.map(({ page, tutorial }) => {
            const status = tutorial ? statusMeta(tutorial.status) : statusMeta('pending');
            const title = page.label || 'Untitled';
            const language = tutorial?.language ? tutorial.language.toUpperCase() : '-';
            const lastGenerated = tutorial ? formatDate(tutorial.lastGeneratedAt || tutorial.lastUpdatedAt, true) : '-';
            const isProcessing = tutorial && (normalizeStatus(tutorial.status) === 'processing' || normalizeStatus(tutorial.status) === 'queued');

            return `
              <article class="lib-item">
                <div class="lib-item-main">
                  <span class="mini-icon">${esc(title.charAt(0).toUpperCase())}</span>
                  <div class="lib-item-info">
                    <strong>${esc(title)}</strong>
                    <div class="lib-item-meta">
                      ${status.className !== 'up-to-date' ? `<span class="badge ${status.className}">${status.label}</span>` : ''}
                      <span class="pill">${esc(language)}</span>
                      <span class="lib-date">${lastGenerated}</span>
                    </div>
                  </div>
                </div>
                <div class="lib-item-links">
                  ${linkHtml(page.url, 'Confluence', 'text-link sm')}
                  ${linkHtml(tutorial?.gammaUrl, 'Gamma', 'text-link sm')}
                  ${linkHtml(tutorial?.sharepointUrl, 'PDF', 'text-link sm')}
                </div>
                <div class="row-actions">
                  ${tutorial ? `
                    <button class="icon-btn" onclick="showHistory('${tutorial.id}')" title="View generation history">
                      <svg width="15" height="15" fill="none" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v6h6M12 7v5l3 2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    </button>
                    <button class="icon-btn" onclick="regenerateTutorial('${tutorial.id}')" ${isProcessing ? 'disabled' : ''} title="Regenerate tutorial">
                      <svg width="15" height="15" fill="none" viewBox="0 0 24 24"><path d="M23 4v6h-6M20.5 15A9 9 0 1 1 18 5.7L23 10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    </button>
                    <button class="icon-btn danger" onclick="deleteTutorial('${tutorial.id}')" ${isProcessing ? 'disabled' : ''} title="Delete from library">
                      <svg width="15" height="15" fill="none" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    </button>
                  ` : ''}
                </div>
              </article>`;
          }).join('')}
        </div>
      </div>`;
  }).join('');

  summary.textContent = `${visiblePages} pages · ${generated} generated`;
  updateExportButton(visiblePages);
}

function updateExportButton(visibleCount) {
  const btn = document.getElementById('exportExcelBtn');
  if (btn) btn.disabled = visibleCount === 0;
}

function toggleLibFolder(folderId) {
  if (collapsedFolders.has(folderId)) {
    collapsedFolders.delete(folderId);
  } else {
    collapsedFolders.add(folderId);
  }
  renderLibrary();
}

async function deleteTutorial(id) {
  const tutorial = tutorials.find(item => item.id === id);
  if (!tutorial) return;

  const ok = confirm(`Delete "${getTutorialTitle(tutorial)}" from the library?`);
  if (!ok) return;

  await fetch(`/api/tutorials/${id}`, { method: 'DELETE' });
  await loadTutorials();
}

async function regenerateTutorial(id) {
  const tutorial = tutorials.find(item => item.id === id);
  if (!tutorial) return;

  tutorial.status = 'running';
  renderLibrary();

  try {
    const response = await fetch(`/api/tutorials/${id}/regenerate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: tutorial.language || 'he', linkType: 'regular' }),
    });

    const reader = response.body.getReader();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = parseServerSentEvents(buffer, value, message => {
        if (message.status === 'done' || message.status === 'error') {
          loadTutorials();
        }
      });
    }
  } catch {
    await loadTutorials();
  }
}

function showHistory(id) {
  const tutorial = tutorials.find(item => item.id === id);
  if (!tutorial) return;

  const modal = document.getElementById('historyModal');
  const title = document.getElementById('historyTitle');
  const subtitle = document.getElementById('historySubtitle');
  const list = document.getElementById('historyList');
  const history = Array.isArray(tutorial.history) ? tutorial.history : [];

  title.textContent = 'Generation History';
  subtitle.textContent = getTutorialTitle(tutorial);

  if (!history.length) {
    list.innerHTML = `
      <div class="empty-state">
        <strong>No history yet</strong>
        Future runs will be recorded here.
      </div>`;
  } else {
    list.innerHTML = history.map(item => {
      const status = statusMeta(item.status);
      return `
        <div class="history-item">
          <strong>${status.className !== 'up-to-date' ? `<span class="badge ${status.className}">${status.label}</span>` : ''}</strong>
          <span>${formatDate(item.timestamp, true)}${item.language ? ` · ${esc(item.language.toUpperCase())}` : ''}</span>
          <span>${item.sessionId ? `Session: ${esc(item.sessionId)}` : 'Session: -'}</span>
          <span>${item.gammaUrl ? `Gamma: ${esc(item.gammaUrl)}` : 'Gamma: -'}</span>
          <span>${item.sharepointUrl ? `PDF: ${esc(item.sharepointUrl)}` : 'PDF: -'}</span>
          ${item.error ? `<span>Error: ${esc(item.error)}</span>` : ''}
        </div>`;
    }).join('');
  }

  modal.classList.add('open');
}

function closeHistory(event) {
  const modal = document.getElementById('historyModal');
  if (!event || event.target === modal) {
    modal.classList.remove('open');
  }
}
