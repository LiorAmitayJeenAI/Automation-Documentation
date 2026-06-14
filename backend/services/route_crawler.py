"""
Playwright-based route crawler for jeenai.app.

Discovers navigable routes (including entity-specific pages such as
/agent-configuration/:uuid and /workflow/flow/:uuid), generates rich
descriptions with Azure OpenAI, and merges them add-only into
routes_map.json.

Designed for SPAs where sidebar/menu items may not be standard <a href>
links — uses click-and-observe to discover routes by interacting with
the UI and monitoring URL changes.

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
from backend.services.screenshots import _login, _enter_admin_app, _looks_like_error_page

logger = logging.getLogger(__name__)

_ROUTES_MAP_PATH = Path(__file__).resolve().parent.parent / "routes_map.json"

MAX_PAGES = 50
PAGE_LOAD_WAIT_MS = 5000
RENDER_SETTLE_MS = 2000

# Cap on how many clickable elements we keep/persist per route. Keeps the
# routes_map.json and the document-formatter prompt small while still giving
# the LLM a real menu of buttons to choose from.
MAX_CLICKABLE_ELEMENTS = 12

# Words in button/element text that indicate dangerous actions to avoid clicking
_DANGEROUS_WORDS = re.compile(
    r"\b(delete|remove|create|upload|import|export|logout|sign.?out|צור|מחק|העלה)\b",
    re.IGNORECASE,
)

_client = AsyncAzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)


# ── Path helpers ──

def _normalize_path(href: str | None, base_origin: str) -> str | None:
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
        return None

    path = path.split("?")[0].split("#")[0]
    if not path:
        return "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _is_infrastructure_path(path: str) -> bool:
    """Filter out CDN, API, static-asset, and other non-page paths."""
    prefixes = ("/cdn-cgi/", "/api/", "/static/", "/_next/", "/favicon", "/assets/")
    return any(path.startswith(p) for p in prefixes)


def _is_support_path(path: str) -> bool:
    """Support/docs articles are never screenshotted, so the crawler skips them
    (no point visiting them or collecting their clickable elements)."""
    return path == "/support" or path.startswith("/support/")


def _normalize_link_type(value: str | None) -> str:
    return "admin" if value == "admin" else "regular"


def _route_link_type(route: dict) -> str:
    return _normalize_link_type(route.get("link_type"))


def _route_key(link_type: str, path: str) -> tuple[str, str]:
    return (_normalize_link_type(link_type), path)


# ── Page inspection ──

async def _gather_page_info(page: Page) -> dict:
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
            "button, [role='tab'], [role='button'], [role='menuitem']",
            "els => {"
            "  const chrome = 'nav, header, aside, [role=\\\"navigation\\\"], "
            "[role=\\\"banner\\\"], [class*=\\\"sidebar\\\"], [class*=\\\"side-menu\\\"], "
            "[class*=\\\"header\\\"], [aria-label=\\\"User interface controls menu\\\"]';"
            "  const out = els"
            "    .filter(e => !e.closest(chrome))"
            "    .map(e => ({"
            "      text: (e.innerText || '').trim().slice(0, 60),"
            "      aria: (e.getAttribute('aria-label') || '').trim().slice(0, 60)"
            "    }))"
            "    .filter(b => b.text || b.aria);"
            "  out.sort((a, b) => (b.text ? 1 : 0) - (a.text ? 1 : 0));"
            "  return out.slice(0, 40);"
            "}",
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


async def _wait_for_page_ready(page: Page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_WAIT_MS)
    except Exception:
        pass
    await page.wait_for_timeout(RENDER_SETTLE_MS)


async def _navigate_and_settle(page: Page, url: str) -> int | None:
    """Navigate to url, wait for render, return HTTP status or None."""
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        logger.warning("Failed to navigate to %s: %s", url, exc)
        return None
    await _wait_for_page_ready(page)
    return response.status if response else None


# ── Link collection (standard <a href>) ──

async def _collect_href_links(page: Page, base_origin: str) -> set[str]:
    """Collect same-origin paths from <a href>, [data-href], and [to] attributes."""
    try:
        hrefs = await page.evaluate("""() => {
            const paths = [];
            document.querySelectorAll('a[href]').forEach(a =>
                paths.push(a.getAttribute('href'))
            );
            document.querySelectorAll('[data-href]').forEach(el =>
                paths.push(el.getAttribute('data-href'))
            );
            document.querySelectorAll('[to]').forEach(el =>
                paths.push(el.getAttribute('to'))
            );
            return paths;
        }""")
    except Exception:
        return set()

    paths: set[str] = set()
    for href in hrefs:
        normalized = _normalize_path(href, base_origin)
        if normalized and not _is_infrastructure_path(normalized):
            paths.add(normalized)
    return paths


# ── SPA sidebar discovery (click-and-observe) ──

async def _discover_sidebar_routes(page: Page, base_origin: str) -> set[str]:
    """
    Click sidebar/nav items one by one and record resulting URL changes.
    Works for SPAs that use React Router, Vue Router, etc.
    """
    discovered: set[str] = set()
    home_url = f"{base_origin}/"

    # Gather unique clickable sidebar items
    try:
        items_info = await page.evaluate("""() => {
            const selectors = [
                'nav a', 'nav button', 'nav [role="button"]', 'nav [role="link"]',
                'nav [role="menuitem"]',
                '[class*="sidebar"] a', '[class*="sidebar"] button',
                '[class*="sidebar"] [role="button"]',
                '[class*="side-menu"] a', '[class*="side-menu"] button',
                '[class*="nav-"] a', '[class*="nav-"] button',
                '[class*="menu"] a', '[class*="menu"] button',
                'aside a', 'aside button',
            ];
            const seen = new Set();
            const results = [];
            for (const sel of selectors) {
                try {
                    document.querySelectorAll(sel).forEach(el => {
                        const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
                        if (!text || seen.has(text)) return;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) return;
                        seen.add(text);
                        results.push({ text, index: results.length });
                    });
                } catch {}
            }
            return results;
        }""")
    except Exception as exc:
        logger.warning("Failed to enumerate sidebar items: %s", exc)
        return discovered

    logger.info("Found %d unique sidebar items to try", len(items_info))

    for item in items_info:
        text = item.get("text", "")
        if _DANGEROUS_WORDS.search(text):
            logger.debug("Skipping dangerous sidebar item: %s", text)
            continue

        initial_path = _normalize_path(page.url, base_origin)

        try:
            # Re-locate the element by its visible text each time (DOM may have re-rendered)
            locator = page.get_by_text(text, exact=True).first
            if not await locator.is_visible(timeout=2000):
                continue
            await locator.click(timeout=3000)
            await page.wait_for_timeout(2000)

            current_path = _normalize_path(page.url, base_origin)
            if (
                current_path
                and current_path != initial_path
                and not _is_infrastructure_path(current_path)
            ):
                discovered.add(current_path)
                logger.info("Sidebar click '%s' → %s", text, current_path)

        except Exception as exc:
            logger.debug("Sidebar click '%s' failed: %s", text, exc)

        # Always navigate back to home so we can click the next item
        try:
            await _navigate_and_settle(page, home_url)
        except Exception:
            pass

    return discovered


# ── Entity page discovery (click items inside list pages) ──

async def _discover_entity_pages(page: Page, base_origin: str, list_path: str) -> set[str]:
    """
    On a list page (e.g. /agents), click on individual items/cards to
    discover their entity URLs (e.g. /agent-configuration/:uuid).
    """
    discovered: set[str] = set()
    list_url = f"{base_origin}{list_path}"

    status = await _navigate_and_settle(page, list_url)
    if status is not None and status >= 400:
        return discovered

    # Find clickable cards/rows in the main content (not sidebar)
    try:
        card_count = await page.evaluate("""() => {
            const mainSelectors = [
                'main [class*="card"]', 'main [class*="item"]', 'main [class*="row"]',
                'main [class*="agent"]', 'main [class*="list"] > *',
                '[class*="content"] [class*="card"]',
                '[class*="content"] [class*="item"]',
                '[class*="grid"] > *',
            ];
            const seen = new Set();
            let count = 0;
            for (const sel of mainSelectors) {
                try {
                    document.querySelectorAll(sel).forEach(el => {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 50 && rect.height > 30) {
                            const key = `${Math.round(rect.x)},${Math.round(rect.y)}`;
                            if (!seen.has(key)) {
                                seen.add(key);
                                count++;
                            }
                        }
                    });
                } catch {}
            }
            return count;
        }""")
    except Exception:
        card_count = 0

    if card_count == 0:
        return discovered

    logger.info("Found %d potential entity items on %s", card_count, list_path)

    # Click each card, record URL, navigate back
    for i in range(min(card_count, 10)):
        await _navigate_and_settle(page, list_url)
        try:
            # Re-query cards each time (DOM re-renders)
            cards = await page.evaluate("""() => {
                const mainSelectors = [
                    'main [class*="card"]', 'main [class*="item"]', 'main [class*="row"]',
                    'main [class*="agent"]', 'main [class*="list"] > *',
                    '[class*="content"] [class*="card"]',
                    '[class*="content"] [class*="item"]',
                    '[class*="grid"] > *',
                ];
                const seen = new Set();
                const results = [];
                for (const sel of mainSelectors) {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 50 && rect.height > 30) {
                                const key = `${Math.round(rect.x)},${Math.round(rect.y)}`;
                                if (!seen.has(key)) {
                                    seen.add(key);
                                    results.push({
                                        x: rect.x + rect.width / 2,
                                        y: rect.y + rect.height / 2,
                                    });
                                }
                            }
                        });
                    } catch {}
                }
                return results;
            }""")

            if i >= len(cards):
                break

            card = cards[i]
            initial_path = _normalize_path(page.url, base_origin)
            await page.mouse.click(card["x"], card["y"])
            await page.wait_for_timeout(2000)

            current_path = _normalize_path(page.url, base_origin)
            if (
                current_path
                and current_path != initial_path
                and not _is_infrastructure_path(current_path)
            ):
                discovered.add(current_path)
                logger.info("Entity click on %s → %s", list_path, current_path)

        except Exception as exc:
            logger.debug("Entity click %d on %s failed: %s", i, list_path, exc)
            continue

    return discovered


# ── Main crawler ──

# Pages that typically contain lists of entities worth clicking into
_LIST_PAGES = {"/agents", "/skills", "/triggers", "/knowledge", "/integrations/connect"}


async def crawl_routes(base_url: str, link_type: str = "regular") -> list[dict]:
    """
    Crawl the product using three strategies:
    1. Click sidebar/nav items and observe URL changes
    2. Collect standard <a href> links from each page
    3. Click entity cards on known list pages
    Then visit each discovered path to gather page info.
    """
    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    normalized_link_type = _normalize_link_type(link_type)

    all_paths: set[str] = set()
    discovered: dict[str, dict] = {}

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        await _login(page)

        if normalized_link_type == "admin":
            page = await _enter_admin_app(page, base_url)

        # ── Strategy 1: Click sidebar items ──
        logger.info("Phase 1: Discovering routes via sidebar interaction...")
        home_url = f"{base_origin}/"
        await _navigate_and_settle(page, home_url)
        sidebar_paths = await _discover_sidebar_routes(page, base_origin)
        all_paths.update(sidebar_paths)
        logger.info("Sidebar discovery found %d paths: %s", len(sidebar_paths), sidebar_paths)

        # ── Strategy 2: Collect <a href> links from home page ──
        await _navigate_and_settle(page, home_url)
        href_paths = await _collect_href_links(page, base_origin)
        all_paths.update(href_paths)
        if href_paths:
            logger.info("Home page href links: %d paths", len(href_paths))

        # Also seed with existing known routes so we can discover links within them
        # (support/docs articles are skipped — we never screenshot them).
        existing = _load_existing()
        for route in existing:
            if _route_link_type(route) != normalized_link_type:
                continue
            path = route.get("path")
            if path and not _is_support_path(path):
                all_paths.add(path)

        # Always include root
        all_paths.add("/")

        # ── Visit all discovered paths and collect info + more links ──
        logger.info("Phase 2: Visiting %d discovered paths...", len(all_paths))
        to_visit = list(all_paths)

        for path in to_visit:
            if len(discovered) >= MAX_PAGES:
                break
            if path in discovered:
                continue
            if _is_support_path(path):
                continue

            url = f"{base_origin}{path}"
            logger.info("Visiting %s (discovered %d so far)", url, len(discovered))

            status = await _navigate_and_settle(page, url)
            if status is not None and status >= 400:
                logger.info("Skipping %s — HTTP %d", url, status)
                continue

            error_reason = await _looks_like_error_page(page)
            if error_reason:
                logger.info("Skipping %s — %s", url, error_reason)
                continue

            final_path = _normalize_path(page.url, base_origin) or path
            if final_path not in discovered:
                info = await _gather_page_info(page)
                info["path"] = final_path
                discovered[final_path] = info

            # Collect any new links found on this page
            page_links = await _collect_href_links(page, base_origin)
            for link in page_links:
                if _is_support_path(link):
                    continue
                if link not in discovered and link not in to_visit:
                    to_visit.append(link)

        # ── Strategy 3: Entity page discovery on list pages ──
        list_pages = _LIST_PAGES & set(discovered.keys())
        if list_pages:
            logger.info("Phase 3: Discovering entity pages from %d list pages...", len(list_pages))
            for list_path in list_pages:
                if len(discovered) >= MAX_PAGES:
                    break
                entity_paths = await _discover_entity_pages(page, base_origin, list_path)
                for ep in entity_paths:
                    if ep not in discovered and len(discovered) < MAX_PAGES:
                        logger.info("Visiting entity page %s", ep)
                        status = await _navigate_and_settle(page, f"{base_origin}{ep}")
                        if status is not None and status >= 400:
                            continue
                        error_reason = await _looks_like_error_page(page)
                        if error_reason:
                            continue
                        final_ep = _normalize_path(page.url, base_origin) or ep
                        if final_ep not in discovered:
                            info = await _gather_page_info(page)
                            info["path"] = final_ep
                            discovered[final_ep] = info

        await browser.close()

    logger.info("Crawl complete: %d routes discovered", len(discovered))
    return list(discovered.values())


# ── LLM description generation ──

def _format_route_for_prompt(info: dict) -> dict:
    clickable = _normalize_clickable_elements(info.get("buttons", []))
    return {
        "path": info.get("path", ""),
        "title": info.get("title", ""),
        "heading": info.get("heading", ""),
        "buttons": [b.get("text") or b.get("aria") for b in clickable],
        "body_preview": (info.get("body_preview", "") or "")[:800],
    }


async def _generate_descriptions(new_infos: list[dict]) -> dict[str, str]:
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


# ── Clickable element inventory ──

def _is_noise_label(label: str) -> bool:
    """True for labels not worth storing (empty, pure number, or overly long)."""
    return not label or label.isdigit() or len(label) > 40


def _normalize_clickable_elements(buttons: list) -> list[dict]:
    """
    Turn the raw buttons captured by _gather_page_info into a clean, capped list
    of {"variants": [...]} groups suitable for persisting in routes_map.json.

    Each group represents one on-page button and holds every label that can
    locate it (its visible text and/or aria-label). A second-language label is
    added later by _add_button_translations. At click time any variant that is
    visible on the page is used, which makes matching language-agnostic.

    - Accepts the object form ({text, aria}), the grouped form ({variants:[...]}),
      and the legacy string form.
    - Drops empties, pure numbers and overly long labels; dedupes whole buttons
      by their first variant; caps to MAX_CLICKABLE_ELEMENTS.
    """
    seen: set[str] = set()
    groups: list[dict] = []
    for raw in buttons or []:
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, dict) and "variants" in raw:
            candidates = list(raw.get("variants") or [])
        elif isinstance(raw, dict):
            candidates = [raw.get("text", ""), raw.get("aria", "")]
        else:
            continue

        variants: list[str] = []
        local_seen: set[str] = set()
        for c in candidates:
            label = str(c).strip()
            if _is_noise_label(label):
                continue
            key = label.lower()
            if key in local_seen:
                continue
            local_seen.add(key)
            variants.append(label)

        if not variants:
            continue

        primary = variants[0].lower()
        if primary in seen:
            continue
        seen.add(primary)

        groups.append({"variants": variants})
        if len(groups) >= MAX_CLICKABLE_ELEMENTS:
            break

    return groups


async def _translate_labels(labels: list[str]) -> dict[str, str]:
    """
    Translate each button label to the other UI language (Hebrew<->English)
    via Azure OpenAI, returning {original_label: translated_label}.

    The site exposes every button in both Hebrew and English depending on the
    account UI language, so storing both variants lets the screenshot step match
    whichever label is actually rendered. Returns {} on any failure (callers
    then keep the single captured variant).
    """
    unique = sorted({l for l in labels if l and not _is_noise_label(l)})
    if not unique:
        return {}

    system_prompt = (
        "You translate UI button labels for the jeenai.app product between Hebrew "
        "and English. For each label: if it is Hebrew, return its English UI label; "
        "if it is English, return its Hebrew UI label. Use the product's natural UI "
        "wording, not a literal translation. If a label is a proper noun or already "
        "language-neutral (e.g. 'Skills'), return it unchanged. "
        "Return ONLY a valid JSON object mapping each input label to its translation. "
        "No markdown fences, no extra text."
    )

    try:
        response = await _client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(unique, ensure_ascii=False)},
            ],
            temperature=1,
            max_completion_tokens=2000,
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
            return {str(k): str(v).strip() for k, v in parsed.items() if str(v).strip()}
    except Exception as exc:
        logger.warning("Label translation failed: %s", exc)

    return {}


async def _add_button_translations(clickable_by_path: dict[str, list[dict]]) -> None:
    """
    For every clickable group across all routes, append the other-language label
    as an additional variant. Mutates the groups in place.
    """
    all_labels: list[str] = []
    for groups in clickable_by_path.values():
        for group in groups:
            variants = group.get("variants") or []
            if variants:
                all_labels.append(variants[0])

    translations = await _translate_labels(all_labels)
    if not translations:
        return

    for groups in clickable_by_path.values():
        for group in groups:
            variants = group.get("variants") or []
            if not variants:
                continue
            translated = translations.get(variants[0], "").strip()
            if translated and translated.lower() not in {v.lower() for v in variants}:
                variants.append(translated)
            group["variants"] = variants


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


def merge_routes(discovered: list[dict], descriptions: dict[str, str], link_type: str = "regular") -> dict:
    normalized_link_type = _normalize_link_type(link_type)
    existing = _load_existing()
    existing_by_key = {
        _route_key(_route_link_type(route), route["path"]): route
        for route in existing
    }

    # Existing routes keep their curated description (add-only), but their
    # verified button inventory is refreshed from the latest crawl.
    added: list[dict] = []
    updated = 0
    for info in discovered:
        path = info.get("path")
        if not path:
            continue
        route_key = _route_key(normalized_link_type, path)
        # Prefer the translation-enriched list attached by discover_and_merge;
        # fall back to normalizing the raw captured buttons.
        clickable = info.get("clickable_elements")
        if clickable is None:
            clickable = _normalize_clickable_elements(info.get("buttons", []))

        if route_key in existing_by_key:
            # Existing route: refresh clickable_elements from this crawl
            # (description stays add-only). Every route in `discovered` was just
            # crawled, so overwrite even when the new capture is empty — that
            # clears stale/old-format data instead of leaving it behind.
            entry = existing_by_key[route_key]
            if entry.get("clickable_elements") != (clickable or None):
                updated += 1
            if clickable:
                entry["clickable_elements"] = clickable
            else:
                entry.pop("clickable_elements", None)
            continue

        description = (
            descriptions.get(path)
            or info.get("heading")
            or info.get("title")
            or ""
        )
        entry = {"link_type": normalized_link_type, "path": path, "description": description}
        if clickable:
            entry["clickable_elements"] = clickable
        existing.append(entry)
        existing_by_key[route_key] = entry
        added.append(entry)

    if added or updated:
        with open(_ROUTES_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info(
            "routes_map.json updated: %d new routes, %d clickable lists refreshed",
            len(added), updated,
        )

    return {"added": added, "updated": updated, "total": len(existing)}


async def discover_and_merge(base_url: str, link_type: str = "regular") -> dict:
    normalized_link_type = _normalize_link_type(link_type)
    discovered = await crawl_routes(base_url, normalized_link_type)

    existing_keys = {
        _route_key(_route_link_type(route), route["path"])
        for route in _load_existing()
    }
    new_infos = [
        d
        for d in discovered
        if d.get("path") and _route_key(normalized_link_type, d["path"]) not in existing_keys
    ]

    # Normalize captured buttons into grouped variants, then enrich each group
    # with its other-language label so screenshots match whichever UI language
    # is rendered. Attach the result to each route's info for merge_routes.
    clickable_by_path: dict[str, list[dict]] = {}
    for info in discovered:
        path = info.get("path")
        if not path:
            continue
        groups = _normalize_clickable_elements(info.get("buttons", []))
        info["clickable_elements"] = groups
        if groups:
            clickable_by_path[path] = groups

    await _add_button_translations(clickable_by_path)

    descriptions = await _generate_descriptions(new_infos)
    result = merge_routes(discovered, descriptions, normalized_link_type)
    result["discovered"] = len(discovered)
    return result
