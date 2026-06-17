"""
Playwright browser automation for taking screenshots of jeenai.app.

Replaces the Langflow Agent + Playwright MCP approach with direct code.
The screenshot_script (list of {url, action}) already specifies exactly
what to capture, so no LLM decision-making is needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Browser

from backend.config import JEEN_USERNAME, JEEN_PASSWORD, SCREENSHOT_DIR

logger = logging.getLogger(__name__)

_ROUTES_MAP_PATH = Path(__file__).resolve().parent.parent / "routes_map.json"

LOGIN_URL = "https://jeenai.app/login"
PAGE_LOAD_WAIT_MS = 5000
RENDER_SETTLE_MS = 1500

# Selector for the apps/grid menu button in the regular app header, and the
# visible labels of the tile that navigates to the admin app.
APPS_MENU_BUTTON_SELECTOR = 'button[aria-label="User interface controls menu"]'
ADMIN_TILE_LABELS = ("Admin", "ניהול")

# Button text that would persist/destroy data — never clicked during interactions,
# even if the LLM requests it (defense in depth alongside the prompt rule).
DESTRUCTIVE_TEXT = re.compile(
    r"\b(delete|remove|save|submit|publish|confirm|deploy|"
    r"מחק|הסר|שמור|שלח|פרסם|אשר)\b",
    re.IGNORECASE,
)

ERROR_PAGE_MARKERS = (
    "404",
    "page not found",
    "not found",
    "404 - not found",
    "this page could not be found",
    "doesn't exist",
    "does not exist",
    "something went wrong",
)


def _sanitize_filename(action: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", action.lower()).strip("_")[:60]
    return slug or "screenshot"


def _make_screenshot_prefix(folder_name: str, part_name: str) -> str:
    def sanitize(s: str) -> str:
        return re.sub(r"[^\w\-]", "_", s.strip(), flags=re.UNICODE).strip("_")[:80]
    parts = [sanitize(p) for p in [folder_name, part_name] if p and p.strip()]
    return "_".join(parts) if parts else ""


async def _looks_like_error_page(page: Page) -> str | None:
    """
    Return a reason string if the page looks like a 404 / error / empty page,
    otherwise None. Used to avoid screenshotting broken pages.
    """
    try:
        body_text = (await page.inner_text("body")).strip()
    except Exception:
        body_text = ""

    lowered = body_text.lower()

    # Near-empty / blank page
    if len(lowered) < 15:
        return "page appears blank or empty"

    # Explicit error markers in the visible text
    for marker in ERROR_PAGE_MARKERS:
        if marker in lowered:
            return f"error marker found: '{marker}'"

    return None


async def _submit_credentials(page: Page) -> None:
    """Fill the two-step login form on the current page (email, then password).

    The password step is optional: when the session is already authenticated
    (e.g. the admin app via SSO), the password field never appears and we skip it.
    """
    if not JEEN_USERNAME or not JEEN_PASSWORD:
        raise RuntimeError("JEEN_USERNAME and JEEN_PASSWORD must be set in .env")

    # Step 1: Type email and press Enter to submit
    email_input = page.locator('input[type="email"], input[name="email"]').first
    await email_input.click()
    await email_input.press_sequentially(JEEN_USERNAME, delay=50)
    await page.keyboard.press("Enter")
    logger.info("Filled email, submitted with Enter...")
    await page.wait_for_timeout(2000)

    # Step 2: Type password and press Enter (may be skipped via SSO)
    try:
        password_input = page.locator('input[type="password"]').first
        await password_input.wait_for(state="visible", timeout=5000)
        await password_input.click()
        await password_input.press_sequentially(JEEN_PASSWORD, delay=50)
        await page.keyboard.press("Enter")
        logger.info("Filled password, submitted with Enter...")
        await page.wait_for_timeout(3000)
    except Exception:
        logger.info("No password step appeared (SSO or already authenticated)")

    await page.wait_for_load_state("domcontentloaded")


async def _login(page: Page) -> None:
    """Log into jeenai.app using stored credentials (two-step form)."""
    logger.info("Logging into jeenai.app...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    await _submit_credentials(page)
    logger.info("Login complete — current URL: %s", page.url)


async def _enter_admin_app(page: Page, admin_url: str) -> Page:
    """
    Navigate from the regular app into the admin app and return the page that
    now shows the admin app.

    After a normal login the session is on jeenai.app. Going straight to
    admin.jeenai.app bounces back to the login page, so the admin app must be
    reached by opening the apps menu and clicking the Admin / ניהול tile. That
    tile opens the admin app in a NEW browser tab, so this helper captures the
    new tab and returns it; callers must use the returned page for all later
    navigation. Raises RuntimeError if the admin app cannot be reached, so
    callers never silently crawl/screenshot the login page.
    """
    admin_host = urlparse(admin_url).netloc
    if not admin_host:
        raise RuntimeError(f"Invalid admin_url for admin entry: {admin_url!r}")

    context = page.context
    logger.info("Entering admin app via apps menu...")

    menu_button = page.locator(APPS_MENU_BUTTON_SELECTOR).first
    try:
        await menu_button.wait_for(state="visible", timeout=15000)
        await menu_button.click()
    except Exception as exc:
        raise RuntimeError(f"Failed to open apps menu: {exc}") from exc

    await page.wait_for_timeout(1000)

    # Each menu tile is an icon (aria-label="menu-grid-item-icon-container")
    # with a separate visible text label. Locate the label, then click the
    # enclosing clickable container when present, falling back to the label
    # node itself (the click bubbles to the tile's handler either way).
    # The admin tile opens a new tab, so wrap each click in expect_event("page")
    # to capture it; if no tab opens we fall back to same-tab navigation.
    clicked = False
    admin_page: Page | None = None
    for label in ADMIN_TILE_LABELS:
        tile = page.get_by_text(label, exact=True).first
        try:
            if await tile.count() == 0:
                continue
        except Exception:
            continue

        clickable_ancestor = tile.locator(
            "xpath=ancestor-or-self::*[self::a or self::button "
            "or @role='button' or @role='menuitem' "
            "or @aria-label='menu-grid-item-icon-container'][1]"
        )
        for target in (clickable_ancestor, tile):
            try:
                if await target.count() == 0:
                    continue
                await target.first.scroll_into_view_if_needed(timeout=3000)
            except Exception as exc:
                logger.debug("Admin tile '%s' not actionable: %s", label, exc)
                continue

            try:
                async with context.expect_event("page", timeout=8000) as new_page_info:
                    await target.first.click(timeout=3000)
                admin_page = await new_page_info.value
                logger.info("Admin tile '%s' opened a new tab", label)
            except Exception:
                # No new tab opened — either the click failed or it navigated
                # in the same tab. Treat the current page as the candidate and
                # let the URL check below decide.
                admin_page = page

            clicked = True
            logger.info("Clicked admin tile labeled '%s'", label)
            break
        if clicked:
            break

    if not clicked or admin_page is None:
        raise RuntimeError("Failed to enter admin app: Admin tile not found")

    # Use a predicate instead of a glob: Playwright's "*" does not cross "/",
    # so "*admin.jeenai.app*" never matches "https://admin.jeenai.app/login".
    try:
        await admin_page.wait_for_url(lambda url: admin_host in url, timeout=30000)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to enter admin app: URL never reached {admin_host} "
            f"(current: {admin_page.url})"
        ) from exc

    await admin_page.bring_to_front()
    try:
        await admin_page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_WAIT_MS)
    except Exception:
        pass

    # The admin app may present its own login form. Give SSO a moment to skip it,
    # then submit credentials explicitly if we're still on the login screen.
    if "/login" in admin_page.url:
        try:
            await admin_page.wait_for_url(lambda url: "/login" not in url, timeout=8000)
        except Exception:
            pass

    if "/login" in admin_page.url:
        logger.info("Admin app still on login — submitting credentials...")
        try:
            await _submit_credentials(admin_page)
            await admin_page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to log into admin app (current: {admin_page.url}): {exc}"
            ) from exc

    await admin_page.wait_for_timeout(RENDER_SETTLE_MS)
    logger.info("Admin app entered — current URL: %s", admin_page.url)
    return admin_page


def _load_clickable_groups(path: str, link_type: str) -> list[list[str]]:
    """
    Return the recorded clickable buttons for a route as a list of label-variant
    groups (each group is the set of labels — e.g. Hebrew + English — that locate
    the same button). Empty list if the route or file is missing.
    """
    norm_lt = "admin" if link_type == "admin" else "regular"
    try:
        with open(_ROUTES_MAP_PATH, encoding="utf-8") as f:
            routes = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(routes, list):
        return []

    for r in routes:
        if not isinstance(r, dict):
            continue
        r_lt = "admin" if r.get("link_type") == "admin" else "regular"
        if r.get("path") != path or r_lt != norm_lt:
            continue
        groups: list[list[str]] = []
        for el in r.get("clickable_elements") or []:
            if isinstance(el, dict) and el.get("variants"):
                variants = [str(v).strip() for v in el["variants"] if str(v).strip()]
            elif isinstance(el, dict):
                variants = [str(el.get(k, "")).strip() for k in ("text", "aria")]
                variants = [v for v in variants if v]
            else:
                variants = [str(el).strip()] if str(el).strip() else []
            if variants:
                groups.append(variants)
        return groups
    return []


def _expand_variants(text: str, groups: list[list[str]]) -> list[str]:
    """
    Given the label the LLM chose, return all sibling label variants for that
    button (so we can try the other-language label if the chosen one is not the
    one currently rendered). Falls back to just the chosen text.
    """
    norm = " ".join(text.split()).lower()
    for group in groups:
        if any(" ".join(v.split()).lower() == norm for v in group):
            # Put the chosen label first, then the other variants.
            ordered = [v for v in group if " ".join(v.split()).lower() == norm]
            ordered += [v for v in group if " ".join(v.split()).lower() != norm]
            return ordered
    return [text]


async def _find_clickable(page: Page, text: str):
    """
    Locate a visible clickable element for the given label, trying several
    strategies in order. Returns the first visible locator found, or None.

    The label text may correspond to a button's accessible name, its visible
    text, or only its aria-label (icon-only buttons), so we try each in turn.
    """
    normalized = " ".join(text.split())
    candidates = [
        page.get_by_role("button", name=normalized, exact=False),
        page.get_by_role("menuitem", name=normalized, exact=False),
        page.get_by_text(normalized, exact=False),
        page.locator(f'[aria-label*="{normalized}"]'),
    ]
    for locator in candidates:
        element = locator.first
        try:
            if await element.count() == 0:
                continue
            if await element.is_visible():
                return element
        except Exception:
            continue
    return None


async def _find_input(page: Page, label: str):
    """
    Locate a visible text input / textarea by its placeholder, aria-label, or
    associated <label> text. Returns the first visible locator found, or None.
    """
    normalized = " ".join(label.split())
    candidates = [
        page.get_by_placeholder(normalized, exact=False),
        page.locator(f'[aria-label*="{normalized}"]'),
        page.get_by_label(normalized, exact=False),
        page.locator(f'input[name*="{normalized}"]'),
        page.locator(f'textarea[name*="{normalized}"]'),
    ]
    for locator in candidates:
        element = locator.first
        try:
            if await element.count() == 0:
                continue
            if await element.is_visible():
                return element
        except Exception:
            continue
    return None


async def _run_interactions(
    page: Page, interactions: list[dict], variant_groups: list[list[str]] | None = None
) -> None:
    """
    Execute a list of interaction steps on the current page before screenshotting.

    Supported steps:
      - {"type": "click", "text": "<button/element label>"}
      - {"type": "fill", "label": "<input placeholder or aria-label>", "value": "<demo text>"}
      - {"type": "wait", "ms": <milliseconds>}

    For a click, the chosen label is expanded to all of its recorded language
    variants (Hebrew / English) and each is tried in turn via accessible-name,
    visible-text and aria-label matching, so the click works regardless of which
    UI language the page is currently rendered in. Destructive labels are
    refused. Raises on failure so the caller can fall back for this item.

    For a fill, the input is located by placeholder / aria-label / <label> text
    and filled with the provided demo value (never triggers real persistence).
    """
    variant_groups = variant_groups or []
    for step in interactions:
        kind = (step.get("type") or "click").lower()

        if kind == "wait":
            await page.wait_for_timeout(int(step.get("ms", 1000)))
            continue

        if kind == "fill":
            label = (step.get("label") or "").strip()
            value = (step.get("value") or "").strip()
            if not label:
                logger.warning("Skipping fill step with no label: %s", step)
                continue
            logger.info("Interaction: filling input label=%r value=%r", label, value)
            element = await _find_input(page, label)
            if element is None:
                raise RuntimeError(f"no visible input for label {label!r}")
            await element.click(timeout=5000)
            await element.fill(value)
            await page.wait_for_timeout(500)
            continue

        if kind == "click":
            text = (step.get("text") or "").strip()
            if not text:
                logger.warning("Skipping click step with no text: %s", step)
                continue
            if DESTRUCTIVE_TEXT.search(text):
                raise RuntimeError(f"refusing destructive interaction: {text!r}")

            candidates = _expand_variants(text, variant_groups)
            logger.info("Interaction: clicking element, candidates=%s", candidates)
            element = None
            for candidate in candidates:
                if DESTRUCTIVE_TEXT.search(candidate):
                    continue
                element = await _find_clickable(page, candidate)
                if element is not None:
                    break
            if element is None:
                raise RuntimeError(f"no visible clickable element for {candidates!r}")
            await element.click(timeout=5000)
            await page.wait_for_timeout(RENDER_SETTLE_MS)
            continue

        logger.warning("Unknown interaction step type %r — skipping: %s", kind, step)


async def take_screenshots(
    screenshot_script: list[dict],
    base_url: str = "https://jeenai.app",
    link_type: str = "regular",
    folder_name: str = "",
    part_name: str = "",
) -> list[dict]:
    """
    Execute the screenshot script and return a list of result dicts.

    Each item in screenshot_script has:
      - url: page URL to navigate to
      - action: description of what to capture (used for naming)
      - slide_section: (optional) heading of the target slide

    Returns list of dicts:
      [{"path": "/abs/path.png", "action": "...", "slide_section": "..."}, ...]
    """
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    results: list[dict] = []
    name_prefix = _make_screenshot_prefix(folder_name, part_name)

    logger.info("Screenshot script received (%d items): %s", len(screenshot_script), screenshot_script)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        await _login(page)

        if link_type == "admin":
            page = await _enter_admin_app(page, base_url)

        for i, item in enumerate(screenshot_script):
            url = item.get("url", "")
            action = item.get("action", f"screenshot_{i + 1}")
            slide_section = item.get("slide_section", "")
            interactions = item.get("interactions") or []

            if not url:
                logger.warning("Skipping item %d: no URL", i)
                continue

            if not url.startswith("http"):
                url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"

            logger.info("Screenshot %d/%d: navigating to %s (action: '%s')", i + 1, len(screenshot_script), url, action)

            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Skip pages that returned an HTTP error status (404, 500, ...)
                status = response.status if response else None
                if status is not None and status >= 400:
                    logger.warning(
                        "Screenshot %d/%d: skipping %s — HTTP status %d",
                        i + 1, len(screenshot_script), url, status,
                    )
                    continue

                # Wait for the SPA content to actually render before judging the page
                try:
                    await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_WAIT_MS)
                except Exception:
                    pass
                await page.wait_for_timeout(RENDER_SETTLE_MS)

                final_url = page.url
                if final_url.rstrip("/") != url.rstrip("/"):
                    logger.warning(
                        "Screenshot %d/%d: redirected to %s (expected %s) — skipping",
                        i + 1, len(screenshot_script), final_url, url,
                    )
                    continue

                page_title = await page.title()
                logger.info(
                    "Screenshot %d/%d: page loaded — title: %s (status: %s)",
                    i + 1, len(screenshot_script), page_title, status,
                )

                # Skip 404 / error / blank pages detected from the rendered content
                error_reason = await _looks_like_error_page(page)
                if error_reason:
                    try:
                        body_preview = (await page.inner_text("body")).strip()[:200]
                    except Exception:
                        body_preview = "(could not read body)"
                    logger.warning(
                        "Screenshot %d/%d: skipping %s — %s | action: '%s' | body preview: %s",
                        i + 1, len(screenshot_script), url, error_reason, action, repr(body_preview),
                    )
                    continue

                # Run any interactions (e.g. click a button to open a modal/panel)
                # before capturing. If an interaction fails we still capture the
                # plain loaded page — we are already on the correct route, so a
                # base screenshot is more useful than none.
                if interactions:
                    route_path = urlparse(url).path or "/"
                    if len(route_path) > 1:
                        route_path = route_path.rstrip("/")
                    variant_groups = _load_clickable_groups(route_path, link_type)
                    try:
                        await _run_interactions(page, interactions, variant_groups)
                    except Exception as exc:
                        logger.warning(
                            "Screenshot %d/%d: interactions failed on %s (%s) — "
                            "capturing plain page instead",
                            i + 1, len(screenshot_script), url, exc,
                        )

                if name_prefix:
                    filename = f"{name_prefix}_{i + 1}.png"
                else:
                    filename = f"{_sanitize_filename(action)}_{i + 1}.png"
                filepath = os.path.join(SCREENSHOT_DIR, filename)
                await page.screenshot(path=filepath, full_page=False)
                results.append({
                    "path": filepath,
                    "action": action,
                    "slide_section": slide_section,
                })
                logger.info("Saved screenshot: %s", filepath)

            except Exception as exc:
                logger.error("Failed screenshot %d (%s): %s", i + 1, url, exc)
                continue

        await browser.close()

    skipped = len(screenshot_script) - len(results)
    logger.info("Screenshots complete: %d saved, %d skipped out of %d total", len(results), skipped, len(screenshot_script))

    return results
