require('dotenv').config();
const path = require('path');
const fs = require('fs');

async function takeScreenshot(page, url, outputPath) {
  await page.goto(url);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(5000);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await page.screenshot({ path: outputPath, fullPage: true });
  console.log(`Screenshot saved: ${outputPath}`);
}

module.exports = { takeScreenshot };
