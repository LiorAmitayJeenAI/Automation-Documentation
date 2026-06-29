"""
FastAPI entry point for the documentation automation backend.

Exposes a single endpoint POST /api/generate that streams SSE progress events
and returns gamma_url + sharepoint_url upon completion.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.config import (
    BACKEND_PORT,
    SP_SCREENSHOTS_FOLDER,
    SP_EXPORT_BASE,
    SP_EXCEL_EXPORT_PATH,
    REGULAR_URL,
    ADMIN_URL,
    FINOPS_URL,
)
from backend.pipeline import run_pipeline
from backend.video_pipeline import run_video_pipeline
from backend.services import sharepoint, route_crawler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Documentation Automation Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_active_tasks: dict[str, asyncio.Task] = {}


class GenerateRequest(BaseModel):
    confluence_url: str
    language: str = "he"
    link_type: str = "regular"
    session_id: str = "default_session"
    folder_name: str = ""
    part_name: str = ""


class DiscoverRoutesRequest(BaseModel):
    link_type: str = "regular"
    target_paths: list[str] | None = None


class ExportPdfsRequest(BaseModel):
    folder_name: str
    pdf_urls: list[str]
    session_id: str = "default_session"


class SyncExportFolderRequest(BaseModel):
    prefix: str
    folder_name: str
    current_urls: list[str]
    session_id: str = "default_session"


class UploadExcelRequest(BaseModel):
    file_name: str = "tutorials-library.xlsx"
    file_base64: str
    session_id: str = "default_session"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/discover-routes")
async def discover_routes(req: DiscoverRoutesRequest):
    """
    Crawl the product to discover navigable routes and merge any new ones
    (add-only) into routes_map.json. Returns a summary of what was added.
    """
    if req.link_type == "admin":
        base_url = ADMIN_URL
    elif req.link_type == "finops":
        base_url = FINOPS_URL
    else:
        base_url = REGULAR_URL
    try:
        result = await route_crawler.discover_and_merge(base_url, req.link_type, target_paths=req.target_paths)
        return result
    except Exception as exc:
        logger.error("Route discovery failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/export-pdfs-to-sharepoint")
async def export_pdfs_to_sharepoint(req: ExportPdfsRequest):
    """
    Copy the given SharePoint PDFs into a new folder under SP_EXPORT_BASE.
    Returns {"folderUrl", "uploaded", "skipped"} on success.
    """
    if not req.pdf_urls:
        return JSONResponse(status_code=400, content={"error": "No PDFs to export"})

    dest_folder = f"{SP_EXPORT_BASE.strip('/')}/{req.folder_name.strip('/')}"
    try:
        result = await sharepoint.copy_pdfs_to_folder(
            req.pdf_urls, dest_folder, session_id=req.session_id,
        )
        return result
    except RuntimeError as exc:
        # Device-flow auth required / pending — surface the message so the UI
        # can show the verification URL + code.
        logger.warning("SharePoint export auth required: %s", exc)
        return JSONResponse(status_code=401, content={"error": str(exc)})
    except Exception as exc:
        logger.error("SharePoint export failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/sync-export-folder")
async def sync_export_folder(req: SyncExportFolderRequest):
    """
    Date-based export with carry-forward.

    1. List subfolders under SP_EXPORT_BASE that start with *prefix*.
    2. Find the most recent one that is NOT *folder_name* (previous date).
    3. Copy all files from that previous folder into today's folder.
    4. Copy/overwrite the caller-supplied *current_urls* into today's folder.

    Returns {"folderUrl", "uploaded", "carried_forward", "skipped"}.
    """
    if not req.current_urls and not req.prefix:
        return JSONResponse(status_code=400, content={"error": "Nothing to export"})

    dest_folder = f"{SP_EXPORT_BASE.strip('/')}/{req.folder_name.strip('/')}"
    carried = {"copied": 0, "skipped": 0}

    try:
        subfolders = await sharepoint.list_subfolders(
            SP_EXPORT_BASE, session_id=req.session_id,
        )
        matching = sorted(
            [f for f in subfolders if f.startswith(req.prefix) and f != req.folder_name],
        )
        if matching:
            prev_folder = f"{SP_EXPORT_BASE.strip('/')}/{matching[-1]}"
            logger.info("Carrying forward from %s → %s", prev_folder, dest_folder)
            carried = await sharepoint.copy_folder_contents(
                prev_folder, dest_folder, session_id=req.session_id,
            )

        upload_result = await sharepoint.copy_pdfs_to_folder(
            req.current_urls, dest_folder, session_id=req.session_id,
        )

        return {
            "folderUrl": upload_result.get("folderUrl"),
            "uploaded": len(upload_result.get("uploaded", [])),
            "uploadedFiles": upload_result.get("uploaded", []),
            "carried_forward": carried.get("copied", 0),
            "skipped": upload_result.get("skipped", 0) + carried.get("skipped", 0),
        }

    except RuntimeError as exc:
        logger.warning("SharePoint sync auth required: %s", exc)
        return JSONResponse(status_code=401, content={"error": str(exc)})
    except Exception as exc:
        logger.error("SharePoint sync failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/upload-excel-to-sharepoint")
async def upload_excel_to_sharepoint(req: UploadExcelRequest):
    """
    Upload a base64-encoded Excel file to SP_EXCEL_EXPORT_PATH.
    Returns {"webUrl"} on success.
    """
    try:
        file_bytes = base64.b64decode(req.file_base64)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid base64 data"})

    try:
        result = await sharepoint.upload_file_bytes(
            file_bytes, req.file_name, SP_EXCEL_EXPORT_PATH,
            session_id=req.session_id,
        )
        return {"webUrl": result.get("webUrl")}
    except RuntimeError as exc:
        logger.warning("SharePoint Excel upload auth required: %s", exc)
        return JSONResponse(status_code=401, content={"error": str(exc)})
    except Exception as exc:
        logger.error("SharePoint Excel upload failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/screenshots/{session_id}/{filename}")
async def serve_screenshot(session_id: str, filename: str):
    """
    Proxy a screenshot stored in SharePoint and serve it with a real image
    extension and content-type. Used when PUBLIC_BASE_URL is configured so that
    Gamma can fetch the screenshots from a public ".png" URL.
    """
    folder_path = f"{SP_SCREENSHOTS_FOLDER}/screenshots/{session_id}"
    try:
        content, content_type = await sharepoint.download_file_bytes(
            folder_path, filename, session_id=session_id,
        )
    except Exception as exc:
        logger.warning("Failed to serve screenshot %s/%s: %s", session_id, filename, exc)
        return JSONResponse(status_code=404, content={"error": "screenshot not found"})

    if not content_type.startswith("image/"):
        content_type = "image/png"
    return Response(content=content, media_type=content_type)


@app.post("/api/generate")
async def generate(req: GenerateRequest, request: Request):
    """
    Run the full documentation automation pipeline.
    Streams SSE events for progress, final event contains gamma_url and sharepoint_url.
    """

    async def event_stream() -> AsyncGenerator[dict, None]:
        async for event in run_pipeline(
            confluence_url=req.confluence_url,
            language=req.language,
            link_type=req.link_type,
            session_id=req.session_id,
            folder_name=req.folder_name,
            part_name=req.part_name,
        ):
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping pipeline stream")
                break
            yield {"data": json.dumps(event.to_dict())}

    return EventSourceResponse(event_stream())


@app.post("/api/generate/sync")
async def generate_sync(req: GenerateRequest, request: Request):
    """
    Synchronous version — runs the full pipeline and returns the final result as JSON.
    Useful for the Node.js server which can parse the result directly.
    """
    last_event = None
    error_msg = None

    async for event in run_pipeline(
        confluence_url=req.confluence_url,
        language=req.language,
        link_type=req.link_type,
        session_id=req.session_id,
        folder_name=req.folder_name,
        part_name=req.part_name,
    ):
        if await request.is_disconnected():
            logger.info("Client disconnected during sync generate, stopping pipeline")
            return JSONResponse(
                status_code=499,
                content={"error": "Client disconnected"},
            )
        last_event = event
        if event.status == "error" and event.stage != "screenshots":
            error_msg = event.detail
            break

    if error_msg:
        return JSONResponse(
            status_code=500,
            content={"error": error_msg},
        )

    if last_event:
        return last_event.to_dict()

    return JSONResponse(
        status_code=500,
        content={"error": "Pipeline produced no events"},
    )


@app.post("/api/generate-video/stream")
async def generate_video_stream(req: GenerateRequest, request: Request):
    """
    Streaming SSE version of the video pipeline.
    Emits one event per stage so callers can show live progress.
    """

    async def event_stream() -> AsyncGenerator[dict, None]:
        async for event in run_video_pipeline(
            confluence_url=req.confluence_url,
            language=req.language,
            link_type=req.link_type,
            session_id=req.session_id,
            folder_name=req.folder_name,
            part_name=req.part_name,
        ):
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping video pipeline stream")
                break
            logger.info(
                "[video-stream] stage=%s status=%s — %s",
                event.stage, event.status, event.detail,
            )
            yield {"data": json.dumps(event.to_dict())}

    return EventSourceResponse(event_stream())


@app.post("/api/generate-video/sync")
async def generate_video_sync(req: GenerateRequest, request: Request):
    """
    Blocking JSON version (used by fire-and-forget paths that don't need streaming).
    Returns {video_url, title} on success.
    """
    last_event = None
    error_msg = None

    async for event in run_video_pipeline(
        confluence_url=req.confluence_url,
        language=req.language,
        link_type=req.link_type,
        session_id=req.session_id,
        folder_name=req.folder_name,
        part_name=req.part_name,
    ):
        if await request.is_disconnected():
            return JSONResponse(status_code=499, content={"error": "Client disconnected"})
        logger.info(
            "[video-sync] stage=%s status=%s — %s",
            event.stage, event.status, event.detail,
        )
        last_event = event
        if event.status == "error":
            error_msg = event.detail
            break

    if error_msg:
        return JSONResponse(status_code=500, content={"error": error_msg})

    if last_event:
        return last_event.to_dict()

    return JSONResponse(status_code=500, content={"error": "Video pipeline produced no events"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=BACKEND_PORT,
        reload=True,
    )
