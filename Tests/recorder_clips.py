"""
Clips-style smooth browser recorder (standalone test build).

Records a real browser session the same way the open-source Agent-Native
"Clips" recorder does: instead of Playwright's choppy ``record_video`` (which
resamples screenshots), it opens a live ``MediaStream`` of the tab via
``getDisplayMedia`` and pipes it into an in-browser ``MediaRecorder``.  The
browser samples the compositor at a constant frame rate, so cursor glides and
UI animations stay smooth.

Mirrors the Clips constants found in
``templates/clips/desktop/src/lib/recorder.ts``:
    CAPTURE_FRAME_RATE   (24 there; 30 here for smoother motion)
    MAX_WIDTH / HEIGHT   1920 x 1080
    VIDEO_BITRATE_BPS    1_200_000

Why two tabs
------------
A ``MediaRecorder`` lives in a page's JS context, which Chromium destroys on
every full ``page.goto``.  Clips keeps its recorder surface separate from the
captured surface, so we do the same:

  * Content tab (C): logs in, navigates, runs interactions with the visible
    cursor / highlight / ripple.  Navigates freely.
  * Recorder tab (R): blank persistent tab that calls ``getDisplayMedia`` to
    capture tab C and runs the ``MediaRecorder``.  Never navigates, so the
    recording survives all of C's page loads.

Chromium launch flags make this picker-free and gesture-free:
  --use-fake-ui-for-media-stream                  (auto-grant the capture)
  --auto-select-tab-capture-source-by-title=<M>   (auto-pick tab C by title)

C is forced to carry a unique title marker via an init script so the auto-select
flag always lands on it.  ``getDisplayMedia`` still needs a user gesture, so it
is invoked from a trusted Playwright click on a button injected into R.

This module does NOT modify the existing ``recorder.py`` pipeline; it reuses its
helpers read-only.

Fallback
--------
If ``getDisplayMedia`` ever misbehaves on a given machine, the robust
alternative is CDP ``Page.startScreencast`` (frames survive navigation over the
CDP connection) muxed to constant-fps video with ``pip install imageio-ffmpeg``
(ships a static ffmpeg binary).  That path is intentionally not built here.

Outputs
-------
  * <run_id>.webm                 smooth recording written directly by the browser
  * <run_id>.execution_log.json   Remotion-ready metadata: timestamps of every
                                  navigation, page-ready, click, and fill, with
                                  element bounding boxes, all relative to the
                                  instant recording started.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, BrowserContext

from backend.config import VIDEO_PROJECT_DIR
from backend.services.screenshots import (
    _login,
    _enter_admin_app,
    _load_clickable_groups,
    _looks_like_error_page,
    _expand_variants,
    _find_input,
    _resolve_clickable_with_retry,
    DESTRUCTIVE_TEXT,
    PAGE_LOAD_WAIT_MS,
    RENDER_SETTLE_MS,
)
from backend.services.recorder import (
    _CURSOR_INIT_JS,
    _JS_HIGHLIGHT_SHOW,
    _JS_HIGHLIGHT_HIDE,
    wait_until_no_spinner,
    _wait_for_page_ready,
)

logger = logging.getLogger(__name__)

# ── Clips-mirrored capture constants ─────────────────────────────────────────
CAPTURE_FRAME_RATE = 30          # Clips uses 24; 30 for smoother cursor motion
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
VIDEO_BITRATE_BPS = 1_200_000    # matches Clips RECORDING_VIDEO_BITRATE_BPS
VIEWPORT = {"width": MAX_WIDTH, "height": MAX_HEIGHT}
MIN_SETTLE_MS = 1200

# Preferred MediaRecorder codecs, best first. The page picks the first supported.
_MIME_CANDIDATES = [
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/webm",
]


def _find_ffmpeg() -> str | None:
    """Locate an ffmpeg binary: system PATH first, else the one Playwright bundles
    in ~/Library/Caches/ms-playwright/ffmpeg-*/. Used only by the CDP fallback."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        str(Path.home() / "Library" / "Caches" / "ms-playwright"),
        str(Path.home() / ".cache" / "ms-playwright"),
    ]
    for root in roots:
        if not root:
            continue
        for name in ("ffmpeg-mac", "ffmpeg-mac-arm64", "ffmpeg-linux", "ffmpeg.exe", "ffmpeg"):
            for match in glob.glob(str(Path(root) / "ffmpeg-*" / name)):
                if os.access(match, os.X_OK):
                    return match
    return None


def _encode_frames_to_webm(
    frames: list[tuple[float, bytes]],
    webm_path: Path,
    fps: int,
    ffmpeg: str,
) -> None:
    """Mux variable-interval CDP screencast JPEG frames into a constant-fps webm.

    Playwright's bundled ffmpeg is a minimal build (only the image2pipe demuxer +
    libvpx/webm), so we resample to constant fps ourselves: each captured frame
    is held (repeated) across every 1/fps slot until the next frame arrives, then
    the resulting fixed-cadence JPEG stream is piped through image2pipe. Holding
    the last frame across gaps is what removes the choppiness of recordVideo.
    """
    if not frames:
        raise RuntimeError("no screencast frames captured")

    t0 = frames[0][0]
    rels = [max(ts - t0, 0.0) for ts, _ in frames]
    duration = rels[-1]
    n_slots = max(1, int(round(duration * fps)) + 1)

    stream = bytearray()
    idx = 0
    for n in range(n_slots):
        t = n / fps
        while idx + 1 < len(frames) and rels[idx + 1] <= t:
            idx += 1
        stream += frames[idx][1]

    cmd = [
        ffmpeg, "-y",
        "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "pipe:0",
        "-c:v", "libvpx",
        "-b:v", str(VIDEO_BITRATE_BPS),
        "-pix_fmt", "yuv420p",
        "-auto-alt-ref", "0",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        str(webm_path),
    ]
    logger.info(
        "Encoding %d frames -> %d slots @ %dfps -> %s",
        len(frames), n_slots, fps, webm_path.name,
    )
    proc = subprocess.run(cmd, input=bytes(stream), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg encode failed ({proc.returncode}): {proc.stderr.decode(errors='ignore')[-800:]}"
        )


def _title_marker_init_js(marker: str) -> str:
    """Init script that forces a stable, unique title on the content tab so the
    --auto-select-tab-capture-source-by-title flag always selects it."""
    safe = json.dumps(marker)
    return (
        "(() => {"
        f"  const T = {safe};"
        "  const set = () => { try { if (document.title !== T) document.title = T; } catch (e) {} };"
        "  set();"
        "  try {"
        "    const obs = new MutationObserver(set);"
        "    const start = () => obs.observe(document.documentElement, {subtree:true, childList:true});"
        "    if (document.documentElement) start();"
        "    else document.addEventListener('DOMContentLoaded', start);"
        "  } catch (e) {}"
        "  setInterval(set, 500);"
        "})();"
    )


# Recorder-tab script: defines start/stop that drive a MediaRecorder over a
# getDisplayMedia stream and return the finished webm as base64. getDisplayMedia
# is called from the button's click handler (trusted -> user activation).
_RECORDER_SETUP_JS = r"""
(opts) => {
  window.__clip = { chunks: [], error: null, ready: false };

  function pickMime(list) {
    for (const m of list) {
      try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m; } catch (e) {}
    }
    return '';
  }

  window.__clipStart = async function () {
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: {
          frameRate: { ideal: opts.fps, max: opts.fps },
          width: { ideal: opts.width },
          height: { ideal: opts.height },
        },
        audio: false,
        preferCurrentTab: false,
      });
      window.__clip.stream = stream;
      const mimeType = pickMime(opts.mimes);
      const recOpts = { videoBitsPerSecond: opts.bitrate };
      if (mimeType) recOpts.mimeType = mimeType;
      const mr = new MediaRecorder(stream, recOpts);
      window.__clip.recorder = mr;
      window.__clip.mimeType = mimeType || 'video/webm';
      mr.ondataavailable = (e) => { if (e.data && e.data.size) window.__clip.chunks.push(e.data); };
      mr.start(100);
      window.__clip.ready = true;
    } catch (err) {
      window.__clip.error = (err && err.message) ? err.message : String(err);
    }
  };

  window.__clipStop = function () {
    return new Promise((resolve, reject) => {
      const c = window.__clip;
      if (!c || !c.recorder) { reject(new Error('recorder not started')); return; }
      c.recorder.onstop = async () => {
        try {
          const blob = new Blob(c.chunks, { type: c.mimeType });
          const buf = await blob.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let binary = '';
          const CH = 0x8000;
          for (let i = 0; i < bytes.length; i += CH) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
          }
          try { c.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
          resolve(btoa(binary));
        } catch (e) { reject(e); }
      };
      try { c.recorder.stop(); } catch (e) { reject(e); }
    });
  };

  const btn = document.createElement('button');
  btn.id = '__clipStartBtn';
  btn.textContent = 'start clip';
  btn.style.cssText = 'position:fixed;top:0;left:0;z-index:2147483647;';
  btn.addEventListener('click', () => { window.__clip._startPromise = window.__clipStart(); });
  document.body.appendChild(btn);
}
"""


def _url_key(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"


def _has_screen_changing_interactions(interactions: list[dict]) -> bool:
    return any(
        (step.get("type") or "click").lower() in ("click", "fill")
        for step in interactions
    )


async def _bbox_dict(element) -> dict | None:
    """Return a JSON-friendly bounding box for an element handle/locator."""
    try:
        bbox = await element.bounding_box()
    except Exception:
        return None
    if not bbox:
        return None
    return {
        "x": round(bbox["x"], 1),
        "y": round(bbox["y"], 1),
        "width": round(bbox["width"], 1),
        "height": round(bbox["height"], 1),
    }


async def _run_interactions_clips(
    page: Page,
    interactions: list[dict],
    variant_groups: list[list[str]],
    now,
    log_event,
) -> None:
    """
    Execute interactions with the visible cursor / highlight / ripple (same
    resolution and destructive-action guards as recorder._run_interactions_visible)
    while logging the bounding box and timestamps of each event so the metadata
    JSON can drive Remotion overlays.

    Raises on failure so the caller can flag the step.
    """
    for step in interactions:
        kind = (step.get("type") or "click").lower()

        if kind == "wait":
            ms = int(step.get("ms", 1000))
            log_event({"type": "wait", "ms": ms, "t": now()})
            await page.wait_for_timeout(ms)
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

            t_start = now()
            bbox = await _bbox_dict(element)
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await page.mouse.move(cx, cy, steps=18)
                await page.wait_for_timeout(200)

            await element.click(timeout=5000)
            await element.fill(value)
            actual = await element.input_value()
            if not actual and value:
                await element.clear()
                await element.press_sequentially(value, delay=50)
            await page.wait_for_timeout(500)

            log_event({
                "type": "fill",
                "label": label,
                "bbox": bbox,
                "t_start": t_start,
                "t_end": now(),
            })
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

            bbox = await _bbox_dict(element)
            t_move = t_highlight = None
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await page.mouse.move(cx, cy, steps=18)
                await page.wait_for_timeout(200)
                t_move = now()
                await page.evaluate(_JS_HIGHLIGHT_SHOW, bbox)
                await page.wait_for_timeout(350)
                t_highlight = now()

            await element.click(timeout=5000)
            t_click = now()
            await page.evaluate(_JS_HIGHLIGHT_HIDE)
            await page.wait_for_timeout(RENDER_SETTLE_MS)

            log_event({
                "type": "click",
                "text": text,
                "candidates": candidates,
                "bbox": bbox,
                "t_move": t_move,
                "t_highlight": t_highlight,
                "t_click": t_click,
            })
            continue

        logger.warning("Unknown interaction type %r — skipping: %s", kind, step)


async def _start_mediarecorder_capture(context, content, base_url, marker):
    """Clips-faithful capture: a persistent recorder tab grabs tab C via
    getDisplayMedia and runs a MediaRecorder. Returns the recorder page on
    success, or raises RuntimeError if the video source can't start (common in
    headless), so the caller can fall back to CDP screencast."""
    recorder = await context.new_page()
    await recorder.goto(base_url, wait_until="domcontentloaded", timeout=30000)
    await recorder.evaluate(_RECORDER_SETUP_JS, {
        "fps": CAPTURE_FRAME_RATE,
        "width": MAX_WIDTH,
        "height": MAX_HEIGHT,
        "bitrate": VIDEO_BITRATE_BPS,
        "mimes": _MIME_CANDIDATES,
    })
    await recorder.click("#__clipStartBtn")

    deadline = time.time() + 15
    cap_error = None
    while time.time() < deadline:
        state = await recorder.evaluate(
            "() => ({ ready: window.__clip && window.__clip.ready, error: window.__clip && window.__clip.error })"
        )
        if state.get("error"):
            cap_error = state["error"]
            break
        if state.get("ready"):
            await content.bring_to_front()
            return recorder
        await recorder.wait_for_timeout(150)
    cap_error = cap_error or "MediaRecorder did not start within 15s"
    try:
        await recorder.close()
    except Exception:
        pass
    raise RuntimeError(cap_error)


async def record_clips_video(
    video_script: list[dict],
    base_url: str = "https://jeenai.app",
    link_type: str = "regular",
    session_id: str = "clips",
    language: str = "he",
    headless: bool = False,
    output_dir: Path | None = None,
    mechanism: str = "auto",
    audio_results: list[dict | None] | None = None,
) -> dict:
    """
    Record a real browser session the Clips way and write a smooth .webm plus a
    Remotion-ready execution metadata JSON.

    mechanism:
      "mediarecorder" - in-browser MediaRecorder over getDisplayMedia (Clips).
      "cdp"           - CDP Page.startScreencast frames muxed to constant-fps
                        webm with ffmpeg (robust, works headless, survives nav).
      "auto"          - try mediarecorder, fall back to cdp if it can't start.

    audio_results: parallel list to video_script with TTS results
      {"path": str, "duration_s": float} or None per step.
      When provided, settle_ms = audio_duration_ms + 200 (tight to speech)
      so the clip matches the narration — same behaviour as recorder.py.

    Returns a superset of the old recorder.record_product_video contract so
    render_video can consume the result directly:
      {
        "run_id", "webm_path", "metadata_path", "webm_public", "fps",
        "total_seconds", "mechanism", "recorded_steps", "failed_steps", "events",
        "step_timings", "step_settles", "step_leads", "recorded_audio",
      }
    """
    run_id = f"clips-run-{int(time.time() * 1000)}"
    marker = f"JEENCLIP_{run_id}"

    out_dir = Path(output_dir) if output_dir else Path(VIDEO_PROJECT_DIR) / "public" / "recordings"
    out_dir.mkdir(parents=True, exist_ok=True)
    webm_path = out_dir / f"{run_id}.webm"
    metadata_path = out_dir / f"{run_id}.execution_log.json"

    audio_results = audio_results or []

    events: list[dict] = []
    steps_meta: list[dict] = []
    recorded_steps: list[dict] = []
    failed_steps: list[dict] = []
    seen_urls: set[str] = set()

    # Pipeline-compatible lists (same shape as recorder.record_product_video)
    step_timings: list[float] = []
    step_settles: list[float] = []
    step_leads: list[float] = []
    recorded_audio: list[dict | None] = []

    # record_start is stamped the instant capture starts, so every logged
    # timestamp lines up with the video timeline.
    record_start = {"t": None}

    def now() -> float:
        if record_start["t"] is None:
            return 0.0
        return round(time.monotonic() - record_start["t"], 3)

    def log_event(ev: dict) -> None:
        events.append(ev)

    launch_args = [
        "--use-fake-ui-for-media-stream",
        f"--auto-select-tab-capture-source-by-title={marker}",
        f"--auto-select-desktop-capture-source={marker}",
        "--autoplay-policy=no-user-gesture-required",
    ]

    async def run_steps(content) -> None:
        """Shared per-step navigation + interaction loop (capture-agnostic)."""
        for i, step in enumerate(video_script):
            url = step.get("url", "")
            action = step.get("action", f"step_{i + 1}")
            interactions = step.get("interactions") or []
            audio = audio_results[i] if i < len(audio_results) else None

            # Audio-tight linger: clip matches narration length with minimal
            # pad. No MIN_SETTLE_MS floor when audio exists — speech IS the
            # timing. Same logic as recorder.record_product_video.
            if audio and audio.get("duration_s"):
                settle_ms = round(audio["duration_s"] * 1000) + 200
            else:
                settle_ms = max(int(step.get("settle_ms", 3000)), MIN_SETTLE_MS)

            if not url.startswith("http"):
                url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"

            url_key = _url_key(url)
            if url_key in seen_urls and not _has_screen_changing_interactions(interactions):
                logger.info("Step %d skipped — duplicate screen: %s", i + 1, url)
                continue

            logger.info("Recording step %d/%d: %s — %s", i + 1, len(video_script), url, action)

            t_nav_start = now()
            try:
                response = await content.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = response.status if response else None
                log_event({
                    "type": "navigate",
                    "step_index": i,
                    "url": url,
                    "action": action,
                    "status": status,
                    "t_start": t_nav_start,
                    "t_end": now(),
                })
                if status and status >= 400:
                    logger.warning("Step %d failed — HTTP %d: %s", i + 1, status, url)
                    failed_steps.append(step)
                    continue

                try:
                    await content.wait_for_load_state("networkidle", timeout=PAGE_LOAD_WAIT_MS)
                except Exception:
                    pass

                final_url = content.url
                if final_url.rstrip("/") != url.rstrip("/"):
                    logger.warning("Step %d failed — redirected to %s", i + 1, final_url)
                    failed_steps.append(step)
                    continue

                error_reason = await _looks_like_error_page(content)
                if error_reason:
                    logger.warning("Step %d failed — %s", i + 1, error_reason)
                    failed_steps.append(step)
                    continue

                route_path = urlparse(url).path.rstrip("/") or "/"
                variant_groups = _load_clickable_groups(route_path, link_type)
                await _wait_for_page_ready(content, interactions, variant_groups)

                # Track the lead time (interaction animation duration) the same
                # way recorder.py does, so the jump-cut editor can keep the
                # cursor glide + click visible in the final video.
                lead_start = time.monotonic()
                ran_interactions = False
                if interactions:
                    ran_interactions = True
                    try:
                        await _run_interactions_clips(
                            content, interactions, variant_groups, now, log_event
                        )
                        await content.wait_for_timeout(RENDER_SETTLE_MS)
                    except Exception as exc:
                        logger.warning(
                            "Step %d interactions failed (%s) — capturing plain page", i + 1, exc
                        )
                        step = {**step, "interaction_failed": True}

                await wait_until_no_spinner(content)

                t_visible = now()
                content_visible_mono = time.monotonic()
                seen_urls.add(url_key)
                log_event({"type": "page_ready", "url": url, "t": t_visible})

                # Pipeline-compatible lists
                step_timings.append(t_visible)
                step_settles.append(settle_ms / 1000.0)
                step_leads.append(
                    content_visible_mono - lead_start if ran_interactions else 0.0
                )
                recorded_audio.append(audio)

                steps_meta.append({
                    "step_index": i,
                    "url": url,
                    "action": action,
                    "t_visible": t_visible,
                    "settle_s": round(settle_ms / 1000.0, 3),
                })
                recorded_steps.append(step)
                await content.wait_for_timeout(settle_ms)

            except Exception as exc:
                logger.error("Step %d failed unexpectedly (%s): %s", i + 1, url, exc)
                failed_steps.append(step)
                continue

    used_mechanism = mechanism
    webm_bytes_len = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, args=launch_args)

        # ── Phase 1: login off-camera ────────────────────────────────────────
        login_context = await browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        login_page = await login_context.new_page()
        await _login(login_page, language=language)
        if link_type == "admin":
            login_page = await _enter_admin_app(login_page, base_url)
        storage_state = await login_context.storage_state()
        await login_context.close()

        # ── Phase 2: recording context (authenticated) ───────────────────────
        context: BrowserContext = await browser.new_context(
            viewport=VIEWPORT,
            ignore_https_errors=True,
            storage_state=storage_state,
        )

        # Content tab C: cursor + title marker injected on every navigation.
        content = await context.new_page()
        await content.add_init_script(_CURSOR_INIT_JS)
        await content.add_init_script(_title_marker_init_js(marker))
        try:
            await content.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            logger.warning("Initial content navigation failed (%s) — continuing", exc)
        await content.wait_for_timeout(400)

        # ── Start capture (with auto-fallback) ───────────────────────────────
        recorder = None          # MediaRecorder tab, if used
        cdp = None               # CDP session, if used
        cdp_frames: list[tuple[float, bytes]] = []

        if mechanism in ("auto", "mediarecorder"):
            try:
                recorder = await _start_mediarecorder_capture(context, content, base_url, marker)
                used_mechanism = "mediarecorder"
                logger.info("Capture started: MediaRecorder (Clips-style)")
            except RuntimeError as exc:
                if mechanism == "mediarecorder":
                    await context.close()
                    await browser.close()
                    raise RuntimeError(
                        f"getDisplayMedia/MediaRecorder failed to start: {exc}. "
                        "Try headless=False or mechanism='cdp'."
                    )
                logger.warning("MediaRecorder unavailable (%s) — falling back to CDP screencast", exc)
                used_mechanism = "cdp"
        else:
            used_mechanism = "cdp"

        if used_mechanism == "cdp":
            ffmpeg = _find_ffmpeg()
            if not ffmpeg:
                await context.close()
                await browser.close()
                raise RuntimeError(
                    "CDP screencast needs ffmpeg. Install it (brew install ffmpeg) "
                    "or `pip install imageio-ffmpeg`."
                )
            loop = asyncio.get_event_loop()
            cdp = await context.new_cdp_session(content)

            def _on_frame(params: dict) -> None:
                try:
                    cdp_frames.append((
                        float(params["metadata"]["timestamp"]),
                        base64.b64decode(params["data"]),
                    ))
                except Exception:
                    return
                # Ack so Chromium keeps streaming frames.
                loop.create_task(
                    cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                )

            cdp.on("Page.screencastFrame", _on_frame)
            await cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 80,
                "everyNthFrame": 1,
                "maxWidth": MAX_WIDTH,
                "maxHeight": MAX_HEIGHT,
            })
            logger.info("Capture started: CDP Page.startScreencast")

        record_start["t"] = time.monotonic()

        # ── Run the shared step loop ─────────────────────────────────────────
        await run_steps(content)
        await content.wait_for_timeout(500)
        total_seconds = now()

        # ── Stop capture and produce the webm ────────────────────────────────
        if used_mechanism == "mediarecorder":
            b64 = await recorder.evaluate("() => window.__clipStop()")
            webm_bytes = base64.b64decode(b64)
            webm_path.write_bytes(webm_bytes)
            webm_bytes_len = len(webm_bytes)
            await context.close()
            await browser.close()
        else:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            await content.wait_for_timeout(200)
            await context.close()
            await browser.close()
            _encode_frames_to_webm(cdp_frames, webm_path, CAPTURE_FRAME_RATE, ffmpeg)
            webm_bytes_len = webm_path.stat().st_size if webm_path.exists() else 0

    metadata = {
        "run_id": run_id,
        "webm_path": str(webm_path),
        "webm_public": f"recordings/{run_id}.webm",
        "mechanism": used_mechanism,
        "fps": CAPTURE_FRAME_RATE,
        "viewport": VIEWPORT,
        "bitrate_bps": VIDEO_BITRATE_BPS,
        "total_seconds": total_seconds,
        "base_url": base_url,
        "link_type": link_type,
        "language": language,
        "events": events,
        "steps": steps_meta,
        "failed_steps": [
            {"url": s.get("url"), "action": s.get("action")} for s in failed_steps
        ],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "Clips recording complete (%s): %d/%d steps, %d failed, %.1fs, %d bytes -> %s",
        used_mechanism, len(recorded_steps), len(video_script), len(failed_steps),
        total_seconds, webm_bytes_len, webm_path,
    )

    return {
        # Clips-specific fields
        "run_id": run_id,
        "metadata_path": str(metadata_path),
        "webm_public": f"recordings/{run_id}.webm",
        "mechanism": used_mechanism,
        "fps": CAPTURE_FRAME_RATE,
        "events": events,
        # Pipeline-compatible fields (same contract as recorder.record_product_video)
        "webm_path": str(webm_path),
        "step_timings": step_timings,
        "step_settles": step_settles,
        "step_leads": step_leads,
        "total_seconds": total_seconds,
        "recorded_steps": recorded_steps,
        "recorded_audio": recorded_audio,
        "failed_steps": failed_steps,
    }
