function esc(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatDate(value, withTime = false) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString('en-GB', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  });
}

function getTutorialTitle(tutorial) {
  return tutorial.label || tutorial.title || 'Untitled tutorial';
}

function getSourceUrl(tutorial) {
  return tutorial.confluenceUrl || tutorial.url || '';
}

function normalizeStatus(status) {
  if (status === 'done' || status === 'up-to-date') return 'up-to-date';
  if (status === 'running' || status === 'processing') return 'processing';
  if (status === 'error' || status === 'failed') return 'failed';
  return 'needs-update';
}

function statusMeta(status) {
  const normalized = normalizeStatus(status);
  const map = {
    'up-to-date': { label: 'Up To Date', className: 'up-to-date' },
    processing: { label: 'Processing', className: 'processing' },
    failed: { label: 'Failed', className: 'failed' },
    'needs-update': { label: 'Needs Update', className: 'needs-update' },
  };
  return map[normalized];
}

function linkHtml(url, label, className = 'text-link') {
  if (!url) return `<span class="${className} off">-</span>`;
  return `<a class="${className}" href="${esc(url)}" target="_blank" rel="noopener">${esc(label)}</a>`;
}

function parseServerSentEvents(buffer, chunk, onMessage) {
  const decoder = new TextDecoder();
  buffer += decoder.decode(chunk, { stream: true });
  const lines = buffer.split('\n');
  const nextBuffer = lines.pop();

  for (const line of lines) {
    if (!line.startsWith('data: ')) continue;
    onMessage(JSON.parse(line.slice(6)));
  }

  return nextBuffer;
}
