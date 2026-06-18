"""
Gamma API integration — generate presentations and poll for results.

Ported from the Langflow custom components GammaPresentationGenerator
and GammaPresentationResult.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from backend.config import GAMMA_API_KEY

logger = logging.getLogger(__name__)

GAMMA_GENERATIONS_URL = "https://public-api.gamma.app/v1.0/generations"
THEME_ID = "ap9a25m9pv1qg4m"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _build_language_instructions(language: str) -> str:
    if language == "he":
        return (
            "The presentation must use proper Hebrew RTL formatting across ALL slides.\n\n"
            "Requirements:\n"
            "- All Hebrew text must be right-aligned.\n"
            "- Titles, subtitles, and bullet points must be right-aligned.\n"
            "- Maintain consistent RTL formatting throughout the presentation.\n"
            "- Avoid left-aligned Hebrew content."
        )
    return (
        "The presentation must use proper English LTR formatting across ALL slides.\n\n"
        "Requirements:\n"
        "- All English text must be left-aligned.\n"
        "- Maintain consistent left-to-right formatting throughout the presentation.\n"
        "- Use natural English presentation formatting conventions."
    )


def _build_image_caption(img: dict) -> str:
    """
    Build a descriptive caption for Gamma from screenshot metadata.
    Prefers slide_section + action (threaded from the pipeline) over
    the lossy filename-derived fallback.
    """
    slide_section = img.get("slide_section", "").strip()
    action = img.get("action", "").strip()

    if slide_section and action:
        return f'Slide: "{slide_section}" | Content: {action}'
    if slide_section:
        return f'Slide: "{slide_section}"'
    if action:
        return action

    name = img.get("name", "")
    base = name.rsplit(".", 1)[0]
    base = re.sub(r"_\d+$", "", base)
    return base.replace("_", " ").strip()


async def generate_presentation(
    title: str,
    prompt: str,
    language: str = "he",
    additional_instructions: str = "",
    images: list[dict] | None = None,
    use_provided_images: bool = False,
) -> str:
    """
    Start a Gamma presentation generation.
    Returns the generation_id to poll.

    `images` is a list of {"name", "url", "downloadUrl"} dicts. The publicly
    fetchable image URL is embedded inline in the input text (Gamma scans for
    whitespace-separated image URLs).

    When `use_provided_images` is True the URLs are known to be publicly
    accessible with a recognized image extension, so imageOptions.source is
    set to "noImages" (= use only the provided screenshots). When False,
    imageOptions stays "aiGenerated" and URLs are still appended as a
    best-effort hint; Gamma will silently skip any it cannot fetch but will
    fill slides with its own AI-generated images instead of leaving them blank.
    """
    images = images or []

    full_prompt = prompt

    embedded = []
    for img in images:
        img_url = img.get("downloadUrl") or img.get("url")
        if not img_url:
            continue
        caption = _build_image_caption(img)
        embedded.append((caption, img_url))

    if embedded:
        lines = ["", "", "Screenshots to embed (place each on the specified slide):", ""]
        for caption, img_url in embedded:
            if caption:
                lines.append(f"{caption}:")
            lines.append(img_url)
            lines.append("")
        full_prompt += "\n".join(lines)

    logger.info(
        "Gamma image config — embedded: %d, use_provided_images: %s, source: aiGenerated",
        len(embedded), use_provided_images,
    )
    if embedded:
        urls = [url for _, url in embedded]
        logger.info("Gamma image URLs: %s", urls)

    lang_formatting = _build_language_instructions(language)
    image_placement = (
        "\n\nThe input text contains screenshot image URLs with placement instructions. "
        "Each screenshot specifies which slide it belongs to (via 'Slide: ...'). "
        "Place each screenshot ONLY on the slide indicated in its caption. "
        "For all other slides that do not have a provided screenshot, use AI-generated images."
        if (embedded and use_provided_images) else (
            "\n\nThe input text may contain screenshot image URLs. If any are "
            "accessible, prefer embedding them over generated images."
            if embedded else ""
        )
    )
    final_instructions = f"{additional_instructions}{image_placement}\n\n{lang_formatting}"

    image_options: dict[str, Any] = {
        "source": "aiGenerated",
        "model": "gpt-image-2",
        "style": (
            "Natural office photo of people interacting casually, soft daylight, genuine expressions, clean modern workspace, blurred background, no screens or phones or robots."
        ),
    }

    payload: dict[str, Any] = {
        "textMode": "generate",
        "format": "presentation",
        "cardSplit": "auto",
        "exportAs": "pdf",
        "inputText": full_prompt,
        "title": title,
        "themeId": THEME_ID,
        "additionalInstructions": final_instructions,
        "textOptions": {
            "amount": "medium",
            "language": language,
            "tone": "professional",
        },
        "imageOptions": image_options,
        "cardOptions": {
            "headerFooter": {
                "bottomRight": {
                    "type": "image",
                    "source": "themeLogo",
                    "size": "md",
                },
            },
        },
    }

    logger.info("Gamma payload — imageOptions: %s", image_options)
    if embedded:
        image_section = full_prompt[full_prompt.rfind("Screenshots to embed"):]
        logger.debug("Gamma image section in prompt: %.500s", image_section)

    headers = {
        "X-API-KEY": GAMMA_API_KEY,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(GAMMA_GENERATIONS_URL, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.error("Gamma API %s: %s", resp.status_code, resp.text)
            raise RuntimeError(f"Gamma API {resp.status_code}: {resp.text}")

    data = resp.json()
    generation_id = data.get("generationId")
    if not generation_id:
        raise RuntimeError(f"Gamma API did not return a generationId: {data}")

    logger.info("Gamma generation started: %s", generation_id)
    return generation_id


async def poll_generation(generation_id: str, max_wait: int = 3600) -> dict:
    """
    Poll a Gamma generation until status is 'completed' or 'failed'.
    Returns {"gamma_url", "pdf_url", "gamma_id", "status"}.
    """
    url = f"{GAMMA_GENERATIONS_URL}/{generation_id}"
    headers = {
        "X-API-KEY": GAMMA_API_KEY,
        "Accept": "*/*",
        "Content-Type": "application/json",
    }

    start = asyncio.get_event_loop().time()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while True:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.error("Gamma poll API %s: %s", resp.status_code, resp.text)
                raise RuntimeError(f"Gamma poll API {resp.status_code}: {resp.text}")
            data = resp.json()
            status = data.get("status")

            elapsed = int(asyncio.get_event_loop().time() - start)
            logger.info("Gamma poll — status: %s (elapsed: %ds)", status, elapsed)

            if status in ("completed", "failed"):
                break

            if asyncio.get_event_loop().time() - start > max_wait:
                logger.warning("Gamma max wait exceeded (%ds)", max_wait)
                break

            await asyncio.sleep(10)

    return {
        "generation_id": data.get("generationId"),
        "status": data.get("status"),
        "gamma_id": data.get("gammaId"),
        "gamma_url": data.get("gammaUrl"),
        "pdf_url": data.get("exportUrl"),
    }


async def generate_and_wait(
    title: str,
    prompt: str,
    language: str = "he",
    additional_instructions: str = "",
    images: list[dict] | None = None,
    use_provided_images: bool = False,
) -> dict:
    """
    Full lifecycle: generate a presentation and poll until done.
    Returns {"gamma_url", "pdf_url", "gamma_id", "status", "generation_id"}.
    """
    generation_id = await generate_presentation(
        title=title,
        prompt=prompt,
        language=language,
        additional_instructions=additional_instructions,
        images=images,
        use_provided_images=use_provided_images,
    )
    return await poll_generation(generation_id)
