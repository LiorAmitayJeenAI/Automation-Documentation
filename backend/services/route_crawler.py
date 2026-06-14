"""
Playwright-based route crawler for jeenai.app.

Discovers navigable routes (including entity-specific pages such as
/agent-configuration/:uuid and /workflow/flow/:uuid), generates rich
descriptions with Azure OpenAI, and merges them add-only into
routes_map.json.

Triggered on demand from the Generate Tutorials page when the
"Update Routes" checkbox is enabled.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Browser
from openai import AsyncAzureOpenAI

from backend.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_API_VERSION,
)
from backend.services.screenshots import _login, _looks_like_error_page

logger = logging.getLogger(__name__)

_ROUTES_MAP_PATH = Path(__file__).resolve().parent.parent / "routes_map.json"

# Safety guards to avoid runaway crawling
MAX_PAGES = 50
MAX_DEPTH = 2
PAGE_LOAD_WAIT_MS = 5000
RENDER_SETTLE_MS = 1500

_client = AsyncAzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)


# ── Path helpers ──

def _normalize_path(href: str | None, base_origin: str) -> str | None:
    """
    Convert an href into a clean same-origin path, or None if it is
    external, an anchor, or otherwise not a navigable internal route.
    """
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None

    if href.startswith("/"):
        path = href
    elif href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if f"{parsed.scheme}://{parsed.netloc}" != base_origin:
            return None
        path = parsed.path
    else:
        # Relative paths are ambiguous in an SPA — skip them.
        return None

    # Strip query string and fragment
    path = path.split("?")[0].split("#")[0]
    if not path:
        return "/"
    # Drop trailing slash (except for root)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


# ── Page inspection ──

async def _gather_page_info(page: Page) -> dict:
    """Collect title, heading, visible buttons/tabs and a body text preview."""
    try:
        title = await page.title()
    except Exception:
        title = ""

    heading = ""
    try:
        heading = (await page.locator("h1, h2").first.inner_text()).strip()[:120]
    except Exception:
        pass

    try:
        buttons = await page.eval_on_selector_all(
            "button, [role='tab'], [role='button']",
            "els => els.map(e => (e.innerText || e.getAttribute('aria-label') || '')"
            ".trim()).filter(Boolean).slice(0, 30)",
        )
    except Exception:
        buttons = []

    try:
        body_preview = (await page.inner_text("body")).strip()[:1500]
    except Exception:
        body_preview = ""

    return {
        "title": title,
        "heading": heading,
        "buttons": buttons,
        "body_preview": body_preview,
    }


async def _collect_links(page: Page, base_origin: str) -> set[str]:
    """Collect all same-origin internal paths from <a href> elements on the page."""
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
    except Exception:
        return set()

    paths: set[str] = set()
    for href in hrefs:
        normalized = _normalize_path(href, base_origin)
        if normalized:
            paths.add(normalized)
    return paths


# ── Crawler ──

async def crawl_routes(base_url: str) -> list[dict]:
    """
    Crawl the product starting from the main page and return a list of
    discovered route info dicts: {path, title, heading, buttons, body_preview}.

    Uses BFS over internal <a href> links up to MAX_DEPTH / MAX_PAGES.
    Never clicks buttons or triggers actions — only follows links.
    """
    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    discovered: dict[str, dict] = {}
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [("/", 0)]

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        await _login(page)

        while queue and len(discovered) < MAX_PAGES:
            path, depth = queue.pop(0)
            if path in visited:
                continue
            visited.add(path)

            url = f"{base_origin}{path}"
            logger.info("Crawling %s (depth %d, discovered %d)", url, depth, len(discovered))

            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                logger.warning("Failed to navigate to %s: %s", url, exc)
                continue

            status = response.status if response else None
            if status is not None and status >= 400:
                logger.info("Skipping %s — HTTP %d", url, status)
                continue

            try:
                await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_WAIT_MS)
            except Exception:
                pass
            await page.wait_for_timeout(RENDER_SETTLE_MS)

            error_reason = await _looks_like_error_page(page)
            if error_reason:
                logger.info("Skipping %s — %s", url, error_reason)
                continue

            final_path = _normalize_path(page.url, base_origin) or path
            if final_path not in discovered:
                info = await _gather_page_info(page)
                info["path"] = final_path
                discovered[final_path] = info
                visited.add(final_path)

            if depth < MAX_DEPTH:
                for link in await _collect_links(page, base_origin):
                    if link not in visited:
                        queue.append((link, depth + 1))

        await browser.close()

    logger.info("Crawl complete: %d routes discovered", len(discovered))
    return list(discovered.values())


# ── LLM description generation ──

def _format_route_for_prompt(info: dict) -> dict:
    return {
        "path": info.get("path", ""),
        "title": info.get("title", ""),
        "heading": info.get("heading", ""),
        "buttons": info.get("buttons", [])[:20],
        "body_preview": (info.get("body_preview", "") or "")[:800],
    }


async def _generate_descriptions(new_infos: list[dict]) -> dict[str, str]:
    """
    Ask Azure OpenAI for a concise rich description per new route.
    Returns a mapping of path -> description. Falls back to {} on failure.
    """
    if not new_infos:
        return {}

    routes_payload = [_format_route_for_prompt(i) for i in new_infos]

    system_prompt = (
        "You are documenting the routes (pages) of the jeenai.app product for a "
        "screenshot automation tool. For each route you receive, write a concise "
        "1-2 sentence description in English.\n\n"
        "Style guidelines (match these existing examples):\n"
        "- \"/agents\" -> \"Agents main page — list of all user agents with a "
        "'create agent' button that opens 3 agent type options\"\n"
        "- \"/\" -> \"Main chat workspace — chat input area with the assistant, also "
        "has 'history' and 'last activity' buttons that open a sidebar (URL stays the same)\"\n\n"
        "Each description must:\n"
        "- Describe what the page shows\n"
        "- Mention notable buttons/tabs/panels that do NOT change the URL\n"
        "- Stay concise (1-2 sentences)\n\n"
        "Return ONLY a valid JSON object mapping each route path to its description. "
        "No markdown fences, no extra text."
    )

    try:
        response = await _client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(routes_payload, ensure_ascii=False)},
            ],
            temperature=1,
            max_completion_tokens=4000,
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception as exc:
        logger.warning("LLM description generation failed: %s", exc)

    return {}


# ── Merge (add-only) ──

def _load_existing() -> list[dict]:
    try:
        with open(_ROUTES_MAP_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict) and r.get("path")]
    except FileNotFoundError:
        logger.warning("routes_map.json not found at %s — starting fresh", _ROUTES_MAP_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load routes_map.json: %s", exc)
    return []


def merge_routes(discovered: list[dict], descriptions: dict[str, str]) -> dict:
    """
    Add-only merge: append routes whose path is not already present.
    Existing routes are never modified or removed.
    """
    existing = _load_existing()
    existing_paths = {r["path"] for r in existing}

    added: list[dict] = []
    for info in discovered:
        path = info.get("path")
        if not path or path in existing_paths:
            continue
        description = (
            descriptions.get(path)
            or info.get("heading")
            or info.get("title")
            or ""
        )
        entry = {"path": path, "description": description}
        existing.append(entry)
        existing_paths.add(path)
        added.append(entry)

    if added:
        with open(_ROUTES_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info("Added %d new routes to routes_map.json", len(added))

    return {"added": added, "total": len(existing)}


async def discover_and_merge(base_url: str) -> dict:
    """
    Full flow: crawl routes, generate descriptions for new ones, and merge
    them add-only into routes_map.json.

    Returns {"added": [...], "total": N, "discovered": M}.
    """
    discovered = await crawl_routes(base_url)

    existing_paths = {r["path"] for r in _load_existing()}
    new_infos = [d for d in discovered if d.get("path") not in existing_paths]

    descriptions = await _generate_descriptions(new_infos)
    result = merge_routes(discovered, descriptions)
    result["discovered"] = len(discovered)
    return result
