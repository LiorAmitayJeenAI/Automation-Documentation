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
