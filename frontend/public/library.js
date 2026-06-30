let tutorials = [];
let foldersData = { folders: [] };
let urlToTutorials = new Map();
let searchTerm = '';
let activeFilter = 'all';
let collapsedFolders = new Set();
let knownFolderIds = new Set();
let expandedPartId = null;
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

  setupExportMenu();

  loadTutorials();
});

function setupExportMenu() {
  const openModalBtn = document.getElementById('openSharepointModalBtn');
  if (openModalBtn) {
    openModalBtn.addEventListener('click', () => openSharepointModal());
  }

  setupSharepointModal();
}

function setupSharepointModal() {
  const modal = document.getElementById('sharepointModal');
  const saveBtn = document.getElementById('spSaveBtn');
  if (!modal) return;

  const langBoxes = modal.querySelectorAll('input[name="spLang"]');
  const typeBoxes = modal.querySelectorAll('input[name="spType"]');

  const updateSaveBtn = () => {
    const hasLang = Array.from(langBoxes).some(cb => cb.checked);
    const hasType = Array.from(typeBoxes).some(cb => cb.checked);
    saveBtn.disabled = !(hasLang && hasType);
  };

  langBoxes.forEach(cb => cb.addEventListener('change', updateSaveBtn));
  typeBoxes.forEach(cb => cb.addEventListener('change', updateSaveBtn));

  saveBtn.addEventListener('click', () => {
    const languages = Array.from(langBoxes).filter(cb => cb.checked).map(cb => cb.value);
    const types = Array.from(typeBoxes).filter(cb => cb.checked).map(cb => cb.value);
    saveToSharePoint(languages, types, saveBtn);
  });
}

function openSharepointModal() {
  const modal = document.getElementById('sharepointModal');
  modal.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = false; });
  document.getElementById('spSaveBtn').disabled = true;
  modal.classList.add('open');
}

function closeSharepointModal(event) {
  const modal = document.getElementById('sharepointModal');
  if (!event || event.target === modal) {
    modal.classList.remove('open');
  }
}

async function saveToSharePoint(languages, types, triggerBtn) {
  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.classList.add('loading');
  }

  try {
    const res = await fetch('/api/export-sharepoint', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ languages, types }),
    });
    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      alert(data.error || 'Failed to save to SharePoint.');
      return;
    }

    const lines = [];
    for (const folder of (data.folders || [])) {
      const typeLabel = folder.type === 'pdf' ? 'presentation' : 'video';
      const plural = folder.count !== 1 ? 's' : '';
      lines.push(`Saved ${folder.count} ${typeLabel}${plural} to "${folder.folderName}".`);
      if (folder.folderUrl) lines.push(folder.folderUrl);
    }
    if (data.errors?.length) lines.push(`\nWarnings: ${data.errors.join('; ')}`);
    if (!lines.length) lines.push('Nothing was exported.');
    alert(lines.join('\n'));

    closeSharepointModal();
    await loadTutorials();
  } catch (err) {
    alert('Failed to save to SharePoint. Check the server logs.');
  } finally {
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.classList.remove('loading');
    }
  }
}

function exportToExcel() {
  const rows = getVisibleRows();
  if (!rows.length) return;

  const data = rows.map(({ folderName, page, tutorial }) => ({
    Folder: folderName,
    Title: page.label || 'Untitled',
    Language: tutorial.language ? tutorial.language.toUpperCase() : '-',
    'Confluence URL': page.url || '',
    'Gamma URL': tutorial.gammaUrl || '',
    'PDF URL': tutorial.exportSharepointUrl || tutorial.sharepointUrl || '',
    'Video URL': tutorial.exportVideoUrl || tutorial.videoUrl || '',
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
    syncCollapsedFolders(foldersData.folders);

    urlToTutorials = new Map();
    for (const t of tutorials) {
      const key = getSourceUrl(t);
      if (!urlToTutorials.has(key)) urlToTutorials.set(key, []);
      urlToTutorials.get(key).push(t);
    }
  } catch {
    tutorials = [];
  }
  renderLibrary();
  schedulePollIfNeeded();
}

function syncCollapsedFolders(folders = []) {
  for (const folder of folders) {
    if (!knownFolderIds.has(folder.id)) {
      knownFolderIds.add(folder.id);
      collapsedFolders.add(folder.id);
    }
  }
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
    tutorial.exportSharepointUrl || tutorial.sharepointUrl || '',
  ].join(' ').toLowerCase();
  const searchMatches = !searchTerm || searchable.includes(searchTerm);

  return langMatches && searchMatches;
}

function getVisibleRows() {
  const rows = [];
  for (const folder of foldersData.folders) {
    for (const page of folder.pages) {
      const pageTutorials = urlToTutorials.get(page.url) || [];
      for (const tutorial of pageTutorials) {
        if (rowMatchesFilters(page, tutorial, folder)) {
          rows.push({ folderName: folder.name, page, tutorial });
        }
      }
    }
  }
  return rows;
}

function getPartKey(folder, page) {
  return `${folder.id}:${page.url}`;
}

function jsString(value) {
  return String(value || '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r');
}

function parsePartLabel(title) {
  const match = String(title || '').match(/^Part\s*(\d+)\s*[-–—]?\s*(.*)$/i);
  if (!match) {
    return {
      badge: String(title || 'U').trim().charAt(0).toUpperCase() || 'U',
      title: title || 'Untitled',
      heading: title || 'Untitled',
    };
  }

  return {
    badge: match[1],
    title: match[2].trim() || `Part ${match[1]}`,
    heading: `Part ${match[1]} - ${match[2].trim() || `Part ${match[1]}`}`,
  };
}

function getLanguageMeta(language) {
  const normalized = (language || '').toLowerCase();
  const map = {
    en: { code: 'en', label: 'English' },
    he: { code: 'he', label: 'Hebrew' },
  };

  return map[normalized] || {
    code: normalized || 'unknown',
    label: normalized ? normalized.toUpperCase() : 'Unknown',
  };
}

function getLanguageOrder(languageKeys) {
  const preferred = activeFilter === 'all' ? ['en', 'he'] : [activeFilter];
  const extras = languageKeys.filter(lang => !preferred.includes(lang)).sort();
  return [...preferred, ...extras];
}

function isTutorialProcessing(tutorial) {
  const status = normalizeStatus(tutorial?.status);
  return status === 'processing' || status === 'queued';
}

function renderStatusPill(tutorial) {
  if (!tutorial) return '';
  const status = statusMeta(tutorial.status);
  return status.className === 'processing' || status.className === 'queued'
    ? `<span class="badge ${status.className}">${status.label}</span>`
    : '';
}

function renderActiveStatusSummary(tutorials) {
  const statuses = tutorials.map(tutorial => statusMeta(tutorial.status));
  const activeStatus = statuses.find(status => status.className === 'processing')
    || statuses.find(status => status.className === 'queued');

  return activeStatus
    ? `<span class="badge ${activeStatus.className}">${activeStatus.label}</span>`
    : '';
}

function assetIcon(type) {
  const icons = {
    confluence: '<svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M7.5 7.5h9v9h-9zM4 4h7m2 0h7M4 20h7m2 0h7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    gamma: '<svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM12 12l8-4.5M12 12v9M12 12L4 7.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    pdf: '<svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M7 3h7l5 5v13H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM14 3v5h5M8 15h8M8 18h5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    video: '<svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M15 10l4.553-2.276A1 1 0 0 1 21 8.723v6.554a1 1 0 0 1-1.447.894L15 14M3 8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
  return icons[type] || '';
}

function externalIcon() {
  return '<svg class="asset-external" width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M20 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}

function assetButton(url, type, label, tutorialId, field) {
  const className = `asset-btn ${type}`;
  if (!url) {
    return `<span class="${className} off">${assetIcon(type)}<span>${esc(label)}</span></span>`;
  }

  const deleteBtn = tutorialId && field
    ? `<button class="asset-delete-btn" onclick="event.preventDefault(); event.stopPropagation(); deleteAsset('${esc(jsString(tutorialId))}', '${esc(field)}')" title="Remove ${esc(label)}">
        <svg width="10" height="10" fill="none" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>
      </button>`
    : '';

  return `
    <span class="asset-btn-wrap">
      <a class="${className}" href="${esc(url)}" target="_blank" rel="noopener">
        ${assetIcon(type)}
        <span>${esc(label)}</span>
        ${externalIcon()}
      </a>${deleteBtn}
    </span>`;
}


function renderLanguageSection(page, tutorial, language) {
  const meta = getLanguageMeta(language);
  const lastGenerated = tutorial ? formatDate(tutorial.lastGeneratedAt || tutorial.lastUpdatedAt, true) : '-';

  return `
    <section class="language-panel">
      <div class="language-panel-head">
        <div class="language-title">
          <strong>${esc(meta.label)}</strong>
        </div>
        <div class="language-actions">
          ${tutorial ? `
            <button class="icon-btn" onclick="event.stopPropagation(); showHistory('${esc(jsString(tutorial.id))}')" title="View generation history">
              <svg width="15" height="15" fill="none" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v6h6M12 7v5l3 2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
            </button>
            <button class="icon-btn danger" onclick="event.stopPropagation(); deleteTutorial('${esc(jsString(tutorial.id))}')" ${isTutorialProcessing(tutorial) ? 'disabled' : ''} title="Delete from library">
              <svg width="15" height="15" fill="none" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
            </button>
          ` : ''}
        </div>
      </div>
      <div class="language-meta">
        ${renderStatusPill(tutorial)}
        <span class="lib-date">${lastGenerated}</span>
      </div>
      <div class="asset-actions">
        ${assetButton(page.url, 'confluence', 'Confluence')}
        ${assetButton(tutorial?.gammaUrl, 'gamma', 'Gamma', tutorial?.id, 'gammaUrl')}
        ${assetButton(tutorial?.exportSharepointUrl || tutorial?.sharepointUrl, 'pdf', 'PDF', tutorial?.id, tutorial?.exportSharepointUrl ? 'exportSharepointUrl' : 'sharepointUrl')}
        ${assetButton(tutorial?.exportVideoUrl || tutorial?.videoUrl, 'video', 'Video', tutorial?.id, tutorial?.exportVideoUrl ? 'exportVideoUrl' : 'videoUrl')}
      </div>
    </section>`;
}

function renderPartCard(card, isExpanded) {
  const { page, tutorials: cardTutorials, key } = card;
  const title = page.label || 'Untitled';
  const part = parsePartLabel(title);
  const tutorialsByLanguage = new Map();

  for (const tutorial of cardTutorials) {
    const lang = (tutorial.language || 'unknown').toLowerCase();
    if (!tutorialsByLanguage.has(lang)) tutorialsByLanguage.set(lang, tutorial);
  }

  const languageKeys = Array.from(tutorialsByLanguage.keys());
  const languageOrder = getLanguageOrder(languageKeys);
  const visibleLanguageOrder = languageOrder.filter(lang => activeFilter === 'all' || lang === activeFilter);
  const activeStatusSummary = renderActiveStatusSummary(cardTutorials);
  const latestDate = cardTutorials
    .map(tutorial => tutorial.lastGeneratedAt || tutorial.lastUpdatedAt || tutorial.createdAt)
    .filter(Boolean)
    .sort()
    .pop();

  return `
    <article class="part-card${isExpanded ? ' expanded' : ''}">
      <button class="part-card-header" type="button" onclick="togglePartCard('${esc(jsString(key))}')">
        <span class="part-chevron">
          <svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <span class="part-heading">
          <strong>${esc(part.heading)}</strong>
          <span>${cardTutorials.length} asset set${cardTutorials.length !== 1 ? 's' : ''}${latestDate ? ` · Updated ${formatDate(latestDate, true)}` : ''}</span>
        </span>
        <span class="part-summary">
          ${activeStatusSummary}
        </span>
      </button>
      <div class="part-card-body">
        <div class="language-grid">
          ${visibleLanguageOrder.map(lang => renderLanguageSection(page, tutorialsByLanguage.get(lang), lang)).join('')}
        </div>
      </div>
    </article>`;
}

function renderLibrary() {
  const container = document.getElementById('libraryFolders');
  const summary = document.getElementById('resultsSummary');

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
  let visibleRows = 0;
  const visiblePartKeys = [];

  container.innerHTML = foldersData.folders.map(folder => {
    const isCollapsed = collapsedFolders.has(folder.id);

    const pageCards = folder.pages.map(page => {
      const pageTutorials = urlToTutorials.get(page.url) || [];
      const matchingTutorials = pageTutorials.filter(tutorial => rowMatchesFilters(page, tutorial, folder));
      if (!matchingTutorials.length) return null;
      const key = getPartKey(folder, page);
      visiblePartKeys.push(key);
      return { key, page, tutorials: matchingTutorials };
    }).filter(Boolean);

    if (!pageCards.length) return '';
    visiblePages += pageCards.length;
    visibleRows += pageCards.reduce((count, card) => count + card.tutorials.length, 0);

    return `
      <div class="folder-group${isCollapsed ? ' collapsed' : ''}">
        <div class="folder-header" onclick="toggleLibFolder('${esc(folder.id)}')">
          <div class="folder-header-left">
            <svg class="folder-chevron" width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <svg class="folder-icon" width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-6l-2-2H5a2 2 0 0 0-2 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <span class="folder-name">${esc(folder.name)}</span>
            <span class="folder-count">${pageCards.length} part${pageCards.length !== 1 ? 's' : ''}</span>
          </div>
        </div>
        <div class="folder-pages">
          ${pageCards.map(card => renderPartCard(card, expandedPartId === card.key)).join('')}
        </div>
      </div>`;
  }).join('');

  if (visiblePartKeys.length && (expandedPartId === null || (expandedPartId && !visiblePartKeys.includes(expandedPartId)))) {
    expandedPartId = visiblePartKeys[0];
    renderLibrary();
    return;
  }

  summary.textContent = `${visiblePages} parts · ${visibleRows} presentation${visibleRows !== 1 ? 's' : ''} · ${generated} generated`;
  updateExportButton(visibleRows);
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

function togglePartCard(partId) {
  expandedPartId = expandedPartId === partId ? '' : partId;
  renderLibrary();
}

async function deleteAsset(tutorialId, field) {
  const fieldLabels = { gammaUrl: 'Gamma', sharepointUrl: 'PDF', exportSharepointUrl: 'PDF', videoUrl: 'Video', exportVideoUrl: 'Video' };
  const label = fieldLabels[field] || field;

  if (!confirm(`Remove the ${label} link from this tutorial?`)) return;

  await fetch(`/api/tutorials/${encodeURIComponent(tutorialId)}/asset`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ field }),
  });
  await loadTutorials();
}

async function deleteTutorial(id) {
  const tutorial = tutorials.find(item => item.id === id);
  if (!tutorial) return;

  const ok = confirm(`Delete "${getTutorialTitle(tutorial)}" from the library?`);
  if (!ok) return;

  await fetch(`/api/tutorials/${id}`, { method: 'DELETE' });
  await loadTutorials();
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
          <span>${item.exportSharepointUrl || item.sharepointUrl ? `PDF: ${esc(item.exportSharepointUrl || item.sharepointUrl)}` : 'PDF: -'}</span>
          <span>${item.exportVideoUrl || item.videoUrl ? `Video: ${esc(item.exportVideoUrl || item.videoUrl)}` : 'Video: -'}</span>
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
