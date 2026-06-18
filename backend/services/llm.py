"""
Azure OpenAI integration for the document formatting step.

Calls Azure OpenAI with the Confluence markdown content and the
document_formatter prompt, returning structured JSON with
presentation_content and screenshot_script.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from openai import AsyncAzureOpenAI

from backend.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_API_VERSION,
)
from backend.prompts.document_formatter import DOCUMENT_FORMATTER_PROMPT
from backend.prompts.narration import NARRATION_PROMPT
from backend.prompts.video_script import VIDEO_SCRIPT_PROMPT, get_language_instructions

logger = logging.getLogger(__name__)

_ROUTES_MAP_PATH = Path(__file__).resolve().parent.parent / "routes_map.json"

_client = AsyncAzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)


def _normalize_link_type(value: str | None) -> str:
    return "admin" if value == "admin" else "regular"


def _route_link_type(route: dict) -> str:
    return _normalize_link_type(route.get("link_type"))


def _load_allowed_routes(link_type: str = "regular") -> list[dict]:
    """Load the curated list of valid product routes used to constrain screenshots."""
    normalized_link_type = _normalize_link_type(link_type)
    try:
        with open(_ROUTES_MAP_PATH, encoding="utf-8") as f:
            routes = json.load(f)
        if isinstance(routes, list):
            return [
                r
                for r in routes
                if (
                    isinstance(r, dict)
                    and r.get("path")
                    and _route_link_type(r) == normalized_link_type
                    and not r["path"].startswith("/support/")
                    and r["path"] != "/chat"
                )
            ]
    except FileNotFoundError:
        logger.warning("routes_map.json not found at %s — screenshots unconstrained", _ROUTES_MAP_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load routes_map.json: %s", exc)
    return []


def _element_variants(el) -> list[str]:
    """Return the label variants (e.g. Hebrew + English) for one clickable element.

    Supports the grouped form ({"variants": [...]}), the {text, aria} form, and
    the legacy bare-string form.
    """
    if isinstance(el, dict) and "variants" in el:
        candidates = el.get("variants") or []
    elif isinstance(el, dict):
        candidates = [el.get("text", ""), el.get("aria", "")]
    else:
        candidates = [el]

    variants: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        label = str(c).strip()
        if label and label.lower() not in seen:
            seen.add(label.lower())
            variants.append(label)
    return variants


def _format_clickable_elements(route: dict) -> str:
    """Render a route's verified clickable buttons for the prompt.

    Each on-page button is shown with all of its label variants (Hebrew /
    English) joined by " / ", and buttons are separated by commas. Used to
    constrain the LLM's interaction click steps to real, on-page buttons.
    Returns an empty string when the route has no recorded clickable elements.
    """
    buttons: list[str] = []
    for el in route.get("clickable_elements") or []:
        variants = _element_variants(el)
        if variants:
            buttons.append(" / ".join(f'"{v}"' for v in variants))
    return ", ".join(buttons)


def _format_allowed_routes(routes: list[dict], base_url: str) -> str:
    if not routes:
        return "(No route list configured — return an empty screenshot_script.)"
    base = base_url.rstrip("/")
    lines = []
    for r in routes:
        path = str(r.get("path", "")).strip()
        if not path.startswith("/"):
            path = "/" + path
        desc = str(r.get("description", "")).strip()
        lines.append(f"- {base}{path} — {desc}" if desc else f"- {base}{path}")
        clickable = _format_clickable_elements(r)
        if clickable:
            lines.append(f"    clickable buttons: {clickable}")
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wraps its JSON output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("```")
        inner = lines[1] if len(lines) > 1 else text
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return text


async def format_document(
    markdown_content: str,
    language: str = "he",
    base_url: str = "https://jeenai.app",
    link_type: str = "regular",
) -> dict:
    """
    Send Confluence markdown to Azure OpenAI and get back structured JSON:
    {
        "presentation_content": "...",
        "screenshot_script": [{"url": "...", "action": "..."}, ...]
    }
    """
    language_name = "Hebrew" if language == "he" else "English"
    allowed_routes = _format_allowed_routes(_load_allowed_routes(link_type), base_url)
    system_prompt = DOCUMENT_FORMATTER_PROMPT.format(
        language_name=language_name,
        base_url=base_url,
        allowed_routes=allowed_routes,
    )

    response = await _client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": markdown_content},
        ],
        temperature=1,
        max_completion_tokens=16000,
    )

    raw_text = response.choices[0].message.content or ""
    logger.info("LLM response length: %d chars", len(raw_text))

    cleaned = _strip_code_fences(raw_text)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM JSON output: %s\nRaw: %s", exc, raw_text[:500])
        json_match = re.search(r'\{[\s\S]*\}', cleaned)
        if json_match:
            parsed = json.loads(json_match.group(0))
        else:
            raise ValueError(f"LLM returned invalid JSON: {raw_text[:200]}") from exc

    if "presentation_content" not in parsed:
        raise ValueError("LLM response missing 'presentation_content' key")
    if "screenshot_script" not in parsed:
        parsed["screenshot_script"] = []

    return parsed


async def generate_video_script(
    markdown_content: str,
    language: str = "he",
    base_url: str = "https://jeenai.app",
    link_type: str = "regular",
) -> list[dict]:
    """
    Generate a comprehensive video recording script from Confluence markdown.

    Returns a list of step dicts, each with:
      url, action, narration, interactions (optional), settle_ms
    """
    allowed_routes = _format_allowed_routes(_load_allowed_routes(link_type), base_url)
    lang_instructions = get_language_instructions(language)
    system_prompt = VIDEO_SCRIPT_PROMPT.format(
        language_instructions=lang_instructions,
        allowed_routes=allowed_routes,
    )

    response = await _client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": markdown_content},
        ],
        max_completion_tokens=8000,
    )

    raw = response.choices[0].message.content or ""
    logger.info("Video script LLM response: %d chars", len(raw))
    cleaned = _strip_code_fences(raw)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        match = re.search(r'\{[\s\S]*\}', cleaned)
        if match:
            parsed = json.loads(match.group(0))
        else:
            raise ValueError(f"Video script LLM returned invalid JSON: {raw[:200]}") from exc

    steps = parsed.get("video_script", [])
    if not isinstance(steps, list):
        raise ValueError(f"Expected list under 'video_script', got {type(steps)}")

    logger.info(
        "═══ VIDEO SCRIPT (%d steps) ═══\n%s\n═══ END VIDEO SCRIPT ═══",
        len(steps),
        json.dumps(steps, ensure_ascii=False, indent=2),
    )

    return steps


async def generate_narration(
    screenshot_results: list[dict],
    presentation_content: str = "",
    language: str = "he",
) -> list[dict]:
    """
    Generate Hebrew (or English) narration for each real screenshot taken by Playwright.

    Receives the actual screenshot_results list (from take_screenshots) — each dict has
    at minimum 'path', 'action', and 'slide_section'. Returns the same list with a
    'narration' key added to every item. Narration is grounded strictly in the 'action'
    and 'slide_section' fields; the LLM is instructed not to invent any UI detail that
    isn't explicitly mentioned there.
    """
    if not screenshot_results:
        return screenshot_results

    input_data = [
        {"action": r.get("action", ""), "slide_section": r.get("slide_section", "")}
        for r in screenshot_results
    ]

    user_message = json.dumps(input_data, ensure_ascii=False)
    if presentation_content:
        # Provide limited context so the narration is coherent, but the LLM
        # is told not to extrapolate beyond the action/slide_section fields.
        user_message = (
            f"Document context (for tone only — do not add features not in action):\n"
            f"{presentation_content[:2000]}\n\n"
            f"Screenshots:\n{user_message}"
        )

    response = await _client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": NARRATION_PROMPT},
            {"role": "user", "content": user_message},
        ],
        max_completion_tokens=4000,
    )

    raw = response.choices[0].message.content or ""
    logger.info("Narration LLM response length: %d chars", len(raw))
    cleaned = _strip_code_fences(raw)

    try:
        narrations = json.loads(cleaned)
        if not isinstance(narrations, list):
            raise ValueError("Expected JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to parse narration JSON: %s | raw: %s", exc, raw[:300])
        # Fallback: use the action text as the narration
        narrations = [r.get("action", "") for r in screenshot_results]

    # Guard against mismatched lengths
    while len(narrations) < len(screenshot_results):
        narrations.append(screenshot_results[len(narrations)].get("action", ""))

    return [{**r, "narration": str(narrations[i])} for i, r in enumerate(screenshot_results)]


def extract_title(markdown_content: str) -> str:
    """Extract the first H1 heading from markdown, or first non-empty line."""
    for line in markdown_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    for line in markdown_content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:100]
    return "Presentation"
