"""
Remotion video rendering service.

Takes a real Playwright WebM recording (from recorder.py) and composes it
with timed Hebrew subtitle cues using Remotion. The final output is an MP4
that shows the actual product interaction with narration overlaid.

No screenshots, no invented content — the visual track is always the raw
browser recording.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path

from backend.config import VIDEO_DIR, VIDEO_PROJECT_DIR, JEEN_VIDEOS_DIR

logger = logging.getLogger(__name__)

FPS = 30
TITLE_FRAMES_DEFAULT = 90   # 3 s fallback when no title audio
END_FRAMES = 45             # 1.5 s end card after the recording
EXPLANATION_FRAMES = 180 # 6 s per explanation slide (failed steps)
RENDER_TIMEOUT_SECONDS = 600  # 10 min — real video can be long


async def render_video(
    title: str,
    recording_result: dict,
    audio_results: list[dict | None] | None = None,
    language: str = "he",
    session_id: str = "default",
    file_stem: str = "",
    extra_audio: dict | None = None,
) -> str | None:
    """
    Compose the WebM recording with subtitles and render to MP4 via Remotion.

    recording_result (from recorder.py) must contain:
      - webm_path: absolute path to the .webm file
      - step_timings: list of float (seconds from recording start per step)
      - total_seconds: total recording duration in seconds
      - recorded_steps: list of step dicts (with 'narration' and 'action')

    Returns absolute path to the rendered MP4, or None on failure.
    """
    webm_path = Path(recording_result.get("webm_path", ""))
    if not webm_path.is_file():
        logger.error("WebM recording not found: %s", webm_path)
        return None

    step_timings: list[float] = recording_result.get("step_timings", [])
    step_settles: list[float] = recording_result.get("step_settles", [])
    recorded_steps: list[dict] = recording_result.get("recorded_steps", [])
    recorded_audio: list[dict | None] = recording_result.get("recorded_audio", [])
    failed_steps: list[dict] = recording_result.get("failed_steps", [])
    total_seconds: float = recording_result.get("total_seconds", 0.0)

    if not recorded_steps or total_seconds <= 0:
        logger.warning("No recorded steps or zero duration — skipping video render")
        return None

    os.makedirs(JEEN_VIDEOS_DIR, exist_ok=True)

    # Copy the WebM into Remotion's public folder so staticFile() can serve it
    recordings_public = Path(VIDEO_PROJECT_DIR) / "public" / "recordings"
    recordings_public.mkdir(parents=True, exist_ok=True)
    dest_webm = recordings_public / f"{session_id}.webm"
    shutil.copy2(webm_path, dest_webm)
    logger.info("Copied WebM to Remotion public: %s", dest_webm)

    # Copy per-step audio files into Remotion's public folder
    audio_public = Path(VIDEO_PROJECT_DIR) / "public" / "audio" / session_id
    audio_public.mkdir(parents=True, exist_ok=True)

    # ── Build segments (jump-cut): only show the "visible content" portion of each step ──
    # Each segment = the settle period after content is fully loaded (skips loading/navigation)
    segments: list[dict] = []
    output_frame_cursor = 0

    for idx, timing in enumerate(step_timings):
        settle_s = step_settles[idx] if idx < len(step_settles) else 2.0
        source_start_frame = math.floor(timing * FPS)
        segment_duration_frames = math.ceil(settle_s * FPS)

        segments.append({
            "sourceStartFrame": source_start_frame,
            "durationFrames": segment_duration_frames,
            "outputStartFrame": output_frame_cursor,
        })
        output_frame_cursor += segment_duration_frames

    # Build subtitle cues — startFrame is now relative to the jump-cut output timeline
    cues = []
    for idx, (seg, step) in enumerate(zip(segments, recorded_steps)):
        audio = recorded_audio[idx] if idx < len(recorded_audio) else None
        audio_filename: str | None = None

        if audio and audio.get("path"):
            src = Path(audio["path"])
            if src.is_file():
                dest_audio = audio_public / src.name
                shutil.copy2(src, dest_audio)
                audio_filename = f"audio/{session_id}/{src.name}"

        cue: dict = {
            "startFrame": seg["outputStartFrame"],
            "text": step.get("narration", ""),
            "action": step.get("action", ""),
        }
        if step.get("caption"):
            cue["caption"] = step["caption"]
        if audio_filename:
            cue["audioFilename"] = audio_filename
        cues.append(cue)

    # ── Per-cue diagnostics ──
    logger.info("Jump-cut segment analysis (%d segments):", len(segments))
    for idx, seg in enumerate(segments):
        audio = recorded_audio[idx] if idx < len(recorded_audio) else None
        audio_s = audio["duration_s"] if audio and audio.get("duration_s") else 0.0
        seg_s = seg["durationFrames"] / FPS
        logger.info(
            "  segment %d: source=%.1fs dur=%.1fs audio=%.1fs%s",
            idx, seg["sourceStartFrame"] / FPS, seg_s, audio_s,
            " [NO AUDIO]" if not audio else "",
        )

    # Build explanation cues for steps Playwright could not navigate to
    explanation_cues = [
        {"text": s.get("narration", ""), "action": s.get("action", "")}
        for s in failed_steps
        if s.get("narration")
    ]

    # ── Copy extra audio (title / end / explanation) into Remotion public ──
    extra_audio = extra_audio or {}
    title_audio_filename: str | None = None
    end_audio_filename: str | None = None

    title_audio = extra_audio.get("title")
    if title_audio and title_audio.get("path"):
        src = Path(title_audio["path"])
        if src.is_file():
            dest = audio_public / src.name
            shutil.copy2(src, dest)
            title_audio_filename = f"audio/{session_id}/{src.name}"

    end_audio = extra_audio.get("end")
    if end_audio and end_audio.get("path"):
        src = Path(end_audio["path"])
        if src.is_file():
            dest = audio_public / src.name
            shutil.copy2(src, dest)
            end_audio_filename = f"audio/{session_id}/{src.name}"

    expl_audio_list = extra_audio.get("explanations") or []
    for idx_e, expl_audio in enumerate(expl_audio_list):
        if idx_e < len(explanation_cues) and expl_audio and expl_audio.get("path"):
            src = Path(expl_audio["path"])
            if src.is_file():
                dest = audio_public / src.name
                shutil.copy2(src, dest)
                explanation_cues[idx_e]["audioFilename"] = f"audio/{session_id}/{src.name}"

    recorded_video_frames = output_frame_cursor  # sum of all segment durations
    explanation_frames = len(explanation_cues) * EXPLANATION_FRAMES

    # Title duration adapts to audio length (+ 0.5 s pad) so narration never gets cut off
    title_audio_data = extra_audio.get("title")
    if title_audio_data and title_audio_data.get("duration_s"):
        title_frames = math.ceil((title_audio_data["duration_s"] + 0.5) * FPS)
    else:
        title_frames = TITLE_FRAMES_DEFAULT

    total_frames = title_frames + recorded_video_frames + explanation_frames + END_FRAMES

    if explanation_cues:
        logger.info(
            "%d failed step(s) will be shown as explanation slides (%d frames)",
            len(explanation_cues), explanation_frames,
        )

    total_duration_s = total_frames / FPS
    recording_s = recorded_video_frames / FPS
    title_s = title_frames / FPS
    end_s = END_FRAMES / FPS
    logger.info(
        "Projected duration: %.1f s (title=%.1f + recording=%.1f + explanations=%.1f + end=%.1f) "
        "[raw recording was %.1f s, saved %.1f s via jump-cuts]",
        total_duration_s, title_s, recording_s, explanation_frames / FPS, end_s,
        total_seconds, total_seconds - recording_s,
    )
    if total_duration_s > 60:
        breakdown = []
        for idx, seg in enumerate(segments):
            seg_s = seg["durationFrames"] / FPS
            action = recorded_steps[idx].get("action", "?") if idx < len(recorded_steps) else "?"
            breakdown.append(f"  step {idx + 1}: {seg_s:.1f}s — {action}")
        logger.warning(
            "Video exceeds 60 s target (%.1f s). Per-segment breakdown:\n%s",
            total_duration_s, "\n".join(breakdown),
        )

    props: dict = {
        "title": title,
        "language": language,
        "recordedVideoFilename": f"recordings/{session_id}.webm",
        "recordedVideoFrames": recorded_video_frames,
        "titleFrames": title_frames,
        "totalFrames": total_frames,
        "segments": segments,
        "cues": cues,
        "explanationCues": explanation_cues,
        "explanationFrames": explanation_frames,
    }
    if title_audio_filename:
        props["titleAudioFilename"] = title_audio_filename
    if end_audio_filename:
        props["endAudioFilename"] = end_audio_filename

    props_fd, props_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(props_fd, "w", encoding="utf-8") as f:
            json.dump(props, f, ensure_ascii=False)

        output_stem = file_stem or session_id
        output_path = os.path.join(JEEN_VIDEOS_DIR, f"{output_stem}.mp4")
        cmd = [
            "npx", "--yes", "remotion", "render",
            "TutorialVideo",
            output_path,
            f"--props={props_path}",
        ]
        logger.info(
            "Rendering video: %d cues, %d frames (%.1f s) → %s",
            len(cues), total_frames, total_frames / FPS, output_path,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=VIDEO_PROJECT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=RENDER_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("Remotion render timed out after %d s", RENDER_TIMEOUT_SECONDS)
            return None

        if proc.returncode != 0:
            logger.error(
                "Remotion render failed (exit %d)\nstdout: %s\nstderr: %s",
                proc.returncode,
                stdout.decode(errors="replace")[-2000:],
                stderr.decode(errors="replace")[-2000:],
            )
            return None

        if not Path(output_path).is_file():
            logger.error("Render completed but output not found: %s", output_path)
            return None

        logger.info("Video rendered: %s", output_path)
        return output_path

    except Exception as exc:
        logger.error("Unexpected error during render: %s", exc, exc_info=True)
        return None
    finally:
        try:
            os.unlink(props_path)
        except OSError:
            pass
