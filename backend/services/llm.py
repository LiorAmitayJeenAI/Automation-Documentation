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


def _load_allowed_routes() -> list[dict]:
    """Load the curated list of valid product routes used to constrain screenshots."""
    try:
        with open(_ROUTES_MAP_PATH, encoding="utf-8") as f:
            routes = json.load(f)
        if isinstance(routes, list):
            return [r for r in routes if isinstance(r, dict) and r.get("path")]
    except FileNotFoundError:
        logger.warning("routes_map.json not found at %s — screenshots unconstrained", _ROUTES_MAP_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load routes_map.json: %s", exc)
    return []


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
) -> dict:
    """
    Send Confluence markdown to Azure OpenAI and get back structured JSON:
    {
        "presentation_content": "...",
        "screenshot_script": [{"url": "...", "action": "..."}, ...]
    }
    """
    language_name = "Hebrew" if language == "he" else "English"
    allowed_routes = _format_allowed_routes(_load_allowed_routes(), base_url)
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
