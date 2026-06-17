"""
Hebrew TTS synthesis for video narration.

Primary: ElevenLabs multilingual-v2 (set ELEVENLABS_API_KEY).
Fallback: Azure Cognitive Services he-IL-AvriNeural (set AZURE_SPEECH_KEY + AZURE_SPEECH_REGION).
If neither key is present, returns None for every step — the pipeline continues
without audio, using fixed settle_ms values instead.

Audio files are written to VIDEO_DIR/audio/{session_id}/step_{i}.mp3.
Duration is measured with ffprobe so Remotion knows exactly how long each clip is.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
from mutagen.mp3 import MP3

from backend.config import (
    VIDEO_DIR,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    AZURE_SPEECH_KEY,
    AZURE_SPEECH_REGION,
)

logger = logging.getLogger(__name__)

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
AZURE_TTS_URL = "https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"


def _mp3_duration(path: Path) -> float:
    """Return audio duration in seconds by reading the MP3 header with mutagen."""
    audio = MP3(str(path))
    return audio.info.length


async def _synthesize_elevenlabs(text: str, out_path: Path) -> None:
    """Call ElevenLabs multilingual-v2 and write MP3 to out_path."""
    url = ELEVENLABS_TTS_URL.format(voice_id=ELEVENLABS_VOICE_ID)
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.80},
    }
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)


async def _synthesize_azure(text: str, out_path: Path) -> None:
    """Call Azure Speech he-IL-AvriNeural and write MP3 to out_path."""
    url = AZURE_TTS_URL.format(region=AZURE_SPEECH_REGION)
    # SSML with explicit Hebrew locale and neural voice
    ssml = (
        "<speak version='1.0' xml:lang='he-IL'>"
        f"<voice name='he-IL-AvriNeural'>{_escape_xml(text)}</voice>"
        "</speak>"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-48khz-96kbitrate-mono-mp3",
        "User-Agent": "JeenVideoBot",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, content=ssml.encode("utf-8"), headers=headers)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


async def synthesize_step(
    text: str,
    step_index: int,
    session_id: str,
) -> dict | None:
    """
    Synthesize one narration string to MP3.

    Returns {"path": str (absolute), "duration_s": float}
    or None if TTS is unavailable or the synthesis fails.
    """
    if not text.strip():
        return None

    audio_dir = Path(VIDEO_DIR) / "audio" / session_id
    audio_dir.mkdir(parents=True, exist_ok=True)
    out_path = audio_dir / f"step_{step_index}.mp3"

    # Try ElevenLabs first, then Azure Speech
    if ELEVENLABS_API_KEY:
        try:
            await _synthesize_elevenlabs(text, out_path)
            duration_s = _mp3_duration(out_path)
            logger.info(
                "TTS step %d (ElevenLabs): %.1f s → %s", step_index, duration_s, out_path
            )
            return {"path": str(out_path), "duration_s": duration_s}
        except Exception as exc:
            logger.warning("ElevenLabs TTS failed for step %d: %s", step_index, exc)

    if AZURE_SPEECH_KEY:
        try:
            await _synthesize_azure(text, out_path)
            duration_s = _mp3_duration(out_path)
            logger.info(
                "TTS step %d (Azure): %.1f s → %s", step_index, duration_s, out_path
            )
            return {"path": str(out_path), "duration_s": duration_s}
        except Exception as exc:
            logger.warning("Azure TTS failed for step %d: %s", step_index, exc)

    logger.info("TTS unavailable for step %d — no audio for this step", step_index)
    return None


async def synthesize_script(
    video_script: list[dict],
    session_id: str,
) -> list[dict | None]:
    """
    Synthesize all step narrations concurrently.
    Returns a list parallel to video_script: each entry is
    {"path": str, "duration_s": float} or None.
    """
    if not ELEVENLABS_API_KEY and not AZURE_SPEECH_KEY:
        logger.info("No TTS API keys configured — skipping audio synthesis")
        return [None] * len(video_script)

    tasks = [
        synthesize_step(
            step.get("narration", ""),
            i,
            session_id,
        )
        for i, step in enumerate(video_script)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sanitized: list[dict | None] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("TTS step %d raised: %s", i, r)
            sanitized.append(None)
        else:
            sanitized.append(r)  # type: ignore[arg-type]
    return sanitized
