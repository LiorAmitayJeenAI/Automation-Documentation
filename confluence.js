require('dotenv').config();

const BASE_URL = process.env.CONFLUENCE_BASE_URL;
const USER = process.env.CONFLUENCE_USER;
const API_TOKEN = process.env.CONFLUENCE_API_TOKEN;
const SPACE_KEY = process.env.CONFLUENCE_SPACE_KEY;

const authHeader = 'Basic ' + Buffer.from(`${USER}:${API_TOKEN}`).toString('base64');

function stripHtml(html) {
  return html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
}

async function fetchPages() {
  const pages = [];
  let start = 0;
  const limit = 50;

  while (true) {
    const url = `${BASE_URL}/wiki/rest/api/content?spaceKey=${SPACE_KEY}&type=page&expand=body.storage&limit=${limit}&start=${start}`;
    const res = await fetch(url, { headers: { Authorization: authHeader, Accept: 'application/json' } });

    if (!res.ok) throw new Error(`Confluence API error: ${res.status} ${res.statusText}`);

    const data = await res.json();
    for (const page of data.results) {
      pages.push({
        id: page.id,
        title: page.title,
        textContent: stripHtml(page.body.storage.value),
      });
    }

    if (data.results.length < limit) break;
    start += limit;
  }

  return pages;
}

module.exports = { fetchPages };
