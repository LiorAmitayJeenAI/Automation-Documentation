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

import asyncio
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
    MAX_CLICKABLE_ELEMENTS,
    CAPTURE_CHILD_BUTTONS,
    MAX_OPENERS_PER_PAGE,
    MAX_CHILDREN_PER_OPENER,
    CRAWLER_PAGE_TIMEOUT_S,
)
from backend.services.screenshots import _login, _enter_admin_app, _looks_like_error_page

logger = logging.getLogger(__name__)

_ROUTES_MAP_PATH = Path(__file__).resolve().parent.parent / "routes_map.json"

MAX_PAGES = 50
PAGE_LOAD_WAIT_MS = 5000
RENDER_SETTLE_MS = 2000

# Cap on how many clickable elements we keep/persist per route, plus the child
# button discovery knobs, all live in backend.config so they can be tuned via
# env without code changes. Keeping the persisted list small keeps both
# routes_map.json and the LLM prompt lean while still giving the LLM a real menu
# of buttons to choose from.

# Words in button/element text that indicate dangerous actions to avoid clicking.
# Upload/import are intentionally NOT listed: the file-picker guard
# (_install_filechooser_guard) cancels any native dialog with no files selected,
# so opening an upload panel is safe and lets us discover its "Advanced Options".
_DANGEROUS_WORDS = re.compile(
    r"\b(delete|remove|create|export|logout|sign.?out|צור|מחק)\b",
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
    if value == "admin":
        return "admin"
    if value == "finops":
        return "finops"
    return "regular"


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
            "button, [role='tab'], [role='button'], [role='menuitem'], "
            "div[aria-label='avatar'], div[aria-label='expandable-section-header']",
            """els => {
              // App chrome (sidebar/header/nav). These buttons are never clicked
              // by the screenshot/video LLM, so they are dropped to free slots for
              // real, page-specific action buttons.
              const chrome = 'nav, header, aside, [role="navigation"], '
                + '[role="banner"], [class*="sidebar"], [class*="side-menu"], '
                + '[class*="header"]';
              // Workspace icon-rail labels — these live inside the sidebar
              // chrome but should be kept as clickable navigation items.
              const WORKSPACE_RAIL = new Set([
                'מקורות מידע', 'סוכנים', 'skills', 'תזמונים'
              ]);
              // Loose nav/avatar/logo items that often live OUTSIDE a recognized
              // chrome container yet are still chrome (Home, the account avatar,
              // the product logo, notifications, search). Dropped by label.
              const CHROME_LABELS = new Set([
                'home', 'בית', 'דף הבית', 'menu', 'תפריט',
                'jeen admin', 'jeen', 'notifications', 'התראות',
                'search', 'חיפוש', 'avatar'
              ]);
              const isAvatar = t => /^[A-Z]{1,3}$/.test((t || '').trim());
              const isOpener = e => e.hasAttribute('aria-haspopup')
                || e.getAttribute('aria-expanded') !== null;
              // In-place expandable section headers (e.g. "Advanced Options" in
              // upload flows) toggle a sub-panel but expose no aria-haspopup/
              // aria-expanded, so recognize them explicitly as openers.
              const isExpandable = e =>
                (e.getAttribute('aria-label') || '') === 'expandable-section-header';
              // Upload triggers open an upload modal/panel (which holds
              // "Advanced Options") on click. They are safe to click because the
              // file-picker guard cancels any native dialog, so treat them as
              // openers. Detected by an associated <input type=file> OR by an
              // upload-related aria-label/text (drop zones and upload buttons
              // often have no file input until clicked).
              const isUploadTrigger = e => {
                if (e.querySelector && e.querySelector('input[type="file"]')) return true;
                const wrap = e.closest('label, [class*="upload"], [class*="dropzone"], [class*="drop-zone"]');
                if (wrap && wrap.querySelector('input[type="file"]')) return true;
                const sig = ((e.getAttribute('aria-label') || '') + ' '
                  + (e.innerText || '')).toLowerCase();
                return /upload|העל|גרור/.test(sig);
              };
              // Header/nav menu openers we DO want to keep (their child menus
              // are captured by _capture_child_buttons): the account avatar,
              // notifications, activity-panel logo, and the app switcher.
              // We keep these even though they live in chrome, because their
              // CHILDREN are real features a tutorial may need to click.
              // Workspace icon-rail buttons are also kept as direct nav items.
              // Returns a stable label, or '' when the element is not recognized.
              const APPS_ARIA = 'User interface controls menu';
              const ACTIVITY_ARIA = 'פתח תפריט';
              const CHROME_TRIGGER = new Set(['notifications', 'התראות', 'avatar']);
              const chromeTrigger = e => {
                const aria = (e.getAttribute('aria-label') || '').trim();
                const text = (e.innerText || '').trim();
                if (aria === APPS_ARIA) return aria;
                if (aria === ACTIVITY_ARIA || e.hasAttribute('data-sidebar-swap')) return ACTIVITY_ARIA;
                if (aria === 'avatar' || e.closest('div[aria-label="avatar"]')) return 'avatar';
                if (CHROME_TRIGGER.has((text || aria).toLowerCase())) return aria || text;
                if (WORKSPACE_RAIL.has(aria.toLowerCase()) || WORKSPACE_RAIL.has(text.toLowerCase())) return aria || text;
                // Any other in-chrome opener (e.g. a header dropdown) is kept too.
                if (isOpener(e) && e.closest(chrome)) return aria || text;
                return '';
              };
              // Rank buttons so the most useful survive the per-route cap: boost
              // page content and "opener" buttons (those that reveal menus/panels)
              // and buttons that carry real text over icon-only ones.
              const score = e => {
                let s = 0;
                if (e.closest('main, [class*="content"]')) s += 3;
                if (isOpener(e)) s += 2;
                if ((e.innerText || '').trim()) s += 1;
                return s;
              };
              const collapse = s => (s || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
              const toObj = e => {
                const trigger = chromeTrigger(e);
                // Single-line accessible name: collapse line breaks/whitespace so
                // the stored label matches what the click-time matcher resolves
                // (a multi-line innerText label otherwise never matched).
                const rawText = collapse(e.innerText);
                const aria = collapse(e.getAttribute('aria-label'));
                // Detect expanded state: aria-expanded OR visual signals
                // (sibling/child panel visible, chevron rotated).
                const ariaExp = e.getAttribute('aria-expanded') === 'true';
                const hasVisiblePanel = (() => {
                  const id = e.getAttribute('aria-controls');
                  if (id) {
                    const panel = document.getElementById(id);
                    if (panel) {
                      const style = window.getComputedStyle(panel);
                      return style.display !== 'none' && style.visibility !== 'hidden'
                        && panel.offsetHeight > 0;
                    }
                  }
                  // Check next sibling as a fallback panel target
                  const sib = e.nextElementSibling;
                  if (sib && (sib.getAttribute('role') === 'region'
                    || sib.getAttribute('role') === 'tabpanel'
                    || sib.classList.contains('panel')
                    || sib.classList.contains('dropdown-menu')
                    || sib.classList.contains('collapse'))) {
                    const style = window.getComputedStyle(sib);
                    return style.display !== 'none' && style.visibility !== 'hidden'
                      && sib.offsetHeight > 0;
                  }
                  // Accordion pattern: button with chevron icon whose parent
                  // wrapper has a visible next sibling (the content panel).
                  const hasChevron = !!e.querySelector('svg[class*="chevron"], svg[class*="arrow"], svg[class*="caret"]');
                  if (hasChevron) {
                    const wrapper = e.closest('div');
                    if (wrapper) {
                      const wrapSib = wrapper.nextElementSibling;
                      if (wrapSib && wrapSib.offsetHeight > 10) {
                        return true;
                      }
                    }
                  }
                  return false;
                })();
                const isExpanded = ariaExp || hasVisiblePanel;
                return {
                  // For avatar triggers, drop the per-user initials (e.g. "JD")
                  // and store only the stable 'avatar' label.
                  text: trigger === 'avatar' ? '' : rawText,
                  aria: trigger === 'avatar' ? 'avatar' : aria,
                  // Recognized chrome openers are clickable openers regardless of
                  // whether they expose aria-haspopup/aria-expanded.
                  opener: isOpener(e) || !!trigger || isExpandable(e) || isUploadTrigger(e),
                  expanded: isExpanded,
                  chrome: !!trigger,
                  // Per-button context used downstream: the control's role, and
                  // whether it lives inside a dialog/drawer (modal_only) so it is
                  // not offered as a top-level page action.
                  role: e.getAttribute('role') || e.tagName.toLowerCase(),
                  modal_only: !!e.closest('[role="dialog"], [aria-modal="true"]'),
                  score: score(e),
                };
              };
              const seen = new Set();
              const out = [];
              const prelim = els
                .map(e => ({ e, b: toObj(e) }))
                // Keep normal page buttons (outside chrome) plus recognized
                // chrome controls (avatar, notifications, app switcher,
                // activity-panel logo, workspace icon rail).
                .filter(({ e, b }) => b.chrome || !e.closest(chrome))
                .map(({ b }) => b)
                .filter(b => b.text || b.aria)
                .filter(b => {
                  if (b.chrome) return true;
                  const l = (b.text || b.aria).toLowerCase();
                  return !CHROME_LABELS.has(l) && !isAvatar(b.text);
                });
              // Count identical labels BEFORE dedup so we can mark controls that
              // legitimately repeat (e.g. a per-row "Actions"/"פעולות" button).
              const counts = {};
              prelim.forEach(b => {
                const k = (b.text || b.aria).toLowerCase();
                counts[k] = (counts[k] || 0) + 1;
              });
              prelim
                .sort((a, b) => b.score - a.score)
                .forEach(b => {
                  const key = (b.text || b.aria).toLowerCase();
                  if (seen.has(key)) return;
                  seen.add(key);
                  b.repeated = counts[key] > 1;
                  out.push(b);
                });
              return out.slice(0, 40);
            }""",
        )
    except Exception as exc:
        logger.warning("Button capture failed (%s) — no buttons for this page", exc)
        buttons = []

    try:
        body_preview = (await page.inner_text("body")).strip()[:1500]
    except Exception:
        body_preview = ""

    try:
        input_fields = await page.eval_on_selector_all(
            "input[type='text'], input[type='search'], input:not([type]), textarea",
            """els => {
              const out = [];
              const seen = new Set();
              for (const e of els) {
                if (e.offsetWidth === 0 && e.offsetHeight === 0) continue;
                if (e.type === 'hidden' || e.type === 'file') continue;
                const label = (e.getAttribute('aria-label') || e.placeholder || e.name || '').trim();
                if (!label || seen.has(label.toLowerCase())) continue;
                seen.add(label.toLowerCase());
                out.push({label});
              }
              return out.slice(0, 10);
            }""",
        )
    except Exception:
        input_fields = []

    return {
        "title": title,
        "heading": heading,
        "buttons": buttons,
        "input_fields": input_fields,
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
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        logger.warning("Failed to navigate to %s: %s", url, exc)
        return None
    await _wait_for_page_ready(page)
    return response.status if response else None


async def _gather_revealed_items(page: Page) -> list[dict]:
    """
    Collect the items revealed inside an open menu / dropdown / dialog / popover,
    WITHOUT the chrome filter that _gather_page_info applies. Popups usually live
    in a portal at the document root (e.g. role="menu"), so the chrome filter
    would otherwise drop legitimate child items (a profile menu's language /
    settings entries, etc.). Returns [{text, aria}, ...].
    """
    try:
        return await page.eval_on_selector_all(
            '[role="menu"] [role="menuitem"], [role="menu"] button, '
            '[role="menu"] a, [role="listbox"] [role="option"], '
            '[role="dialog"] button, [role="dialog"] [role="menuitem"], '
            '[role="dialog"] div[aria-label="expandable-section-header"], '
            '[class*="popover"] button, [class*="popover"] [role="menuitem"], '
            '[class*="dropdown"] button, [class*="dropdown"] [role="menuitem"], '
            'div[aria-label="expandable-section-header"]',
            """els => {
              const seen = new Set();
              const out = [];
              for (const e of els) {
                const r = e.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                const text = (e.innerText || '').trim().slice(0, 60);
                const aria = (e.getAttribute('aria-label') || '').trim().slice(0, 60);
                if (!text && !aria) continue;
                const key = (text || aria).toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({ text, aria });
              }
              return out.slice(0, 40);
            }""",
        )
    except Exception:
        return []


async def _expand_expandable_children(
    page: Page, children: list[dict], before: set[str],
) -> None:
    """
    While an opener's panel/modal is still open, expand any expandable-section-
    header child (e.g. "Advanced Options") and record the sub-options it reveals
    as nested `opens` on that child (mutates *children* in place).

    Sub-options already visible in the panel (and base-page labels) are excluded
    via *seen* so only the genuinely newly revealed controls are kept.
    """
    seen = set(before) | {
        (c.get("text") or c.get("aria") or "").lower()
        for c in children
        if (c.get("text") or c.get("aria"))
    }
    for child in children:
        if (child.get("aria") or "") != "expandable-section-header":
            continue
        child_label = (child.get("text") or "").strip()
        if not child_label:
            continue
        try:
            header = page.get_by_text(child_label, exact=True).first
            if not await header.is_visible(timeout=1500):
                continue
            await header.click(timeout=2500)
            await page.wait_for_timeout(RENDER_SETTLE_MS)
            revealed = await _gather_revealed_items(page)
            sub = [
                b for b in revealed
                if (b.get("text") or b.get("aria") or "").lower() not in seen
                and not _DANGEROUS_WORDS.search(b.get("text") or b.get("aria") or "")
            ]
            if sub:
                child["opens"] = sub[:MAX_CHILDREN_PER_OPENER]
                seen.update(
                    (b.get("text") or b.get("aria") or "").lower() for b in sub
                )
        except Exception as exc:
            logger.debug(
                "Expanding section '%s' failed: %s", child_label, exc
            )


async def _capture_child_buttons(page: Page, base_origin: str, info: dict) -> None:
    """
    For each safe "opener" button on the route, click it, diff the buttons that
    appear, and record the newly revealed buttons as nested `opens` on the parent
    button (mutates info["buttons"] in place). Depth is limited to 1 — children
    only, except in-place expandable sections (e.g. "Advanced Options") whose
    sub-options are captured one level deeper via _expand_expandable_children.

    State is reset between openers (Escape, then re-navigate to the route) so an
    open dropdown/modal does not bleed into the next opener's diff. Openers that
    navigate away (real links, not in-place toggles) are skipped, destructive
    openers are refused, and the opener / child counts are capped to keep crawl
    time and routes_map.json bounded.
    """
    path = info.get("path")
    if not path:
        return

    base_buttons = info.get("buttons") or []
    before = {
        (b.get("text") or b.get("aria") or "").lower()
        for b in base_buttons
        if (b.get("text") or b.get("aria"))
    }

    openers = [
        b for b in base_buttons
        if b.get("opener")
        and (b.get("text") or b.get("aria"))
        and not _DANGEROUS_WORDS.search(b.get("text") or b.get("aria") or "")
    ]
    if not openers:
        return

    route_url = f"{base_origin}{path}"
    for parent in openers[:MAX_OPENERS_PER_PAGE]:
        text_label = (parent.get("text") or "").strip()
        aria_label = (parent.get("aria") or "").strip()
        label = text_label or aria_label
        try:
            initial_path = _normalize_path(page.url, base_origin)

            # Locate the opener. Visible-text match first; fall back to an
            # aria-label selector for icon-only openers (avatar, notifications,
            # app switcher) that have no clickable text.
            locator = None
            if text_label:
                candidate = page.get_by_text(text_label, exact=True).first
                try:
                    if await candidate.is_visible(timeout=2000):
                        locator = candidate
                except Exception:
                    locator = None
            if locator is None and aria_label:
                escaped = aria_label.replace('"', '\\"')
                candidate = page.locator(f'[aria-label="{escaped}"]').first
                try:
                    if await candidate.is_visible(timeout=2000):
                        locator = candidate
                except Exception:
                    locator = None
            if locator is None:
                continue

            await locator.click(timeout=3000)
            await page.wait_for_timeout(RENDER_SETTLE_MS)

            # An opener that changed the URL is a navigation link, not an in-place
            # toggle — skip it (its target route is captured on its own).
            if _normalize_path(page.url, base_origin) != initial_path:
                await _navigate_and_settle(page, route_url)
                continue

            # Prefer the popup-aware capture (keeps menu items the chrome filter
            # would drop); fall back to a full-page diff if nothing was found.
            revealed = await _gather_revealed_items(page)
            if not revealed:
                after = await _gather_page_info(page)
                revealed = after.get("buttons") or []

            new_children = [
                b for b in revealed
                if (b.get("text") or b.get("aria") or "").lower() not in before
                and not _DANGEROUS_WORDS.search(b.get("text") or b.get("aria") or "")
            ]
            if new_children:
                capped = new_children[:MAX_CHILDREN_PER_OPENER]
                # Depth-2 (only for in-place expandable sections such as
                # "Advanced Options"): while the panel is still open, expand each
                # expandable-section-header child and record its sub-options so
                # they are not lost. Limited to these headers to keep crawl time
                # and routes_map.json bounded.
                await _expand_expandable_children(page, capped, before)
                parent["opens"] = capped
                logger.info(
                    "Opener '%s' on %s revealed %d child button(s)",
                    label, path, len(parent["opens"]),
                )
        except Exception as exc:
            logger.debug(
                "Child capture for opener '%s' on %s failed: %s", label, path, exc
            )

        # Reset so the next opener starts from a clean page state.
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass
        await _navigate_and_settle(page, route_url)


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


async def _process_page(
    page: Page, path: str, base_origin: str,
    to_visit: list[str], discovered: dict[str, dict],
) -> None:
    """Process a single page: navigate, gather info, capture child buttons, collect links."""
    url = f"{base_origin}{path}"
    logger.info("_process_page: navigating to %s", url)
    status = await _navigate_and_settle(page, url)
    if status is not None and status >= 400:
        logger.info("Skipping %s — HTTP %d", url, status)
        return

    error_reason = await _looks_like_error_page(page)
    if error_reason:
        logger.info("Skipping %s — %s", url, error_reason)
        return

    final_path = _normalize_path(page.url, base_origin) or path
    if final_path not in discovered:
        logger.info("_process_page: gathering info for %s", final_path)
        info = await _gather_page_info(page)
        info["path"] = final_path
        discovered[final_path] = info
        logger.info("_process_page: page %s saved (discovered %d)", final_path, len(discovered))
        if CAPTURE_CHILD_BUTTONS:
            logger.info("_process_page: capturing child buttons for %s", final_path)
            await _capture_child_buttons(page, base_origin, info)
            logger.info("_process_page: child buttons done for %s", final_path)

    page_links = await _collect_href_links(page, base_origin)
    new_links = [l for l in page_links if not _is_support_path(l) and l not in discovered and l not in to_visit]
    if new_links:
        logger.info("_process_page: found %d new links on %s", len(new_links), final_path)
    for link in new_links:
        to_visit.append(link)


async def _process_entity_page(
    page: Page, ep: str, base_origin: str, discovered: dict[str, dict],
) -> None:
    """Process a single entity page: navigate, gather info, capture child buttons."""
    logger.info("_process_entity_page: navigating to %s", ep)
    status = await _navigate_and_settle(page, f"{base_origin}{ep}")
    if status is not None and status >= 400:
        logger.info("_process_entity_page: skipping %s — HTTP %d", ep, status)
        return
    error_reason = await _looks_like_error_page(page)
    if error_reason:
        logger.info("_process_entity_page: skipping %s — %s", ep, error_reason)
        return
    final_ep = _normalize_path(page.url, base_origin) or ep
    if final_ep not in discovered:
        logger.info("_process_entity_page: gathering info for %s", final_ep)
        info = await _gather_page_info(page)
        info["path"] = final_ep
        discovered[final_ep] = info
        logger.info("_process_entity_page: page %s saved (discovered %d)", final_ep, len(discovered))
        if CAPTURE_CHILD_BUTTONS:
            logger.info("_process_entity_page: capturing child buttons for %s", final_ep)
            await _capture_child_buttons(page, base_origin, info)
            logger.info("_process_entity_page: child buttons done for %s", final_ep)


def _install_filechooser_guard(page: Page) -> None:
    """Auto-dismiss native OS file pickers so clicking an upload trigger can never
    freeze the crawler. The dialog is cancelled with no files selected, so nothing
    is ever uploaded."""
    page.on("filechooser", lambda chooser: asyncio.ensure_future(chooser.set_files([])))


async def crawl_routes(
    base_url: str, link_type: str = "regular", target_paths: list[str] | None = None,
) -> list[dict]:
    """
    Crawl the product using three strategies:
    1. Click sidebar/nav items and observe URL changes
    2. Collect standard <a href> links from each page
    3. Click entity cards on known list pages
    Then visit each discovered path to gather page info.

    When *target_paths* is provided (e.g. ["/agents"]), only those paths (and
    their entity sub-pages) are crawled — the broad discovery phases are skipped.
    """
    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    normalized_link_type = _normalize_link_type(link_type)

    all_paths: set[str] = set()
    discovered: dict[str, dict] = {}

    targeted = bool(target_paths)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        _install_filechooser_guard(page)

        await _login(page, base_url=base_url, link_type=normalized_link_type)

        if normalized_link_type == "admin":
            page = await _enter_admin_app(page, base_url)
            _install_filechooser_guard(page)

        if targeted:
            # Targeted mode: only crawl the specified paths
            all_paths.update(target_paths)
            logger.info("Targeted crawl: %d path(s) requested — %s", len(target_paths), target_paths)
        else:
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

        # ── Visit all discovered/targeted paths and collect info + more links ──
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

            try:
                await asyncio.wait_for(
                    _process_page(page, path, base_origin, to_visit, discovered),
                    timeout=CRAWLER_PAGE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.warning("Page %s exceeded %ds budget — skipping", path, CRAWLER_PAGE_TIMEOUT_S)
                continue

        # ── Strategy 3: Entity page discovery on list pages ──
        if targeted:
            list_pages = _LIST_PAGES & set(target_paths)
        else:
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
                        try:
                            await asyncio.wait_for(
                                _process_entity_page(page, ep, base_origin, discovered),
                                timeout=CRAWLER_PAGE_TIMEOUT_S,
                            )
                        except asyncio.TimeoutError:
                            logger.warning("Entity page %s exceeded %ds budget — skipping", ep, CRAWLER_PAGE_TIMEOUT_S)
                            continue

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

# Matches class/test-id-like tokens (lowercase words joined by hyphens, no
# spaces) such as "dropdown-trigger", "record-meeting-button", "logo-wrapper",
# "menu-grid-item-icon-container". These leak in from DOM attributes and are not
# real, human-readable button labels, so they must never be stored or clicked.
_JUNK_LABEL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def _is_noise_label(label: str) -> bool:
    """True for labels not worth storing (empty, pure number, overly long, or a
    class/test-id-like token rather than a real label)."""
    label = label.strip()
    return (
        not label
        or label.isdigit()
        or len(label) > 40
        or bool(_JUNK_LABEL.match(label))
    )


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
    - Drops empties, pure numbers, overly long labels, and class/test-id tokens.
    - Dedupes globally across ALL variants (not just the first one): a label that
      already identifies an earlier button is never reused on a later button, so
      no single label (e.g. "Menu") can resolve to more than one element on the
      page. Caps to MAX_CLICKABLE_ELEMENTS.
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
            # Collapse any newlines/whitespace so stored labels are single-line
            # and resolvable by the click-time matcher.
            label = " ".join(str(c).split())
            if _is_noise_label(label):
                continue
            key = label.lower()
            # Skip a label already used by this or any earlier button so every
            # stored label maps to exactly one button.
            if key in local_seen or key in seen:
                continue
            local_seen.add(key)
            variants.append(label)

        if not variants:
            continue

        seen.update(local_seen)
        group: dict = {"variants": variants}
        if isinstance(raw, dict) and raw.get("expanded"):
            group["default_expanded"] = True
        # Carry per-button context so downstream consumers can treat repeated
        # row controls and modal-only buttons correctly.
        if isinstance(raw, dict):
            if raw.get("modal_only"):
                group["modal_only"] = True
            if raw.get("repeated"):
                group["repeated"] = True
            if raw.get("role"):
                group["role"] = raw["role"]
        # Preserve nested child buttons (revealed by clicking this opener),
        # normalized recursively. Children are deduped within their own menu
        # scope, not against top-level buttons, since they live in a different
        # reveal state.
        child_raw = raw.get("opens") if isinstance(raw, dict) else None
        if child_raw:
            child_groups = _normalize_clickable_elements(child_raw)
            if child_groups:
                group["opens"] = child_groups
        groups.append(group)

    # Cap the list, but never drop menu openers: groups that reveal child
    # buttons (`opens`) are kept first so a feature's menu is not pushed out by
    # page-specific buttons on a button-heavy route. The rest fill the remaining
    # slots in their original (score) order.
    if len(groups) > MAX_CLICKABLE_ELEMENTS:
        with_children = [g for g in groups if g.get("opens")]
        without_children = [g for g in groups if not g.get("opens")]
        groups = (with_children + without_children)[:MAX_CLICKABLE_ELEMENTS]

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


def _collect_first_variants(groups: list[dict], out: list[str]) -> None:
    """Collect the primary label of every group, recursing into nested `opens`."""
    for group in groups:
        variants = group.get("variants") or []
        if variants:
            out.append(variants[0])
        children = group.get("opens") or []
        if children:
            _collect_first_variants(children, out)


def _apply_translations(groups: list[dict], translations: dict[str, str]) -> None:
    """
    Append the other-language label to each group as an extra variant, recursing
    into nested `opens`. Collision avoidance is scoped to each menu: top-level
    buttons share one scope, and each opener's `opens` list is its own scope, so
    an added variant stays unambiguous where matching actually happens.
    """
    existing = {
        v.lower()
        for group in groups
        for v in (group.get("variants") or [])
    }
    for group in groups:
        variants = group.get("variants") or []
        if variants:
            translated = translations.get(variants[0], "").strip()
            if translated:
                key = translated.lower()
                if (
                    key not in {v.lower() for v in variants}
                    and key not in existing
                ):
                    variants.append(translated)
                    existing.add(key)
                    group["variants"] = variants
        children = group.get("opens") or []
        if children:
            _apply_translations(children, translations)


async def _add_button_translations(clickable_by_path: dict[str, list[dict]]) -> None:
    """
    For every clickable group across all routes (including nested child buttons),
    append the other-language label as an additional variant. Mutates in place.
    """
    all_labels: list[str] = []
    for groups in clickable_by_path.values():
        _collect_first_variants(groups, all_labels)

    translations = await _translate_labels(all_labels)
    if not translations:
        return

    for groups in clickable_by_path.values():
        _apply_translations(groups, translations)


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

        input_fields = info.get("input_fields") or []

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
            if input_fields:
                entry["input_fields"] = input_fields
            else:
                entry.pop("input_fields", None)
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
        if input_fields:
            entry["input_fields"] = input_fields
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


async def discover_and_merge(
    base_url: str, link_type: str = "regular", target_paths: list[str] | None = None,
) -> dict:
    normalized_link_type = _normalize_link_type(link_type)
    discovered = await crawl_routes(base_url, normalized_link_type, target_paths=target_paths)

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
