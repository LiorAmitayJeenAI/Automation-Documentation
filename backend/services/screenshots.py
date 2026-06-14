"""
Playwright browser automation for taking screenshots of jeenai.app.

Replaces the Langflow Agent + Playwright MCP approach with direct code.
The screenshot_script (list of {url, action}) already specifies exactly
what to capture, so no LLM decision-making is needed.
"""

from __future__ import annotations

import logging
import os
import re

from playwright.async_api import async_playwright, Page, Browser

from backend.config import JEEN_USERNAME, JEEN_PASSWORD, SCREENSHOT_DIR

logger = logging.getLogger(__name__)

LOGIN_URL = "https://jeenai.app/login"
PAGE_LOAD_WAIT_MS = 5000
RENDER_SETTLE_MS = 1500

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


async def _login(page: Page) -> None:
    """Log into jeenai.app using stored credentials (two-step form)."""
    if not JEEN_USERNAME or not JEEN_PASSWORD:
        raise RuntimeError("JEEN_USERNAME and JEEN_PASSWORD must be set in .env")

    logger.info("Logging into jeenai.app...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)

    # Step 1: Type email and press Enter to submit
    email_input = page.locator('input[type="email"], input[name="email"]').first
    await email_input.click()
    await email_input.press_sequentially(JEEN_USERNAME, delay=50)
    await page.keyboard.press("Enter")
    logger.info("Filled email, submitted with Enter...")
    await page.wait_for_timeout(2000)

    # Step 2: Type password and press Enter to submit
    password_input = page.locator('input[type="password"]').first
    await password_input.click()
    await password_input.press_sequentially(JEEN_PASSWORD, delay=50)
    await page.keyboard.press("Enter")
    logger.info("Filled password, submitted with Enter...")
    await page.wait_for_timeout(3000)
    await page.wait_for_load_state("domcontentloaded")
    logger.info("Login complete — current URL: %s", page.url)


async def take_screenshots(
    screenshot_script: list[dict],
    base_url: str = "https://jeenai.app",
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

    logger.info("Screenshot script received (%d items): %s", len(screenshot_script), screenshot_script)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        await _login(page)

        for i, item in enumerate(screenshot_script):
            url = item.get("url", "")
            action = item.get("action", f"screenshot_{i + 1}")
            slide_section = item.get("slide_section", "")

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
