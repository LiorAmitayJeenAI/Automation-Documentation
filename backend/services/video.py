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
TITLE_FRAMES = 90        # 3 s title card before the recording
END_FRAMES = 60          # 2 s end card after the recording
EXPLANATION_FRAMES = 180 # 6 s per explanation slide (failed steps)
RENDER_TIMEOUT_SECONDS = 600  # 10 min — real video can be long


async def render_video(
    title: str,
    recording_result: dict,
    audio_results: list[dict | None] | None = None,
    language: str = "he",
    session_id: str = "default",
    file_stem: str = "",
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

    # Build subtitle cues — startFrame is relative to the recording (not the title slide)
    cues = []
    for idx, (timing, step) in enumerate(zip(step_timings, recorded_steps)):
        audio = recorded_audio[idx] if idx < len(recorded_audio) else None
        audio_filename: str | None = None

        if audio and audio.get("path"):
            src = Path(audio["path"])
            if src.is_file():
                dest_audio = audio_public / src.name
                shutil.copy2(src, dest_audio)
                audio_filename = f"audio/{session_id}/{src.name}"

        cue: dict = {
            "startFrame": math.floor(timing * FPS),
            "text": step.get("narration", ""),
            "action": step.get("action", ""),
        }
        if step.get("caption"):
            cue["caption"] = step["caption"]
        if audio_filename:
            cue["audioFilename"] = audio_filename
        cues.append(cue)

    # Build explanation cues for steps Playwright could not navigate to
    explanation_cues = [
        {"text": s.get("narration", ""), "action": s.get("action", "")}
        for s in failed_steps
        if s.get("narration")
    ]

    recorded_video_frames = math.ceil(total_seconds * FPS)
    explanation_frames = len(explanation_cues) * EXPLANATION_FRAMES
    total_frames = TITLE_FRAMES + recorded_video_frames + explanation_frames + END_FRAMES

    if explanation_cues:
        logger.info(
            "%d failed step(s) will be shown as explanation slides (%d frames)",
            len(explanation_cues), explanation_frames,
        )

    props: dict = {
        "title": title,
        "language": language,
        "recordedVideoFilename": f"recordings/{session_id}.webm",
        "recordedVideoFrames": recorded_video_frames,
        "totalFrames": total_frames,
        "cues": cues,
        "explanationCues": explanation_cues,
        "explanationFrames": explanation_frames,
    }

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
