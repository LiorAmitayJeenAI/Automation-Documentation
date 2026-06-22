"""
Standalone video generation pipeline.

Completely independent from the presentation pipeline (pipeline.py).
Produces an MP4 tutorial video from a Confluence page by:
  1. Fetching Confluence content
  2. Generating a video script (5-12 steps, narration in the chosen language)
  3. Recording a real Playwright browser session following the script
  4. Rendering the recording with subtitle overlays via Remotion
  5. Uploading the MP4 to SharePoint
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncGenerator

from backend.config import REGULAR_URL, ADMIN_URL, SP_VIDEO_FOLDER_PATH
from backend.services import confluence, llm, sharepoint
from backend.services import recorder, video, tts

logger = logging.getLogger(__name__)


class VideoEvent:
    def __init__(self, stage: str, status: str, detail: str = "", data: dict | None = None):
        self.stage = stage
        self.status = status
        self.detail = detail
        self.data = data or {}

    def to_dict(self) -> dict:
        return {"stage": self.stage, "status": self.status, "detail": self.detail, **self.data}


def _make_file_stem(folder_name: str, part_name: str, session_id: str, language: str = "") -> str:
    """Build a clean file stem like ``he-Admin-&-control-Part-5-Organization-System-Prompt``."""
    def sanitize(s: str) -> str:
        s = re.sub(r"[^\w&\-]", "-", s.strip(), flags=re.UNICODE)
        return re.sub(r"-{2,}", "-", s).strip("-")

    parts = [sanitize(p) for p in [folder_name, part_name] if p and p.strip()]
    stem = "-".join(parts) if parts else session_id
    if language:
        stem = f"{language.lower()}-{stem}"
    return stem


async def run_video_pipeline(
    confluence_url: str,
    language: str = "he",
    link_type: str = "regular",
    session_id: str = "default_video_session",
    folder_name: str = "",
    part_name: str = "",
) -> AsyncGenerator[VideoEvent, None]:
    """
    Yield VideoEvent objects for each stage of the video generation process.
    The final event carries video_url (SharePoint URL for the MP4).
    """
    base_url = ADMIN_URL if link_type == "admin" else REGULAR_URL
    file_stem = _make_file_stem(folder_name, part_name, session_id, language)

    # ── 1. Fetch Confluence content ──
    yield VideoEvent("confluence", "running", "Fetching Confluence page...")
    try:
        title, markdown_content = await confluence.fetch_page_as_markdown(confluence_url)
        yield VideoEvent("confluence", "done", f"Fetched: {title}", {"title": title})
    except Exception as exc:
        yield VideoEvent("confluence", "error", str(exc))
        return

    lang_label = "Hebrew" if language == "he" else "English"

    # ── 2. Generate video script (5-7 steps, target <60 s) ──
    yield VideoEvent("script", "running", "Generating video script with AI...")
    try:
        video_script = await llm.generate_video_script(
            markdown_content,
            language=language,
            base_url=base_url,
            link_type=link_type,
        )
        yield VideoEvent(
            "script", "done",
            f"Script ready: {len(video_script)} steps",
            {"step_count": len(video_script)},
        )
    except Exception as exc:
        yield VideoEvent("script", "error", str(exc))
        return

    if not video_script:
        yield VideoEvent("script", "error", "No steps generated — cannot produce video")
        return

    # ── 3. Synthesize TTS audio for each step ──
    yield VideoEvent("tts", "running", f"Synthesizing {lang_label} voiceover...")
    try:
        audio_results = await tts.synthesize_script(
            video_script, session_id, language=language,
        )
        n_audio = sum(1 for a in audio_results if a)
        yield VideoEvent(
            "tts", "done",
            f"Audio ready: {n_audio}/{len(video_script)} steps have voiceover",
            {"audio_steps": n_audio},
        )
    except Exception as exc:
        logger.warning("TTS synthesis failed (%s) — continuing without audio", exc)
        audio_results = [None] * len(video_script)
        yield VideoEvent("tts", "done", "Voiceover skipped (TTS unavailable)")

    # ── 4. Record the real browser session ──
    yield VideoEvent("record", "running", f"Recording browser session ({len(video_script)} steps)...")
    try:
        recording_result = await recorder.record_product_video(
            video_script=video_script,
            base_url=base_url,
            link_type=link_type,
            session_id=session_id,
            audio_results=audio_results,
            language=language,
        )
        n_recorded = len(recording_result.get("recorded_steps", []))
        duration = recording_result.get("total_seconds", 0)
        yield VideoEvent(
            "record", "done",
            f"Recorded {n_recorded} steps ({duration:.1f} s)",
            {"recorded_steps": n_recorded, "duration_seconds": duration},
        )
    except Exception as exc:
        yield VideoEvent("record", "error", str(exc))
        return

    if not recording_result.get("recorded_steps"):
        yield VideoEvent("record", "error", "No steps were captured successfully")
        return

    # ── 4a2. Fix narration for steps whose interactions failed ──
    # When Playwright could not open a planned tab/panel, the viewer sees only the
    # plain page. Rewrite those steps' narration to match, then re-synthesize their
    # audio and tighten their segment timing so voiceover, subtitle and screen agree.
    try:
        recorded_steps = recording_result.get("recorded_steps", [])
        if any(s.get("interaction_failed") for s in recorded_steps):
            yield VideoEvent(
                "narration_fix", "running",
                "Aligning narration with captured screens...",
            )
            fixed_steps = await llm.regenerate_narrations(recorded_steps, language=language)
            recording_result["recorded_steps"] = fixed_steps

            recorded_audio = recording_result.get("recorded_audio", [])
            step_settles = recording_result.get("step_settles", [])
            n_fixed = 0
            for idx, step in enumerate(fixed_steps):
                if not step.get("interaction_failed"):
                    continue
                new_audio = await tts.synthesize_step(
                    step.get("narration", ""),
                    step_index=800 + idx,
                    session_id=session_id,
                    language=language,
                )
                if new_audio and idx < len(recorded_audio):
                    recorded_audio[idx] = new_audio
                    # Tighten the segment to the new clip, but never exceed the
                    # originally recorded linger window (avoids bleeding into the
                    # next step's navigation in the jump-cut output).
                    if idx < len(step_settles):
                        old_settle = step_settles[idx]
                        step_settles[idx] = min(new_audio["duration_s"] + 0.2, old_settle)
                    n_fixed += 1

            yield VideoEvent(
                "narration_fix", "done",
                f"Re-aligned {n_fixed} narration(s) to the captured screens",
                {"realigned_steps": n_fixed},
            )
    except Exception as exc:
        logger.warning("Narration realignment failed (%s) — keeping originals", exc)

    # ── 4b. Synthesize TTS for explanation slides (title/end cards are silent) ──
    extra_audio: dict = {"title": None, "end": None, "explanations": []}
    try:
        failed_steps = recording_result.get("failed_steps", [])
        if failed_steps:
            expl_tasks = [
                tts.synthesize_step(
                    s.get("narration", ""), step_index=950 + i,
                    session_id=session_id, language=language,
                )
                for i, s in enumerate(failed_steps)
                if s.get("narration")
            ]
            expl_results = await asyncio.gather(*expl_tasks)
            extra_audio["explanations"] = list(expl_results)

        n_extra = sum(1 for e in extra_audio["explanations"] if e)
        logger.info("Extra TTS (explanations): %d clips voiced", n_extra)
    except Exception as exc:
        logger.warning("Extra TTS synthesis failed (%s) — continuing without", exc)

    # ── 5. Render with Remotion (subtitles + audio + branding) ──
    yield VideoEvent("render", "running", "Rendering video with Remotion...")
    try:
        mp4_path = await video.render_video(
            title=title,
            recording_result=recording_result,
            audio_results=audio_results,
            language=language,
            session_id=session_id,
            file_stem=file_stem,
            extra_audio=extra_audio,
        )
        if not mp4_path:
            yield VideoEvent("render", "error", "Remotion render returned no output")
            return
        yield VideoEvent("render", "done", f"Video rendered: {mp4_path}")
    except Exception as exc:
        yield VideoEvent("render", "error", str(exc))
        return

    # ── 6. Upload MP4 to SharePoint ──
    video_url = None
    yield VideoEvent("upload", "running", "Uploading video to SharePoint...")
    try:
        page_id = confluence.extract_page_id(confluence_url)
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:60]
        upload_list = await sharepoint.upload_local_files(
            [mp4_path], SP_VIDEO_FOLDER_PATH, session_id=session_id,
        )
        if upload_list:
            video_url = upload_list[0].get("webUrl")
        yield VideoEvent(
            "upload", "done",
            "Video uploaded to SharePoint",
            {"video_url": video_url},
        )
    except Exception as exc:
        logger.error("Video upload failed: %s", exc)
        yield VideoEvent("upload", "error", str(exc))

    # ── Final ──
    yield VideoEvent(
        "complete", "done",
        "Video pipeline completed",
        {"video_url": video_url, "title": title},
    )
