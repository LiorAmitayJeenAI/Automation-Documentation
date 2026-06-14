"""
Main pipeline orchestrator.

Replicates the full Langflow documentation_automation flow:
1. Fetch Confluence content
2. LLM formats into presentation_content + screenshot_script
3. Two parallel branches:
   A. Screenshots → upload to SharePoint → read image URLs
   B. (waits for A) Generate Gamma presentation with images → poll → upload PDF to SharePoint
4. Return gamma_url + sharepoint_url
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncGenerator, Any

from urllib.parse import quote

from backend.config import (
    REGULAR_URL,
    ADMIN_URL,
    SP_FOLDER_PATH,
    SP_SCREENSHOTS_FOLDER,
    PUBLIC_BASE_URL,
    IMGBB_API_KEY,
)
from backend.prompts.presentation_style import PRESENTATION_STYLE_PROMPT
from backend.services import confluence, llm, screenshots, gamma, sharepoint, imgbb

logger = logging.getLogger(__name__)


class PipelineEvent:
    """Structured progress event emitted during pipeline execution."""

    def __init__(self, stage: str, status: str, detail: str = "", data: dict | None = None):
        self.stage = stage
        self.status = status
        self.detail = detail
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "status": self.status,
            "detail": self.detail,
            **self.data,
        }


async def run_pipeline(
    confluence_url: str,
    language: str = "he",
    link_type: str = "regular",
    session_id: str = "default_session",
) -> AsyncGenerator[PipelineEvent, None]:
    """
    Run the full documentation automation pipeline, yielding progress events.

    Yields PipelineEvent objects for each major step.
    The final event contains gamma_url and sharepoint_url.
    """
    base_url = ADMIN_URL if link_type == "admin" else REGULAR_URL

    # Per-run SharePoint subfolder so only this run's screenshots are read back
    # and passed to Gamma (avoids accumulating images across runs).
    run_screenshots_folder = f"{SP_SCREENSHOTS_FOLDER}/screenshots/{session_id}"

    # ── Step 1: Fetch Confluence content ──
    yield PipelineEvent("confluence", "running", "Fetching Confluence page content...")
    try:
        title, markdown_content = await confluence.fetch_page_as_markdown(confluence_url)
        yield PipelineEvent("confluence", "done", f"Fetched: {title}", {"title": title})
    except Exception as exc:
        yield PipelineEvent("confluence", "error", str(exc))
        return

    # ── Step 2: LLM formats the document ──
    yield PipelineEvent("llm", "running", "Formatting document with AI...")
    try:
        formatted = await llm.format_document(markdown_content, language=language, base_url=base_url)
        presentation_content = formatted["presentation_content"]
        screenshot_script = formatted.get("screenshot_script", [])
        presentation_title = llm.extract_title(presentation_content)
        yield PipelineEvent(
            "llm", "done",
            f"Formatted document. {len(screenshot_script)} screenshots planned.",
            {"presentation_title": presentation_title, "screenshot_count": len(screenshot_script)},
        )
    except Exception as exc:
        yield PipelineEvent("llm", "error", str(exc))
        return

    # ── Step 3: Parallel branches ──
    # Branch B (screenshots) runs first, then Branch A uses the image URLs.

    # ── Branch B: Screenshots ──
    image_list: list[dict] = []
    images_publicly_accessible = False
    if screenshot_script:
        yield PipelineEvent("screenshots", "running", f"Taking {len(screenshot_script)} screenshots...")
        try:
            screenshot_results = await screenshots.take_screenshots(screenshot_script, base_url=base_url)
            saved_paths = [r["path"] for r in screenshot_results]
            yield PipelineEvent(
                "screenshots", "done",
                f"Captured {len(saved_paths)} screenshots",
                {"screenshot_paths": saved_paths},
            )

            if saved_paths:
                yield PipelineEvent("screenshot_upload", "running", "Uploading screenshots to SharePoint...")
                upload_results = await sharepoint.upload_local_files(
                    saved_paths, run_screenshots_folder, session_id=session_id,
                )
                yield PipelineEvent(
                    "screenshot_upload", "done",
                    f"Uploaded {len(upload_results)} screenshots",
                )

                # Build a path-to-metadata lookup for attaching action/slide_section
                _path_meta = {r["path"]: r for r in screenshot_results}

                # Try imgbb first (gives clean public .png URLs), then
                # fall back to PUBLIC_BASE_URL proxy, then aiGenerated.
                if IMGBB_API_KEY:
                    yield PipelineEvent("image_hosting", "running", "Uploading screenshots to imgbb...")
                    image_list = await imgbb.upload_images(saved_paths)
                    if image_list:
                        images_publicly_accessible = True
                        # Attach screenshot metadata to each image for Gamma captions
                        for img, path in zip(image_list, saved_paths):
                            meta = _path_meta.get(path, {})
                            img["action"] = meta.get("action", "")
                            img["slide_section"] = meta.get("slide_section", "")
                        yield PipelineEvent(
                            "image_hosting", "done",
                            f"Hosted {len(image_list)} images on imgbb",
                        )
                    else:
                        yield PipelineEvent("image_hosting", "error", "imgbb upload returned no results")

                if not images_publicly_accessible:
                    yield PipelineEvent("image_read", "running", "Reading screenshot URLs from SharePoint...")
                    image_list = await sharepoint.read_image_urls(
                        run_screenshots_folder, session_id=session_id,
                    )

                    if PUBLIC_BASE_URL:
                        for img in image_list:
                            name = img.get("name", "")
                            if name:
                                img["downloadUrl"] = (
                                    f"{PUBLIC_BASE_URL}/screenshots/"
                                    f"{quote(session_id)}/{quote(name)}"
                                )
                        images_publicly_accessible = True
                    else:
                        logger.warning(
                            "Neither IMGBB_API_KEY nor PUBLIC_BASE_URL is set — "
                            "SharePoint download URLs lack a .png extension and "
                            "will be skipped by Gamma. Falling back to AI-generated images."
                        )

                    # Attach screenshot metadata by matching filenames
                    for img in image_list:
                        name = img.get("name", "")
                        for meta in screenshot_results:
                            if name and name in meta.get("path", ""):
                                img["action"] = meta.get("action", "")
                                img["slide_section"] = meta.get("slide_section", "")
                                break

                    yield PipelineEvent(
                        "image_read", "done",
                        f"Found {len(image_list)} images in SharePoint",
                    )

        except Exception as exc:
            logger.warning("Screenshot branch failed (continuing without images): %s", exc)
            yield PipelineEvent("screenshots", "error", f"Screenshots failed: {exc}")
    else:
        yield PipelineEvent("screenshots", "skipped", "No screenshots in script")

    # ── Branch A: Gamma presentation ──
    yield PipelineEvent("gamma", "running", "Generating presentation with Gamma...")
    try:
        gamma_result = await gamma.generate_and_wait(
            title=presentation_title,
            prompt=presentation_content,
            language=language,
            additional_instructions=PRESENTATION_STYLE_PROMPT,
            images=image_list,
            use_provided_images=images_publicly_accessible,
        )

        if gamma_result.get("status") != "completed":
            yield PipelineEvent("gamma", "error", f"Gamma generation failed: {gamma_result.get('status')}")
            return

        gamma_url = gamma_result.get("gamma_url")
        pdf_url = gamma_result.get("pdf_url")
        yield PipelineEvent(
            "gamma", "done",
            "Presentation generated",
            {"gamma_url": gamma_url},
        )
    except Exception as exc:
        yield PipelineEvent("gamma", "error", str(exc))
        return

    # ── Step 4: Upload PDF to SharePoint ──
    sharepoint_url = None
    if pdf_url:
        yield PipelineEvent("pdf_upload", "running", "Uploading PDF to SharePoint...")
        try:
            # Build a stable, unique file name from the Confluence page id and
            # presentation title. The Gamma export name is derived from the deck
            # title (often just the part number, e.g. "2.pdf") and collides
            # across tutorials, overwriting previously uploaded PDFs.
            page_id = confluence.extract_page_id(confluence_url)
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", presentation_title).strip("-")[:60]
            if page_id:
                file_name = f"{page_id}-{slug}.pdf" if slug else f"{page_id}.pdf"
            else:
                file_name = f"{slug or 'presentation'}.pdf"

            upload_result = await sharepoint.download_url_and_upload(
                pdf_url, file_name, SP_FOLDER_PATH, session_id=session_id,
            )
            sharepoint_url = upload_result.get("webUrl")
            yield PipelineEvent(
                "pdf_upload", "done",
                "PDF uploaded to SharePoint",
                {"sharepoint_url": sharepoint_url},
            )
        except Exception as exc:
            logger.error("PDF upload failed: %s", exc)
            yield PipelineEvent("pdf_upload", "error", str(exc))

    # ── Final result ──
    yield PipelineEvent(
        "complete", "done",
        "Pipeline completed",
        {"gamma_url": gamma_url, "sharepoint_url": sharepoint_url},
    )
