require('dotenv').config();
const Anthropic = require('@anthropic-ai/sdk');

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const APP_BASE = 'https://jeenai.app';

async function inferUrl(title, textContent) {
  const message = await client.messages.create({
    model: 'claude-sonnet-4-6',
    max_tokens: 256,
    messages: [
      {
        role: 'user',
        content: `You are helping automate screenshot capture for the web app at ${APP_BASE}.

A Confluence documentation page titled "${title}" describes a product feature. Based on the content below, return ONLY the single most relevant full URL within ${APP_BASE} to navigate to for a screenshot. If you cannot determine a specific page, return ${APP_BASE}.

Documentation:
${textContent.slice(0, 3000)}`,
      },
    ],
  });

  const text = message.content[0].text.trim();
  // Extract a URL from the response
  const match = text.match(/https?:\/\/[^\s"']+/);
  return match ? match[0] : APP_BASE;
}

module.exports = { inferUrl };
