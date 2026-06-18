"""
TTS synthesis for video narration (Hebrew and English).

Primary: ElevenLabs multilingual v3 (set ELEVENLABS_API_KEY).
Fallback: Azure Cognitive Services (set AZURE_SPEECH_KEY + AZURE_SPEECH_REGION).
If neither key is present, returns None for every step — the pipeline continues
without audio, using fixed settle_ms values instead.

Rate-limit resilience:
  - Concurrency capped via asyncio.Semaphore so at most _MAX_CONCURRENT requests
    hit ElevenLabs at once.
  - HTTP 429 triggers exponential backoff with jitter (up to _MAX_RETRIES attempts).
  - Azure is tried only after ElevenLabs exhausts all retries.

Audio files are written to VIDEO_DIR/audio/{session_id}/step_{i}.mp3.
Duration is measured with mutagen so Remotion knows exactly how long each clip is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re as _re
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

_AZURE_VOICES = {
    "he": {"locale": "he-IL", "voice": "he-IL-AvriNeural"},
    "en": {"locale": "en-US", "voice": "en-US-GuyNeural"},
}

_MAX_CONCURRENT = 2
_MAX_RETRIES = 5
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 16.0

_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)


def _mp3_duration(path: Path) -> float:
    """Return audio duration in seconds by reading the MP3 header with mutagen."""
    audio = MP3(str(path))
    return audio.info.length


async def _synthesize_elevenlabs(text: str, out_path: Path, language: str = "he") -> None:
    """Call ElevenLabs multilingual v3 and write MP3 to out_path.

    Retries on HTTP 429 with exponential backoff + jitter.
    """
    url = ELEVENLABS_TTS_URL.format(voice_id=ELEVENLABS_VOICE_ID)
    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "language_code": language,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.80},
    }
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                delay = float(retry_after)
            else:
                delay = min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_MAX_S)
            delay += random.uniform(0.1, 0.5)
            logger.warning(
                "ElevenLabs 429 (attempt %d/%d) — retrying in %.1fs",
                attempt + 1, _MAX_RETRIES, delay,
            )
            last_exc = httpx.HTTPStatusError(
                f"429 Too Many Requests", request=resp.request, response=resp,
            )
            await asyncio.sleep(delay)
            continue

        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        return

    raise last_exc or RuntimeError("ElevenLabs retries exhausted")


async def _synthesize_azure(text: str, out_path: Path, language: str = "he") -> None:
    """Call Azure Speech with the appropriate locale/voice and write MP3 to out_path."""
    url = AZURE_TTS_URL.format(region=AZURE_SPEECH_REGION)
    voice_cfg = _AZURE_VOICES.get(language, _AZURE_VOICES["he"])
    ssml = (
        f"<speak version='1.0' xml:lang='{voice_cfg['locale']}'>"
        f"<voice name='{voice_cfg['voice']}'>{_escape_xml(text)}</voice>"
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


def _sanitize_for_tts(text: str) -> str:
    """Strip characters that may trip TTS APIs; keep Hebrew, basic Latin, punctuation."""
    cleaned = _re.sub(r'[^\w\s\u0590-\u05FF.,!?;:\-]', '', text)
    cleaned = ' '.join(cleaned.split())
    return cleaned[:500]


async def _try_synthesize(
    text: str, out_path: Path, step_index: int, language: str = "he",
) -> dict | None:
    """Attempt synthesis: ElevenLabs first (with retries), then Azure fallback."""
    if ELEVENLABS_API_KEY:
        try:
            async with _semaphore:
                await _synthesize_elevenlabs(text, out_path, language=language)
            duration_s = _mp3_duration(out_path)
            logger.info(
                "TTS step %d (ElevenLabs, %s): %.1f s → %s",
                step_index, language, duration_s, out_path,
            )
            return {"path": str(out_path), "duration_s": duration_s}
        except Exception as exc:
            logger.warning("ElevenLabs TTS failed for step %d: %s", step_index, exc)

    if AZURE_SPEECH_KEY:
        try:
            await _synthesize_azure(text, out_path, language=language)
            duration_s = _mp3_duration(out_path)
            logger.info(
                "TTS step %d (Azure fallback, %s): %.1f s → %s",
                step_index, language, duration_s, out_path,
            )
            return {"path": str(out_path), "duration_s": duration_s}
        except Exception as exc:
            logger.warning("Azure TTS failed for step %d: %s", step_index, exc)

    return None


async def synthesize_step(
    text: str,
    step_index: int,
    session_id: str,
    language: str = "he",
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

    result = await _try_synthesize(text, out_path, step_index, language=language)
    if result:
        return result

    sanitized = _sanitize_for_tts(text)
    if sanitized and sanitized != text:
        logger.warning(
            "TTS step %d: retrying with sanitized text (%d→%d chars)",
            step_index, len(text), len(sanitized),
        )
        result = await _try_synthesize(sanitized, out_path, step_index, language=language)
        if result:
            return result

    logger.warning("TTS step %d FAILED — no audio for this step after retry", step_index)
    return None


async def synthesize_script(
    video_script: list[dict],
    session_id: str,
    language: str = "he",
) -> list[dict | None]:
    """
    Synthesize all step narrations with concurrency cap.
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
            language=language,
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

    voiced = sum(1 for r in sanitized if r is not None)
    total = len(sanitized)
    if voiced < total:
        logger.warning(
            "TTS: only %d/%d steps voiced — %d steps will be SILENT",
            voiced, total, total - voiced,
        )
    else:
        logger.info("TTS: all %d/%d steps voiced", voiced, total)

    return sanitized
