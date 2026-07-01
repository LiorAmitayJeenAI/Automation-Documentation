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
from backend.prompts.narration import NARRATION_PROMPT, VIDEO_NARRATION_REGEN_PROMPT
from backend.prompts.video_script import VIDEO_SCRIPT_PROMPT, get_language_instructions

logger = logging.getLogger(__name__)

_ROUTES_MAP_PATH = Path(__file__).resolve().parent.parent / "routes_map.json"

_client = AsyncAzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)


def _normalize_link_type(value: str | None) -> str:
    if value == "admin":
        return "admin"
    if value == "finops":
        return "finops"
    return "regular"


def _route_link_type(route: dict) -> str:
    return _normalize_link_type(route.get("link_type"))


def _load_allowed_routes(link_type: str = "regular", language: str = "he") -> list[dict]:
    """Load the curated list of valid product routes used to constrain screenshots.

    Routes with a "language" field are only included when it matches the requested
    language. Routes without a "language" field are shared and always included.
    """
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
                    and (r.get("language") is None or r.get("language") == language)
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


_DIALOG_ONLY_LABELS = re.compile(
    r"^(cancel|close|back|dismiss|ok|בטל|ביטול|סגור|חזור|אישור)$",
    re.IGNORECASE,
)


def _is_top_level_noise(variants: list[str]) -> bool:
    """True when a TOP-LEVEL button is a modal/dialog control the model should
    never pick as a feature action (e.g. Cancel/Close/Back). Such buttons only
    make sense inside a dialog, so offering them at the page level leads the LLM
    to pick contradictory actions (e.g. clicking "Cancel" to "add a user").
    """
    for v in variants:
        if _DIALOG_ONLY_LABELS.match(" ".join(v.split())):
            return True
    return False


def _format_clickable_elements(route: dict) -> str:
    """Render a route's verified clickable buttons for the prompt.

    Each on-page button is shown with all of its label variants (Hebrew /
    English) joined by " / ", and buttons are separated by commas. Used to
    constrain the LLM's interaction click steps to real, on-page buttons.

    A button that reveals other buttons when clicked (an opener) lists those
    child buttons inline as "(opens -> ...)", so the LLM knows it must click the
    parent first to reach them. Returns an empty string when the route has no
    recorded clickable elements.

    Modal/dialog-only controls (Cancel/Close/Back) and buttons explicitly tagged
    ``modal_only`` are dropped from the TOP-LEVEL menu so the model cannot pick a
    button whose meaning contradicts the step (they remain available nested under
    their opener when reached through a real flow).
    """
    buttons: list[str] = []
    for el in route.get("clickable_elements") or []:
        variants = _element_variants(el)
        if not variants:
            continue
        children = el.get("opens") if isinstance(el, dict) else None
        modal_only = bool(el.get("modal_only")) if isinstance(el, dict) else False
        if not children and (modal_only or _is_top_level_noise(variants)):
            continue
        label = " / ".join(f'"{v}"' for v in variants)
        if isinstance(el, dict) and el.get("default_expanded"):
            label += " [already open on page load]"
        if children:
            child_labels = [
                " / ".join(f'"{v}"' for v in _element_variants(c))
                for c in children
                if _element_variants(c)
            ]
            if child_labels:
                label += f" (opens -> {'; '.join(child_labels)})"
        buttons.append(label)
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
        input_fields = r.get("input_fields")
        if input_fields:
            labels = ", ".join(f'"{f["label"]}"' for f in input_fields if f.get("label"))
            if labels:
                lines.append(f"    fillable inputs: {labels}")
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
    allowed_routes = _format_allowed_routes(_load_allowed_routes(link_type, language), base_url)
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
    allowed_routes = _format_allowed_routes(_load_allowed_routes(link_type, language), base_url)
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


async def pick_live_target(
    goal: str,
    intended_label: str,
    candidates: list[str],
    language: str = "he",
) -> str | None:
    """
    Live-recovery tiebreak: the planned button could not be found on the page.
    Given the step's goal and the buttons ACTUALLY visible right now, pick the
    single button that best advances the goal — or decline.

    Returns the chosen label (guaranteed to be an exact, whitespace-insensitive
    member of *candidates*) or None when nothing fits. Never invents a label.
    """
    if not candidates:
        return None

    system = (
        "You are operating a live web app to record a product tutorial. The "
        "planned button could not be found on the current screen. From the list "
        "of buttons that are ACTUALLY visible on the page right now, choose the "
        "single button that best performs the step's goal. Respond with ONLY the "
        "exact button text copied verbatim from the list, or the single word "
        "NONE if no button genuinely fits. Never invent or translate a label. "
        "Never choose a destructive/irreversible button (delete, save, submit, "
        "publish, confirm) or a dismiss button (cancel, close, back)."
    )
    user = json.dumps(
        {
            "goal": goal,
            "planned_label": intended_label,
            "visible_buttons": candidates,
        },
        ensure_ascii=False,
    )

    try:
        response = await _client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_completion_tokens=60,
        )
    except Exception as exc:
        logger.debug("pick_live_target LLM error: %s", exc)
        return None

    choice = _strip_code_fences((response.choices[0].message.content or "").strip())
    choice = choice.strip().strip('"').strip()
    if not choice or choice.upper() == "NONE":
        return None

    # Accept only an exact (whitespace-insensitive) member of the candidate list.
    norm = " ".join(choice.split()).lower()
    for c in candidates:
        if " ".join(c.split()).lower() == norm:
            logger.info("pick_live_target chose %r for goal %r", c, goal)
            return c
    logger.debug("pick_live_target returned non-candidate %r — ignoring", choice)
    return None


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


def _describe_failed_interaction(step: dict) -> str:
    """Build a short human-readable description of the click/tab that did not happen."""
    labels: list[str] = []
    for inter in step.get("interactions") or []:
        kind = (inter.get("type") or "click").lower()
        if kind == "click" and inter.get("text"):
            labels.append(str(inter["text"]).strip())
        elif kind == "fill" and inter.get("label"):
            labels.append(str(inter["label"]).strip())
    if labels:
        return ", ".join(labels)
    return step.get("action", "")


def _describe_actual_state(step: dict) -> tuple[str, str]:
    """Return (outcome, performed_action) describing what really happened on a
    flagged step, so narration can be rewritten to match the screen.

    - "adapted": a different real control was clicked (the recovered label).
    - "toured":  the page was scroll-toured as an overview.
    - "failed":  nothing landed; the plain base page is shown.
    """
    outcome = step.get("outcome")
    if outcome == "adapted" and step.get("adapted_labels"):
        return "adapted", ", ".join(str(x) for x in step["adapted_labels"])
    if outcome == "toured":
        return "toured", ""
    return "failed", ""


async def regenerate_narrations(
    recorded_steps: list[dict],
    language: str = "he",
) -> list[dict]:
    """
    Rewrite narration for steps whose on-screen result diverged from the plan.

    The recorder flags a step with ``interaction_failed: True`` and an ``outcome``
    of ``adapted`` (a different real control was clicked), ``toured`` (the page was
    scroll-toured as an overview), or ``failed`` (plain base page). In every case
    the original narration no longer matches the screen, so this pass asks the LLM
    to rewrite ONLY those flagged steps to match what actually happened. Steps that
    recorded as planned are returned unchanged.

    Returns the same list with the ``narration`` field replaced on flagged steps.
    Falls back to the original narration on any error.
    """
    failed_indices = [
        i for i, s in enumerate(recorded_steps) if s.get("interaction_failed")
    ]
    if not failed_indices:
        return recorded_steps

    logger.info(
        "Regenerating narration for %d step(s) whose on-screen result changed",
        len(failed_indices),
    )

    input_data = []
    for i in failed_indices:
        s = recorded_steps[i]
        outcome, performed = _describe_actual_state(s)
        input_data.append({
            "url": s.get("url", ""),
            "action": s.get("action", ""),
            "outcome": outcome,
            "planned_interaction": _describe_failed_interaction(s),
            "performed_action": performed,
            "original_narration": s.get("narration", ""),
            "language": language,
        })

    try:
        response = await _client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": VIDEO_NARRATION_REGEN_PROMPT},
                {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
            ],
            max_completion_tokens=2000,
        )
        raw = response.choices[0].message.content or ""
        cleaned = _strip_code_fences(raw)
        rewritten = json.loads(cleaned)
        if not isinstance(rewritten, list):
            raise ValueError("Expected JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Narration regeneration failed (%s) — keeping originals", exc)
        return recorded_steps

    updated = [dict(s) for s in recorded_steps]
    for n, idx in enumerate(failed_indices):
        if n < len(rewritten) and str(rewritten[n]).strip():
            old = updated[idx].get("narration", "")
            updated[idx]["narration"] = str(rewritten[n]).strip()
            logger.info(
                "Step %d narration rewritten:\n  was: %s\n  now: %s",
                idx + 1, old, updated[idx]["narration"],
            )
    return updated


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
