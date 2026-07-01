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

import difflib
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, BrowserContext

from backend.config import VIDEO_DIR
from backend.services import llm
from backend.services.screenshots import (
    _login,
    _enter_admin_app,
    _load_clickable_groups,
    _looks_like_error_page,
    _wait_for_loader_gone,
    _expand_variants,
    _find_clickable,
    _find_input,
    _resolve_clickable_with_retry,
    DESTRUCTIVE_TEXT,
    LOADER_SELECTORS,
    PAGE_LOAD_WAIT_MS,
    RENDER_SETTLE_MS,
)

logger = logging.getLogger(__name__)

VIEWPORT = {"width": 1920, "height": 1080}
MIN_SETTLE_MS = 1200  # minimum dwell time per step regardless of script value

# Selectors that indicate the page is still loading / not painted yet. Combines
# the screenshot pipeline's vetted LOADER_SELECTORS (loader/spinner/progressbar/
# aria-busy, case-insensitive) with skeleton placeholders so the recorder never
# starts a step's segment while the app's loading screen is still on display.
_SPINNER_SELECTORS = list(LOADER_SELECTORS) + [
    '[class*="skeleton" i]',
    '[class*="placeholder" i]',
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
    Return a CSS-ish selector for the first click/hover/fill target in the
    interaction list, so we can wait for it before stamping cue time.
    Returns None if nothing usable is found.
    """
    for step in interactions:
        kind = (step.get("type") or "click").lower()
        if kind in ("click", "hover"):
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
    0. ALWAYS first wait for any known loader/spinner overlay to disappear, so
       the app's loading screen is never the first frame of the step's recorded
       segment (the user explicitly does not want the loader in the video).
    1. If the step has interactions, wait for the first click/hover/fill target
       element to become visible — that proves the SPA has finished painting
       the relevant UI.
    2. Fall back to wait_until_no_spinner: poll until no visible spinner /
       loading / skeleton / aria-busy elements remain.
    3. If neither signal arrives, log a warning and proceed (fall back to
       current behaviour, which already waited for networkidle + RENDER_SETTLE_MS).
    """
    # Step 0: block on the app loader/spinner clearing before we judge readiness
    # or stamp any cue time. This runs on every step, including those with an
    # interaction target (whose DOM node can exist behind a loading overlay).
    try:
        await _wait_for_loader_gone(page)
    except Exception as exc:
        logger.debug("loader-gone wait failed (%s) — continuing", exc)

    target = _first_interaction_selector(interactions)

    if target:
        try:
            # For clicks/hovers, search using the same resolution chain as _find_clickable
            first_kind = (interactions[0].get("type") or "click").lower()
            if first_kind in ("click", "hover"):
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
    """Glide the visible cursor to the box centre and dwell briefly before the
    click so the target the narration is about is clearly pointed at.

    The purple highlight ring/box that used to be drawn here was removed at the
    user's request (it read as an intrusive purple square over the UI). The
    cursor glide and the injected click-ripple — which fires automatically on
    the real mousedown dispatched by element.click() — remain as the click cues.
    """
    cx = bbox["x"] + bbox["width"] / 2
    cy = bbox["y"] + bbox["height"] / 2
    await page.mouse.move(cx, cy, steps=18)
    await page.wait_for_timeout(550)


async def _run_interactions_visible(
    page: Page,
    interactions: list[dict],
    variant_groups: list[list[str]] | None = None,
    action_hint: str = "",
    language: str = "he",
) -> dict:
    """
    Execute interactions with visual feedback: the mouse glides to each target
    (driving the injected cursor) and a ripple fires on mousedown.

    Mirrors screenshots._run_interactions in behaviour (same destructive-action
    guard, same fill/click/hover/wait types) but adds the visual layer needed for
    video recording.

    Unlike the old version, this no longer raises when a planned click target is
    missing. Instead it attempts a live recovery (re-pick a real on-screen
    control) and reports the outcome to the caller, which decides whether to keep
    the step, mark it adapted, or degrade it to a scroll-tour. Returns a summary:

      {"any_success": bool, "any_adapted": bool, "any_failed": bool,
       "clicked_labels": list[str]}
    """
    variant_groups = variant_groups or []
    summary = {
        "any_success": False,
        "any_adapted": False,
        "any_failed": False,
        "clicked_labels": [],
    }

    for step in interactions:
        kind = (step.get("type") or "click").lower()

        if kind == "wait":
            await page.wait_for_timeout(int(step.get("ms", 1000)))
            continue

        if kind == "close":
            # Explicitly dismiss an open pop-up/modal so a following scroll (or the
            # rest of the step) acts on the PAGE behind it, not inside the pop-up.
            # Non-destructive close ladder (Escape, then a close/cancel control).
            if await _dialog_open(page):
                await _close_open_dialog(page)
                await page.wait_for_timeout(RENDER_SETTLE_MS)
            summary["any_success"] = True
            continue

        if kind == "scroll":
            target = (step.get("target") or "").strip()
            # Prefer scrolling a named section into view (searching inside an open
            # modal first); otherwise scroll the active container down one step.
            # This reveals content inside scrollable pop-ups, not just the window.
            revealed = False
            if target and target.lower() != "bottom":
                try:
                    revealed = bool(await page.evaluate(_JS_SCROLL_TO_TEXT, target))
                except Exception:
                    revealed = False
            if not revealed:
                try:
                    await page.evaluate(_JS_SCROLL_STEP, 0.7)
                except Exception:
                    await page.evaluate(
                        "() => window.scrollBy({top: 500, behavior: 'smooth'})"
                    )
            await page.wait_for_timeout(int(step.get("ms", 1200)))
            summary["any_success"] = True
            continue

        if kind == "fill":
            label = (step.get("label") or "").strip()
            value = (step.get("value") or "").strip()
            if not label:
                logger.warning("Skipping fill step with no label: %s", step)
                continue

            element = await _find_input(page, label)
            if element is None:
                logger.info("fill target %r not found — marking step interaction failed", label)
                summary["any_failed"] = True
                continue

            # Glide the cursor to the field BEFORE the click so the click into
            # the input is always shown (cursor + ripple), never an invisible
            # press. Inputs need no interactivity probe — they are inherently
            # fillable controls.
            bbox = await _genuine_target_bbox(page, element, require_interactive=False)
            if bbox:
                await _glide_and_highlight(page, bbox)

            await element.click(timeout=5000)
            await element.fill(value)

            actual = await element.input_value()
            if not actual and value:
                logger.debug("fill() produced empty value for %r — retrying with press_sequentially", label)
                await element.clear()
                await element.press_sequentially(value, delay=50)

            await page.wait_for_timeout(500)
            summary["any_success"] = True
            continue

        if kind == "hover":
            text = (step.get("text") or "").strip()
            if not text:
                logger.warning("Skipping hover step with no text: %s", step)
                continue

            candidates = _expand_variants(text, variant_groups)
            element = await _resolve_clickable_with_retry(page, candidates)
            if element is None:
                logger.info("hover %r unresolved — marking failed", text)
                summary["any_failed"] = True
                continue

            bbox = await _genuine_target_bbox(page, element)
            if bbox is None:
                logger.info("hover %r has no genuine visible target — marking failed", text)
                summary["any_failed"] = True
                continue

            await _glide_and_highlight(page, bbox)
            await page.wait_for_timeout(RENDER_SETTLE_MS)
            summary["any_success"] = True
            continue

        if kind == "click":
            text = (step.get("text") or "").strip()
            if not text:
                logger.warning("Skipping click step with no text: %s", step)
                continue
            if DESTRUCTIVE_TEXT.search(text):
                logger.info("refusing destructive planned click %r — marking failed", text)
                summary["any_failed"] = True
                continue

            candidates = _expand_variants(text, variant_groups)
            element = await _resolve_clickable_with_retry(page, candidates)

            adapted_this = False
            chosen_label = text
            bbox = None
            if element is not None:
                bbox = await _genuine_target_bbox(page, element)

            # Live recovery: if the planned label could not be resolved to a
            # genuine on-screen control, re-pick a real one from the page.
            if element is None or bbox is None:
                recovered = await _recover_click_target(page, text, action_hint, language)
                if recovered is not None:
                    element, bbox, chosen_label = recovered
                    adapted_this = True
                elif element is None:
                    logger.info("click %r unresolved and no recovery target", text)
                    summary["any_failed"] = True
                    continue

            # Toggle-aware: if the target is already expanded (e.g. a menu the
            # page opened by default, or one opened by a previous interaction),
            # clicking it would CLOSE it. Don't click — but still glide the
            # cursor (no ripple, no click) so the viewer sees the element the
            # narration is about, then leave it open.
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
                    chosen_label,
                )
                if bbox:
                    await _glide_and_highlight(page, bbox)
                await page.wait_for_timeout(RENDER_SETTLE_MS)
                summary["any_success"] = True
                if adapted_this:
                    summary["any_adapted"] = True
                summary["clicked_labels"].append(chosen_label)
                continue

            if bbox is None:
                logger.info("click %r has no genuine visible target — marking failed", text)
                summary["any_failed"] = True
                continue

            # Glide the cursor to the target → real click (fires the ripple).
            await _glide_and_highlight(page, bbox)
            await element.click(timeout=5000)
            await page.wait_for_timeout(RENDER_SETTLE_MS)
            summary["any_success"] = True
            if adapted_this:
                summary["any_adapted"] = True
            summary["clicked_labels"].append(chosen_label)
            continue

        logger.warning("Unknown interaction type %r — skipping: %s", kind, step)

    return summary


def _url_key(url: str) -> str:
    """Normalise a URL for dedup comparison (strip trailing slash and query string)."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"


def _has_screen_changing_interactions(interactions: list[dict]) -> bool:
    """True only if at least one interaction visibly changes the recorded step."""
    return any(
        (step.get("type") or "click").lower() in ("click", "hover", "fill", "scroll")
        for step in interactions
    )


# ── Live recovery + verification helpers (Groups C & D) ───────────────────────

# Enumerate the genuinely visible, on-screen, named interactive controls so we
# can re-pick a real target when the planned label cannot be resolved.
_JS_LIST_CONTROLS = r"""() => {
  const sel = 'button, a, [role="button"], [role="tab"], [role="menuitem"],' +
    ' [role="option"], [role="link"], [role="radio"], [role="checkbox"],' +
    ' input, select, textarea';
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    if (r.bottom < 0 || r.top > window.innerHeight) continue;
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || '')
      .trim().replace(/\s+/g, ' ').slice(0, 80);
    if (!name) continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(name);
    if (out.length >= 40) break;
  }
  return out;
}"""


def _fuzzy_pick(intended: str, names: list[str], cutoff: float = 0.82) -> str | None:
    """Cheap, deterministic match of *intended* against live control *names*.

    Accepts only confident matches (exact, containment, or a high-ratio close
    match) so a weak guess never triggers a wrong click — the LLM tiebreak and
    the scroll-tour fallback handle the uncertain cases.
    """
    norm = " ".join((intended or "").split()).lower()
    if not norm or not names:
        return None
    lowered = {" ".join(n.split()).lower(): n for n in names}
    if norm in lowered:
        return lowered[norm]
    for low, original in lowered.items():
        if norm in low or low in norm:
            return original
    match = difflib.get_close_matches(norm, list(lowered.keys()), n=1, cutoff=cutoff)
    return lowered[match[0]] if match else None


async def _recover_click_target(
    page: Page, intended_text: str, action_hint: str, language: str,
):
    """Re-pick a real, clickable target when the planned label is missing.

    Scans the page's visible controls, fuzzy-matches the intended label, and
    falls back to an LLM tiebreak (which may decline). Returns (element, bbox,
    chosen_label) for a genuine on-screen interactive target, or None.
    """
    try:
        names = await page.evaluate(_JS_LIST_CONTROLS)
    except Exception:
        names = []
    if not names:
        return None

    choice = _fuzzy_pick(intended_text, names)
    if choice is None:
        try:
            choice = await llm.pick_live_target(
                goal=action_hint or intended_text,
                intended_label=intended_text,
                candidates=names,
                language=language,
            )
        except Exception as exc:
            logger.debug("pick_live_target failed: %s", exc)
            choice = None
    if not choice or DESTRUCTIVE_TEXT.search(choice):
        return None

    element = await _find_clickable(page, choice)
    if element is None:
        return None
    bbox = await _genuine_target_bbox(page, element)
    if bbox is None:
        return None
    logger.info("Recovered click target: planned %r -> live %r", intended_text, choice)
    return element, bbox, choice


async def _page_signature(page: Page) -> str:
    """A cheap fingerprint of the visible page state, used to verify that a click
    actually changed the screen (URL, DOM size, text volume, open dialogs)."""
    try:
        return await page.evaluate(
            r"""() => {
                const nodes = document.querySelectorAll('*').length;
                const txt = document.body ? document.body.innerText.length : 0;
                const dialogs = document.querySelectorAll(
                    '[role="dialog"], [aria-modal="true"]'
                ).length;
                return location.href + '|' + nodes + '|' + txt + '|' + dialogs;
            }"""
        )
    except Exception:
        return ""


# ── Scroll helpers (Issue #3) ────────────────────────────────────────────────
# The narration often describes content that is below the fold OR inside a
# scrollable pop-up/modal (e.g. the file-upload dialog after "Advanced options").
# Plain window scrolling does not move a modal's inner scroll area, so the screen
# never shows what the narration is talking about. These helpers find the ACTIVE
# scroll container — the largest scrollable element inside an open dialog, else
# the largest scrollable element on the page, else the window — and scroll THAT.

# Picks the active scroll container and scrolls it by a fraction of its height.
# Returns {scrolled, atBottom, inDialog} so the caller knows whether more remains.
_JS_SCROLL_STEP = r"""(ratio) => {
  function scrollableInside(root) {
    const cands = [root, ...root.querySelectorAll('*')];
    let best = null, bestArea = 0;
    for (const el of cands) {
      if (el.scrollHeight - el.clientHeight <= 16) continue;
      const st = getComputedStyle(el);
      if (st.overflowY !== 'auto' && st.overflowY !== 'scroll') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 40) continue;
      const area = r.width * r.height;
      if (area > bestArea) { bestArea = area; best = el; }
    }
    return best;
  }
  const dlgs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog')]
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 40 && r.height > 40; });
  let container = null, inDialog = false;
  if (dlgs.length) { container = scrollableInside(dlgs[dlgs.length - 1]); inDialog = !!container; }
  if (!container) container = scrollableInside(document.body);
  if (container) {
    container.scrollBy({top: Math.round(container.clientHeight * ratio), behavior: 'smooth'});
    const atBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 4;
    return {scrolled: true, atBottom, inDialog};
  }
  window.scrollBy({top: Math.round(window.innerHeight * ratio), behavior: 'smooth'});
  const atBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 4;
  return {scrolled: true, atBottom, inDialog: false};
}"""

# Resets the active scroll container (modal body or window) back to the top.
_JS_SCROLL_RESET = r"""() => {
  const dlgs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog')]
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 40 && r.height > 40; });
  function scrollableInside(root) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (el.scrollHeight - el.clientHeight <= 16) continue;
      const st = getComputedStyle(el);
      if (st.overflowY === 'auto' || st.overflowY === 'scroll') {
        const r = el.getBoundingClientRect();
        if (r.width >= 40 && r.height >= 40) return el;
      }
    }
    return null;
  }
  let c = dlgs.length ? scrollableInside(dlgs[dlgs.length - 1]) : null;
  if (!c) c = scrollableInside(document.body);
  if (c) c.scrollTo({top: 0, behavior: 'smooth'});
  else window.scrollTo({top: 0, behavior: 'smooth'});
}"""

# Jumps the active scroll container (modal body or window) to the top INSTANTLY
# (no animation). Used as a per-step baseline so each step begins at the top and
# narration about top-of-page content is never shown over a leftover scrolled-down
# view inherited from the previous step.
_JS_SCROLL_TOP_INSTANT = r"""() => {
  const dlgs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog')]
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 40 && r.height > 40; });
  function scrollableInside(root) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (el.scrollHeight - el.clientHeight <= 16) continue;
      const st = getComputedStyle(el);
      if (st.overflowY === 'auto' || st.overflowY === 'scroll') {
        const r = el.getBoundingClientRect();
        if (r.width >= 40 && r.height >= 40) return el;
      }
    }
    return null;
  }
  let c = dlgs.length ? scrollableInside(dlgs[dlgs.length - 1]) : null;
  if (!c) c = scrollableInside(document.body);
  if (c) c.scrollTo({top: 0, behavior: 'auto'});
  else window.scrollTo({top: 0, behavior: 'auto'});
}"""

# Scrolls a heading/label/text matching *target* into view, searching inside an
# open dialog first (so a modal's inner content is revealed). Returns true on hit.
_JS_SCROLL_TO_TEXT = r"""(target) => {
  if (!target) return false;
  const dlgs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog')]
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 40 && r.height > 40; });
  const scope = dlgs.length ? dlgs[dlgs.length - 1] : document;
  const sel = 'h1,h2,h3,h4,h5,h6,[class*="title"],[class*="header"],label,legend,p,span,div,a,button';
  const nodes = [...scope.querySelectorAll(sel)];
  const match = nodes.find(e => e.textContent && e.textContent.trim().includes(target));
  if (match) { match.scrollIntoView({behavior: 'smooth', block: 'center'}); return true; }
  return false;
}"""


# ── Dialog / pop-up awareness + cleanup (Issue #1) ───────────────────────────
# When a click opens a modal/dialog, it must be closed before moving on to a
# step that does not use it — otherwise the pop-up lingers over later content.

# True when a real, sizeable modal dialog is currently open on the page.
_JS_DIALOG_OPEN = r"""() => {
  const dlgs = document.querySelectorAll(
    '[role="dialog"],[aria-modal="true"],dialog[open]'
  );
  for (const d of dlgs) {
    const r = d.getBoundingClientRect();
    if (r.width > 80 && r.height > 80) {
      const st = getComputedStyle(d);
      if (st.visibility !== 'hidden' && st.display !== 'none') return true;
    }
  }
  return false;
}"""

# Clicks an explicit close/cancel control inside the topmost open dialog.
# Conservative matching (no bare "x") so we never click an unrelated control.
_JS_CLOSE_DIALOG_BTN = r"""() => {
  const dlgs = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],dialog[open]')]
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 80 && r.height > 80; });
  if (!dlgs.length) return false;
  const dlg = dlgs[dlgs.length - 1];
  const closeRe = /(^|\s)(close|cancel|dismiss)(\s|$)|סגור|סגירה|ביטול|בטל|^[×✕⨯✖]$/i;
  const btns = dlg.querySelectorAll('button,[role="button"],[aria-label]');
  for (const b of btns) {
    const r = b.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) continue;
    const label = (b.getAttribute('aria-label') || b.innerText || b.title || '').trim();
    if (label && closeRe.test(label)) { b.click(); return true; }
  }
  return false;
}"""


async def _dialog_open(page: Page) -> bool:
    try:
        return bool(await page.evaluate(_JS_DIALOG_OPEN))
    except Exception:
        return False


async def _close_open_dialog(page: Page) -> bool:
    """Close a lingering dialog using a non-destructive ladder: Escape, then an
    explicit close/cancel control inside the dialog. Returns True once no dialog
    remains. Never clicks a save/submit/delete control (close labels only)."""
    for _ in range(2):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(350)
        if not await _dialog_open(page):
            return True
    try:
        await page.evaluate(_JS_CLOSE_DIALOG_BTN)
    except Exception:
        pass
    await page.wait_for_timeout(400)
    return not await _dialog_open(page)


async def _step_uses_open_dialog(
    page: Page, interactions: list[dict], variant_groups: list[list[str]],
) -> bool:
    """True when the step's first actionable interaction targets an element that
    lives INSIDE the currently-open dialog — i.e. the step still needs the dialog,
    so it must not be closed. Resolves the target against the current page state."""
    for step in interactions:
        kind = (step.get("type") or "click").lower()
        if kind == "close":
            # The step explicitly wants to dismiss the pop-up (e.g. to then scroll
            # the page behind it), so it does NOT need the dialog kept open.
            return False
        if kind == "scroll":
            # A scroll step explores the CURRENT view. When a dialog is open, the
            # scroll is meant to move within that pop-up (container-aware scrolling
            # targets the open dialog), so the dialog must stay open. Deciding on
            # the first actionable interaction: a scroll-first step keeps the popup.
            return True
        if kind == "click":
            text = (step.get("text") or "").strip()
            if not text:
                continue
            candidates = _expand_variants(text, variant_groups)
            element = None
            for cand in candidates:
                element = await _find_clickable(page, cand)
                if element is not None:
                    break
        elif kind == "fill":
            label = (step.get("label") or "").strip()
            if not label:
                continue
            element = await _find_input(page, label)
        else:
            continue
        if element is None:
            return False
        try:
            return bool(await element.evaluate(
                "(el) => !!el.closest('[role=\"dialog\"],[aria-modal=\"true\"],dialog')"
            ))
        except Exception:
            return False
    return False


async def _run_scroll_tour(page: Page) -> None:
    """Smoothly scroll through the page (or an open scrollable modal) so the
    viewer sees real content when no interaction could be performed (the graceful
    fallback that keeps a step from becoming a dead, static frame).

    Scrolls the ACTIVE scroll container — a scrollable modal body when a dialog is
    open, otherwise the window — so a tour inside a pop-up reveals its content
    instead of leaving it static while the page behind it does not move."""
    try:
        for _ in range(2):
            result = await page.evaluate(_JS_SCROLL_STEP, 0.7)
            await page.wait_for_timeout(1200)
            if isinstance(result, dict) and result.get("atBottom"):
                break
        await page.evaluate(_JS_SCROLL_RESET)
        await page.wait_for_timeout(600)
    except Exception as exc:
        logger.debug("scroll tour failed: %s", exc)


async def record_product_video(
    video_script: list[dict],
    base_url: str = "https://jeenai.app",
    link_type: str = "regular",
    session_id: str = "default",
    audio_results: list[dict | None] | None = None,
    language: str = "he",
    on_step_status: "callable | None" = None,
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
    recorded_url_keys: list[str] = []
    outcomes: list[str] = []  # per recorded step: planned | adapted | toured
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
                logger.info(
                    "📋 Step %d/%d [SKIPPED] %s — duplicate screen",
                    i + 1, len(video_script), action,
                )
                if on_step_status:
                    on_step_status(i + 1, len(video_script), "skipped", action, url)
                continue

            logger.info(
                "Recording step %d/%d: %s — %s", i + 1, len(video_script), url, action
            )

            try:
                # Resolve variant groups once (used by the dialog-cleanup check,
                # the ready-wait, and the interactions runner).
                route_path = urlparse(url).path.rstrip("/") or "/"
                variant_groups = _load_clickable_groups(route_path, link_type)

                if needs_navigation:
                    # Navigating to a new URL discards any open dialog from the
                    # previous step, so no explicit close is needed here.
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    status = response.status if response else None
                    if status and status >= 400:
                        logger.warning(
                            "📋 Step %d/%d [FAILED] %s — HTTP %d",
                            i + 1, len(video_script), action, status,
                        )
                        if on_step_status:
                            on_step_status(i + 1, len(video_script), "failed", action, url)
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
                            "📋 Step %d/%d [FAILED] %s — redirected to %s",
                            i + 1, len(video_script), action, final_url,
                        )
                        if on_step_status:
                            on_step_status(i + 1, len(video_script), "failed", action, url)
                        failed_steps.append(step)
                        continue

                    # Detect 404 / blank / error pages
                    error_reason = await _looks_like_error_page(page)
                    if error_reason:
                        logger.warning(
                            "📋 Step %d/%d [FAILED] %s — %s",
                            i + 1, len(video_script), action, error_reason,
                        )
                        if on_step_status:
                            on_step_status(i + 1, len(video_script), "failed", action, url)
                        failed_steps.append(step)
                        continue
                else:
                    logger.info("Step %d — same URL, continuing from current state", i + 1)
                    # Pop-up awareness: a dialog opened by a previous step lingers
                    # on the same screen. If THIS step does not act inside that
                    # dialog, close it so it doesn't sit over unrelated content.
                    if await _dialog_open(page):
                        if await _step_uses_open_dialog(page, interactions, variant_groups):
                            logger.info("Step %d — keeping open dialog (step acts inside it)", i + 1)
                        else:
                            logger.info("Step %d — closing leftover dialog before continuing", i + 1)
                            await _close_open_dialog(page)

                # ── Wait for the SPA to finish painting ──
                await _wait_for_page_ready(page, interactions, variant_groups)

                # Baseline scroll position: start the step at the TOP of the page
                # so a step that scrolled down does not leave the next same-URL
                # step's narration playing over a scrolled-down view (e.g. scrolling
                # down, then talking about top-of-page content). Scroll interactions
                # in THIS step then move down from a known top. Done instantly (no
                # animation) so it never appears as motion in the recording.
                # IMPORTANT: skip this when a pop-up/modal is open — a button was
                # just clicked to open it and the step will scroll WITHIN it; force-
                # resetting would fight the popup's own scroll position.
                try:
                    if not await _dialog_open(page):
                        await page.evaluate(_JS_SCROLL_TOP_INSTANT)
                except Exception:
                    pass

                # Run interactions with visible cursor and click ripple.
                # The runner no longer fails the step on a missing target: it
                # tries a live recovery (re-pick a real on-screen control). The
                # fallback ladder is planned -> adapted -> guided scroll-tour, so
                # a step is never a dead, static frame. We verify the screen
                # actually changed and degrade to a scroll-tour when nothing
                # effective happened. Mark when the (painted) page is ready so the
                # jump-cut editor can keep the interaction animation.
                lead_start = time.time()
                ran_interactions = False
                outcome = "planned"
                if interactions:
                    ran_interactions = True
                    sig_before = await _page_signature(page)
                    try:
                        summary = await _run_interactions_visible(
                            page, interactions, variant_groups,
                            action_hint=action, language=language,
                        )
                    except Exception as exc:
                        logger.warning("Step %d interactions errored (%s)", i + 1, exc)
                        summary = {
                            "any_success": False, "any_adapted": False,
                            "any_failed": True, "clicked_labels": [],
                        }
                    await page.wait_for_timeout(RENDER_SETTLE_MS)
                    sig_after = await _page_signature(page)
                    screen_changed = bool(sig_before) and sig_before != sig_after

                    landed = summary["any_success"] or summary["any_adapted"]
                    expected_change = _has_screen_changing_interactions(interactions)

                    if not landed or (
                        expected_change and not screen_changed
                        and not summary["any_success"]
                    ):
                        logger.info(
                            "Step %d — no effective interaction; showing a guided scroll-tour",
                            i + 1,
                        )
                        await _run_scroll_tour(page)
                        outcome = "toured"
                        step = {**step, "interaction_failed": True, "outcome": "toured"}
                    elif summary["any_adapted"]:
                        outcome = "adapted"
                        step = {
                            **step,
                            "interaction_failed": True,
                            "outcome": "adapted",
                            "adapted_labels": summary["clicked_labels"],
                        }
                    else:
                        outcome = "planned"
                        step = {**step, "outcome": "planned"}
                else:
                    step = {**step, "outcome": "planned"}

                # One final spinner check after interactions (clicks may trigger
                # new loading states, e.g. opening a modal with a skeleton).
                await wait_until_no_spinner(page)

                # Dedup: drop a step that adds nothing new — an already-seen
                # screen where no planned/adapted action landed (e.g. a degraded
                # scroll-tour of a page we already showed). This prevents the
                # "same static page three times" failure mode.
                is_new_url = url_key not in seen_urls
                showed_new = is_new_url or outcome in ("planned", "adapted")
                if not showed_new:
                    logger.info(
                        "📋 Step %d/%d [SKIPPED] %s — already-seen screen with no new content",
                        i + 1, len(video_script), action,
                    )
                    if on_step_status:
                        on_step_status(i + 1, len(video_script), "skipped", action, url)
                    continue

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
                recorded_url_keys.append(url_key)
                outcomes.append(outcome)

                _status_labels = {
                    "planned": "PLANNED",
                    "adapted": "ADAPTED",
                    "toured": "TOURED",
                }
                status_label = _status_labels.get(outcome, outcome.upper())
                adapted_detail = ""
                if outcome == "adapted" and step.get("adapted_labels"):
                    adapted_detail = f" (live targets: {step['adapted_labels']})"
                logger.info(
                    "📋 Step %d/%d [%s] %s%s",
                    i + 1, len(video_script), status_label, action, adapted_detail,
                )
                if on_step_status:
                    on_step_status(i + 1, len(video_script), outcome, action, url)

                # Linger on the page so the viewer can read the subtitle
                await page.wait_for_timeout(settle_ms)

            except Exception as exc:
                logger.error(
                    "📋 Step %d/%d [FAILED] %s — %s",
                    i + 1, len(video_script), action, exc,
                )
                if on_step_status:
                    on_step_status(i + 1, len(video_script), "failed", action, url)
                failed_steps.append(step)
                continue

        total_seconds = time.time() - record_start + 1.0  # 1 s buffer at end
        await page.wait_for_timeout(500)

        # Retrieve the video path BEFORE closing the context
        webm_path = await page.video.path()

        # Closing the context finalises the WebM file on disk
        await context.close()
        await browser.close()

    # ── Per-run recording report: how each planned step actually resolved ──
    n_planned = outcomes.count("planned")
    n_adapted = outcomes.count("adapted")
    n_toured = outcomes.count("toured")
    n_skipped = len(video_script) - len(recorded_steps) - len(failed_steps)
    logger.info(
        "Recording complete: %d/%d steps captured, %d failed, %.1f s, file: %s",
        len(recorded_steps), len(video_script), len(failed_steps), total_seconds, webm_path,
    )
    logger.info(
        "── Recording report ──\n"
        "  planned (clicked as scripted): %d\n"
        "  adapted (re-picked live target): %d\n"
        "  toured  (scroll-tour fallback):  %d\n"
        "  skipped (duplicate/empty):       %d\n"
        "  failed  (nav/error -> slide):    %d",
        n_planned, n_adapted, n_toured, n_skipped, len(failed_steps),
    )
    for idx, (st, oc) in enumerate(zip(recorded_steps, outcomes)):
        detail = ""
        if oc == "adapted" and st.get("adapted_labels"):
            detail = f" -> live: {st['adapted_labels']}"
        logger.info("  step %d: %s [%s]%s", idx + 1, st.get("action", "?"), oc, detail)

    return {
        "webm_path": str(webm_path),
        "step_timings": step_timings,
        "step_settles": step_settles,
        "step_leads": step_leads,
        "total_seconds": total_seconds,
        "recorded_steps": recorded_steps,
        "recorded_audio": recorded_audio,
        "outcomes": outcomes,
        "failed_steps": failed_steps,
        "report": {
            "planned": n_planned,
            "adapted": n_adapted,
            "toured": n_toured,
            "skipped": n_skipped,
            "failed": len(failed_steps),
        },
    }
