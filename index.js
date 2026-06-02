require('dotenv').config();
const { chromium } = require('playwright');
const { fetchPages } = require('./confluence');
const { inferUrl } = require('./ai');
const { takeScreenshot } = require('./screenshot');
const path = require('path');

function slugify(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

async function login(page) {
  await page.goto('https://jeenai.app/login');
  await page.fill('input[type="email"]', process.env.EMAIL);
  await page.click('button[type="submit"]');
  await page.waitForSelector('input[type="password"]');
  await page.fill('input[type="password"]', process.env.PASSWORD);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 50000 }),
    page.click('button[type="submit"]'),
  ]);
  await page.waitForTimeout(15000);
}

(async () => {
  console.log('Fetching Confluence pages...');
  const pages = await fetchPages();
  console.log(`Found ${pages.length} pages.`);

  const browser = await chromium.launch();
  const page = await browser.newPage();

  console.log('Logging into jeenai.app...');
  await login(page);

  for (const confluencePage of pages) {
    console.log(`\nProcessing: "${confluencePage.title}"`);
    const url = await inferUrl(confluencePage.title, confluencePage.textContent);
    console.log(`  → URL: ${url}`);
    const outputPath = path.join('screenshots', slugify(confluencePage.title), 'screenshot.png');
    await takeScreenshot(page, url, outputPath);
  }

  await browser.close();
  console.log('\nDone.');
})();
