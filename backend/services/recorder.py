"""
Playwright browser recorder.

Records a real browser session navigating the product according to a video script.
Uses Playwright's built-in record_video feature — every click, page load, and
UI animation is captured as-is. No screenshots, no invented content.

Returns the path to the recorded WebM file along with per-step timing data
so that Hebrew subtitle cues can be synchronised to the exact moment each
page becomes visible in the recording.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, BrowserContext

from backend.config import VIDEO_DIR
from backend.services.screenshots import (
    _login,
    _enter_admin_app,
    _load_clickable_groups,
    _looks_like_error_page,
    _expand_variants,
    _find_clickable,
    _find_input,
    _resolve_clickable_with_retry,
    DESTRUCTIVE_TEXT,
    PAGE_LOAD_WAIT_MS,
    RENDER_SETTLE_MS,
)

logger = logging.getLogger(__name__)

VIEWPORT = {"width": 1920, "height": 1080}
MIN_SETTLE_MS = 1200  # minimum dwell time per step regardless of script value

# Selectors that indicate the page is still loading / not painted yet.
_SPINNER_SELECTORS = [
    '[class*="spinner"]',
    '[class*="loading"]',
    '[class*="skeleton"]',
    '[aria-busy="true"]',
]
_SPINNER_COMBINED = ", ".join(_SPINNER_SELECTORS)
_SPINNER_POLL_TIMEOUT_MS = 4000
_SPINNER_POLL_INTERVAL_MS = 200

async def wait_until_no_spinner(page: Page, timeout_ms: int = _SPINNER_POLL_TIMEOUT_MS) -> bool:
    """
    Poll until no visible spinner / loading / skeleton elements remain on the
    page.  Returns True if the page became clean within *timeout_ms*, False if
    the timeout elapsed (caller should fall back to current behaviour).
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        count = await page.evaluate(
            """(sel) => {
                const els = document.querySelectorAll(sel);
                let visible = 0;
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) visible++;
                }
                return visible;
            }""",
            _SPINNER_COMBINED,
        )
        if count == 0:
            return True
        await page.wait_for_timeout(_SPINNER_POLL_INTERVAL_MS)
    return False


def _first_interaction_selector(interactions: list[dict]) -> str | None:
    """
    Return a CSS-ish selector for the first click/fill target in the
    interaction list, so we can wait for it before stamping cue time.
    Returns None if nothing usable is found.
    """
    for step in interactions:
        kind = (step.get("type") or "click").lower()
        if kind == "click":
            text = (step.get("text") or "").strip()
            if text:
                return text
        elif kind == "fill":
            label = (step.get("label") or "").strip()
            if label:
                return label
    return None


async def _wait_for_page_ready(
    page: Page,
    interactions: list[dict],
    variant_groups: list[list[str]] | None = None,
) -> None:
    """
    Wait for a real "page is ready" signal after goto+networkidle.

    Strategy (tried in order):
    1. If the step has interactions, wait for the first click/fill target
       element to become visible — that proves the SPA has finished painting
       the relevant UI.
    2. Fall back to wait_until_no_spinner: poll until no visible spinner /
       loading / skeleton / aria-busy elements remain.
    3. If neither signal arrives, log a warning and proceed (fall back to
       current behaviour, which already waited for networkidle + RENDER_SETTLE_MS).
    """
    target = _first_interaction_selector(interactions)

    if target:
        try:
            # For clicks, search using the same resolution chain as _find_clickable
            first_kind = (interactions[0].get("type") or "click").lower()
            if first_kind == "click":
                candidates = _expand_variants(target, variant_groups or [])
                for candidate in candidates:
                    el = await _find_clickable(page, candidate)
                    if el is not None:
                        logger.debug("Page ready — interaction target %r found", candidate)
                        return
            elif first_kind == "fill":
                el = await _find_input(page, target)
                if el is not None:
                    logger.debug("Page ready — input target %r found", target)
                    return
        except Exception as exc:
            logger.debug("Interaction-target wait failed (%s), trying spinner poll", exc)

    # Fallback: wait for spinners / skeletons / aria-busy to disappear
    clean = await wait_until_no_spinner(page)
    if clean:
        logger.debug("Page ready — no spinners detected")
    else:
        logger.warning(
            "Spinner wait timed out after %d ms — proceeding anyway",
            _SPINNER_POLL_TIMEOUT_MS,
        )


# ── Injected cursor + click-ripple script ────────────────────────────────────
# Runs on every navigation via page.add_init_script so headless Chromium shows
# a visible pointer and a ripple on every click.
_CURSOR_INIT_JS = r"""
(() => {
  /* --- custom cursor --- */
  const cursor = document.createElement('div');
  cursor.id = '__jeen_cursor__';
  cursor.style.cssText =
    'position:fixed;top:0;left:0;pointer-events:none;z-index:2147483647;';
  cursor.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">' +
    '<defs><filter id="cs"><feDropShadow dx="1" dy="1" stdDeviation="1.5"' +
    ' flood-color="rgba(0,0,0,0.55)"/></filter></defs>' +
    '<path d="M4 2 L4 18 L8 13.5 L11.5 21 L13.5 20 L10 12.5 L16 12.5 Z"' +
    ' fill="white" stroke="#222" stroke-width="0.8" filter="url(#cs)"/>' +
    '</svg>';

  /* --- click ripple --- */
  const ripple = document.createElement('div');
  ripple.id = '__jeen_ripple__';
  ripple.style.cssText =
    'position:fixed;pointer-events:none;z-index:2147483646;border-radius:50%;' +
    'background:rgba(108,92,231,0.45);width:0;height:0;' +
    'transform:translate(-50%,-50%) scale(0);';

  document.addEventListener('mousemove', (e) => {
    cursor.style.left = (e.clientX - 3) + 'px';
    cursor.style.top  = (e.clientY - 2) + 'px';
  });

  document.addEventListener('mousedown', (e) => {
    ripple.style.left       = e.clientX + 'px';
    ripple.style.top        = e.clientY + 'px';
    ripple.style.transition = 'none';
    ripple.style.width      = '0';
    ripple.style.height     = '0';
    ripple.style.opacity    = '1';
    ripple.style.transform  = 'translate(-50%,-50%) scale(0)';
    requestAnimationFrame(() => {
      ripple.style.transition =
        'width .45s ease,height .45s ease,opacity .45s ease,transform .45s ease';
      ripple.style.width     = '64px';
      ripple.style.height    = '64px';
      ripple.style.opacity   = '0';
      ripple.style.transform = 'translate(-50%,-50%) scale(1)';
    });
  });

  /* Attach to body as soon as it exists (init scripts run before HTML is parsed) */
  function attach() {
    if (!document.body || document.getElementById('__jeen_cursor__')) return;
    document.body.appendChild(cursor);
    document.body.appendChild(ripple);
    obs.disconnect();
  }
  const obs = new MutationObserver(attach);
  obs.observe(document, {childList: true, subtree: true});
  attach();
})();
"""

_JS_HIGHLIGHT_SHOW = """(bbox) => {
  const old = document.getElementById('__jeen_hl__');
  if (old) old.remove();
  const hl = document.createElement('div');
  hl.id = '__jeen_hl__';
  hl.style.cssText =
    'position:fixed;pointer-events:none;z-index:2147483645;border-radius:6px;' +
    'border:2px solid #6C5CE7;' +
    'box-shadow:0 0 0 3px rgba(108,92,231,0.25),0 0 18px rgba(108,92,231,0.55);' +
    'left:'  + (bbox.x - 5)            + 'px;' +
    'top:'   + (bbox.y - 5)            + 'px;' +
    'width:' + (bbox.width  + 10)      + 'px;' +
    'height:'+ (bbox.height + 10)      + 'px;';
  document.body.appendChild(hl);
}"""

_JS_HIGHLIGHT_HIDE = """() => {
  const hl = document.getElementById('__jeen_hl__');
  if (hl) hl.remove();
}"""

# Minimum on-screen size (px) for a target to be worth highlighting/clicking.
# Anything smaller is almost certainly a zero-size / collapsed / stale node.
_MIN_TARGET_PX = 6

# Probe: is the element a genuine, on-screen, hit-testable, INTERACTIVE target?
# Returns {related, interactive}. `related` is true when the element painted at
# the box centre is the target itself or shares its DOM lineage (so the ring
# would land on the real element, not on top of an overlay). `interactive` is
# true when the element or an ancestor is an actual control — this rejects the
# loose text/aria fallback matching a paragraph that merely contains the label.
_JS_HIT_TEST = """(el, pt) => {
  const hit = document.elementFromPoint(pt.x, pt.y);
  if (!hit) return { related: false, interactive: false };
  const related = el === hit || el.contains(hit) || hit.contains(el);
  const interactive = !!el.closest(
    'button, a, [role="button"], [role="tab"], [role="menuitem"], ' +
    '[role="option"], [role="link"], [role="radio"], [role="checkbox"], ' +
    'input, select, textarea, [onclick], [tabindex]'
  );
  return { related, interactive };
}"""
# ─────────────────────────────────────────────────────────────────────────────


async def _genuine_target_bbox(page: Page, element, require_interactive: bool = True):
    """
    Return a viewport bounding box for *element* only if it is a genuine,
    visible, on-screen, hit-testable target — otherwise None.

    This is the guard against the "phantom" highlight: _find_clickable's looser
    fallback can match a stale, hidden, zero-size, off-screen, or non-interactive
    node (e.g. a paragraph that merely contains the button's label text). Drawing
    the ring on such a node makes a button frame appear where there is no button.

    Steps: require the element to be visible, scroll it into view so an element
    below the fold gets a correct on-screen box, reject empty / sub-pixel boxes,
    reject boxes whose centre is outside the recorded viewport, and finally
    hit-test the centre point (it must resolve to the element / its lineage, and
    — when require_interactive — sit inside a real interactive control).
    """
    try:
        if not await element.is_visible():
            return None
    except Exception:
        return None

    try:
        await element.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    try:
        bbox = await element.bounding_box()
    except Exception:
        bbox = None
    if not bbox:
        return None

    width = bbox.get("width", 0)
    height = bbox.get("height", 0)
    if width < _MIN_TARGET_PX or height < _MIN_TARGET_PX:
        return None

    cx = bbox["x"] + width / 2
    cy = bbox["y"] + height / 2
    if not (0 <= cx <= VIEWPORT["width"] and 0 <= cy <= VIEWPORT["height"]):
        return None

    try:
        probe = await element.evaluate(_JS_HIT_TEST, {"x": cx, "y": cy})
    except Exception:
        probe = {"related": True, "interactive": True}  # permissive if probe fails

    if not probe.get("related"):
        return None
    if require_interactive and not probe.get("interactive"):
        return None

    return bbox


async def _glide_and_highlight(page: Page, bbox: dict) -> None:
    """Glide the visible cursor to the box centre and draw the highlight ring.

    Leaves the ring on screen — the caller hides it after the click/hold so the
    press is visible. The injected click-ripple fires automatically on the
    real mousedown dispatched by element.click().
    """
    cx = bbox["x"] + bbox["width"] / 2
    cy = bbox["y"] + bbox["height"] / 2
    await page.mouse.move(cx, cy, steps=18)
    await page.wait_for_timeout(200)
    await page.evaluate(_JS_HIGHLIGHT_SHOW, bbox)
    await page.wait_for_timeout(350)


async def _run_interactions_visible(
    page: Page,
    interactions: list[dict],
    variant_groups: list[list[str]] | None = None,
) -> None:
    """
    Execute interactions with visual feedback: the mouse glides to each target
    (driving the injected cursor), a purple highlight ring appears before each
    click, and a ripple fires on mousedown.

    Mirrors screenshots._run_interactions in behaviour (same destructive-action
    guard, same fill/click/wait types) but adds the visual layer needed for
    video recording. Raises on failure so the caller can fall back to plain page.
    """
    variant_groups = variant_groups or []

    for step in interactions:
        kind = (step.get("type") or "click").lower()

        if kind == "wait":
            await page.wait_for_timeout(int(step.get("ms", 1000)))
            continue

        if kind == "scroll":
            target = (step.get("target") or "").strip()
            await page.evaluate("""(target) => {
                if (!target || target === 'bottom') {
                    window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});
                } else {
                    const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[class*="title"],[class*="header"]')];
                    const match = headings.find(e => e.textContent.trim().includes(target));
                    if (match) {
                        match.scrollIntoView({behavior: 'smooth', block: 'center'});
                    } else {
                        window.scrollBy({top: 500, behavior: 'smooth'});
                    }
                }
            }""", target)
            await page.wait_for_timeout(int(step.get("ms", 1200)))
            continue

        if kind == "fill":
            label = (step.get("label") or "").strip()
            value = (step.get("value") or "").strip()
            if not label:
                logger.warning("Skipping fill step with no label: %s", step)
                continue

            element = await _find_input(page, label)
            if element is None:
                raise RuntimeError(f"no visible input for label {label!r}")

            # Glide the cursor to the field and draw the highlight ring BEFORE
            # the click, so the click into the input is always shown (cursor +
            # ring + ripple), never an invisible press. Inputs need no
            # interactivity probe — they are inherently fillable controls.
            bbox = await _genuine_target_bbox(page, element, require_interactive=False)
            if bbox:
                await _glide_and_highlight(page, bbox)

            await element.click(timeout=5000)
            if bbox:
                await page.evaluate(_JS_HIGHLIGHT_HIDE)
            await element.fill(value)

            actual = await element.input_value()
            if not actual and value:
                logger.debug("fill() produced empty value for %r — retrying with press_sequentially", label)
                await element.clear()
                await element.press_sequentially(value, delay=50)

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
            element = await _resolve_clickable_with_retry(page, candidates)
            if element is None:
                raise RuntimeError(f"no visible clickable element for {candidates!r}")

            # Validate this is a genuine, on-screen, hit-testable interactive
            # target BEFORE drawing anything. This single check fixes both:
            #  • the phantom frame — a stale/hidden/zero-size/off-screen or
            #    non-interactive match (e.g. a paragraph containing the label)
            #    no longer gets a highlight ring drawn around empty space; and
            #  • the invisible press — when bounding_box() was falsy the old
            #    code skipped the visuals but still clicked. Now an unverifiable
            #    target fails the step (plain page captured, narration realigned)
            #    instead of producing a silent, unseen click.
            bbox = await _genuine_target_bbox(page, element)

            # Toggle-aware: if the target is already expanded (e.g. a menu the
            # page opened by default, or one opened by a previous interaction),
            # clicking it would CLOSE it. Don't click — but still glide + ring
            # (no ripple, no click) so the viewer sees the element the narration
            # is about, then leave it open.
            try:
                already_expanded = await element.evaluate("""(el) => {
                    if (el.getAttribute('aria-expanded') === 'true') return true;
                    const cls = (el.className || '').toLowerCase();
                    if (/\\b(open|expanded|active|is-open|is-expanded|is-active)\\b/.test(cls)) return true;
                    const state = el.getAttribute('data-state');
                    if (state === 'open' || state === 'expanded') return true;
                    const ariaControls = el.getAttribute('aria-controls');
                    if (ariaControls) {
                        const panel = document.getElementById(ariaControls);
                        if (panel) {
                            const rect = panel.getBoundingClientRect();
                            if (rect.height > 10) return true;
                        }
                    }
                    const hasChevron = !!el.querySelector('svg[class*="chevron"], svg[class*="arrow"], svg[class*="caret"]');
                    if (hasChevron) {
                        const wrapper = el.closest('div');
                        if (wrapper) {
                            const sib = wrapper.nextElementSibling;
                            if (sib) {
                                const rect = sib.getBoundingClientRect();
                                if (rect.height > 10) return true;
                            }
                        }
                    }
                    return false;
                }""")
            except Exception:
                already_expanded = False

            if already_expanded:
                logger.info(
                    "Element %r already expanded — showing without clicking to avoid closing it",
                    text,
                )
                if bbox:
                    await _glide_and_highlight(page, bbox)
                    await page.evaluate(_JS_HIGHLIGHT_HIDE)
                await page.wait_for_timeout(RENDER_SETTLE_MS)
                continue

            if bbox is None:
                raise RuntimeError(
                    f"resolved element for {candidates!r} is not a genuine visible target"
                )

            # Glide → highlight ring → real click (fires the ripple) → hide ring.
            await _glide_and_highlight(page, bbox)
            await element.click(timeout=5000)
            await page.evaluate(_JS_HIGHLIGHT_HIDE)
            await page.wait_for_timeout(RENDER_SETTLE_MS)
            continue

        logger.warning("Unknown interaction type %r — skipping: %s", kind, step)


def _url_key(url: str) -> str:
    """Normalise a URL for dedup comparison (strip trailing slash and query string)."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"


def _has_screen_changing_interactions(interactions: list[dict]) -> bool:
    """True only if at least one interaction is a click, fill, or scroll (not just waits)."""
    return any(
        (step.get("type") or "click").lower() in ("click", "fill", "scroll")
        for step in interactions
    )


async def record_product_video(
    video_script: list[dict],
    base_url: str = "https://jeenai.app",
    link_type: str = "regular",
    session_id: str = "default",
    audio_results: list[dict | None] | None = None,
    language: str = "he",
) -> dict:
    """
    Record a real browser session following the video_script steps.

    Each step in video_script should have:
      - url: absolute product URL
      - action: short description (used for logging)
      - narration: Hebrew narration text (passed through, not used for recording)
      - interactions: optional list of {type, text/ms} steps
      - settle_ms: how long to linger on the page after it loads (ms)

    audio_results: parallel list to video_script with TTS results
      {"path": str, "duration_s": float} or None per step.
      When provided, settle_ms = audio_duration_ms + 200 (tight to speech, no
      MIN_SETTLE_MS floor) so the clip matches the narration with no silent tail.

    Returns:
      {
        "webm_path": str,              absolute path to the .webm recording
        "step_timings": list[float],   seconds-from-recording-start when each step became visible
        "total_seconds": float,        total duration of the recording
        "recorded_steps": list[dict],  subset of video_script that loaded successfully
        "recorded_audio": list[dict|None], audio results for recorded steps only
        "failed_steps": list[dict],    steps that could not be shown (used for explanation slides)
      }
    """
    session_video_dir = Path(VIDEO_DIR) / "recordings" / session_id
    session_video_dir.mkdir(parents=True, exist_ok=True)

    audio_results = audio_results or []
    step_timings: list[float] = []
    step_settles: list[float] = []  # actual settle duration (seconds) per recorded step
    step_leads: list[float] = []  # interaction-animation lead (seconds) before content settles
    recorded_steps: list[dict] = []
    recorded_audio: list[dict | None] = []
    failed_steps: list[dict] = []
    seen_urls: set[str] = set()
    webm_path: str | None = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # ── Phase 1: login without recording so the auth screen never appears in the video ──
        login_context = await browser.new_context(
            viewport=VIEWPORT,
            ignore_https_errors=True,
        )
        login_page = await login_context.new_page()
        await _login(login_page, base_url=base_url, language=language, link_type=link_type)
        if link_type == "admin":
            login_page = await _enter_admin_app(login_page, base_url)
        storage_state = await login_context.storage_state()
        await login_context.close()

        # ── Phase 2: recording context starts already authenticated ──
        context: BrowserContext = await browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(session_video_dir),
            record_video_size=VIEWPORT,
            ignore_https_errors=True,
            storage_state=storage_state,
        )
        page = await context.new_page()
        # Inject visible cursor + click-ripple on every navigation
        await page.add_init_script(_CURSOR_INIT_JS)
        record_start = time.time()

        # ── Navigate each step ──
        for i, step in enumerate(video_script):
            url = step.get("url", "")
            action = step.get("action", f"step_{i + 1}")
            interactions = step.get("interactions") or []
            audio = audio_results[i] if i < len(audio_results) else None

            # Audio-tight linger: clip matches narration length with minimal pad.
            # No MIN_SETTLE_MS floor when audio exists — speech IS the timing.
            if audio and audio.get("duration_s"):
                settle_ms = round(audio["duration_s"] * 1000) + 200
            else:
                settle_ms = max(int(step.get("settle_ms", 3000)), MIN_SETTLE_MS)

            if not url.startswith("http"):
                url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"

            url_key = _url_key(url)
            current_key = _url_key(page.url) if page.url else ""
            needs_navigation = (current_key != url_key)

            # Skip only pure duplicates: same URL already shown, no interactions,
            # and we'd have to navigate there (nothing new to show).
            if url_key in seen_urls and not _has_screen_changing_interactions(interactions) and needs_navigation:
                logger.info("Step %d skipped — duplicate screen: %s", i + 1, url)
                continue

            logger.info(
                "Recording step %d/%d: %s — %s", i + 1, len(video_script), url, action
            )

            try:
                if needs_navigation:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    status = response.status if response else None
                    if status and status >= 400:
                        logger.warning("Step %d failed — HTTP %d: %s", i + 1, status, url)
                        failed_steps.append(step)
                        continue

                    try:
                        await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_WAIT_MS)
                    except Exception:
                        pass

                    # Detect redirects (SPA route guards — page not reachable in current state)
                    final_url = page.url
                    if final_url.rstrip("/") != url.rstrip("/"):
                        logger.warning(
                            "Step %d failed — redirected to %s", i + 1, final_url
                        )
                        failed_steps.append(step)
                        continue

                    # Detect 404 / blank / error pages
                    error_reason = await _looks_like_error_page(page)
                    if error_reason:
                        logger.warning("Step %d failed — %s", i + 1, error_reason)
                        failed_steps.append(step)
                        continue
                else:
                    logger.info("Step %d — same URL, continuing from current state", i + 1)

                # ── Wait for the SPA to finish painting ──
                # Resolve variant groups once (used by both ready-wait and interactions)
                route_path = urlparse(url).path.rstrip("/") or "/"
                variant_groups = _load_clickable_groups(route_path, link_type)

                await _wait_for_page_ready(page, interactions, variant_groups)

                # Run interactions with visible cursor, highlight ring, and ripple.
                # If they fail we still record the plain page, but flag the step so
                # the pipeline can rewrite its narration to match what is actually
                # shown (the base page, not the tab/panel that never opened).
                # Mark when the (painted) page is ready so the jump-cut editor can
                # keep the interaction animation that plays between now and the
                # post-interaction settle stamp below.
                lead_start = time.time()
                ran_interactions = False
                if interactions:
                    ran_interactions = True
                    try:
                        await _run_interactions_visible(page, interactions, variant_groups)
                        await page.wait_for_timeout(RENDER_SETTLE_MS)
                    except Exception as exc:
                        logger.warning(
                            "Step %d interactions failed (%s) — capturing plain page", i + 1, exc
                        )
                        step = {**step, "interaction_failed": True}

                # One final spinner check after interactions (clicks may trigger
                # new loading states, e.g. opening a modal with a skeleton).
                await wait_until_no_spinner(page)

                # Mark the moment the content is fully visible — this is the subtitle cue start
                content_visible_time = time.time()
                seen_urls.add(url_key)
                step_timings.append(content_visible_time - record_start)
                step_settles.append(settle_ms / 1000.0)
                # Lead = how long the interaction animation (cursor glide, ring,
                # ripple, press) took. The editor extends the segment backward by
                # this much so the button press is visible in the final video.
                step_leads.append(content_visible_time - lead_start if ran_interactions else 0.0)
                recorded_steps.append(step)
                recorded_audio.append(audio)

                # Linger on the page so the viewer can read the subtitle
                await page.wait_for_timeout(settle_ms)

            except Exception as exc:
                logger.error("Step %d failed unexpectedly (%s): %s", i + 1, url, exc)
                failed_steps.append(step)
                continue

        total_seconds = time.time() - record_start + 1.0  # 1 s buffer at end
        await page.wait_for_timeout(500)

        # Retrieve the video path BEFORE closing the context
        webm_path = await page.video.path()

        # Closing the context finalises the WebM file on disk
        await context.close()
        await browser.close()

    logger.info(
        "Recording complete: %d/%d steps captured, %d failed, %.1f s, file: %s",
        len(recorded_steps), len(video_script), len(failed_steps), total_seconds, webm_path,
    )

    return {
        "webm_path": str(webm_path),
        "step_timings": step_timings,
        "step_settles": step_settles,
        "step_leads": step_leads,
        "total_seconds": total_seconds,
        "recorded_steps": recorded_steps,
        "recorded_audio": recorded_audio,
        "failed_steps": failed_steps,
    }
