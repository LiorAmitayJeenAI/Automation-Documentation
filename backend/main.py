"""
FastAPI entry point for the documentation automation backend.

Exposes a single endpoint POST /api/generate that streams SSE progress events
and returns gamma_url + sharepoint_url upon completion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.config import BACKEND_PORT, SP_SCREENSHOTS_FOLDER, REGULAR_URL, ADMIN_URL
from backend.pipeline import run_pipeline
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


class DiscoverRoutesRequest(BaseModel):
    link_type: str = "regular"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/discover-routes")
async def discover_routes(req: DiscoverRoutesRequest):
    """
    Crawl the product to discover navigable routes and merge any new ones
    (add-only) into routes_map.json. Returns a summary of what was added.
    """
    base_url = ADMIN_URL if req.link_type == "admin" else REGULAR_URL
    try:
        result = await route_crawler.discover_and_merge(base_url, req.link_type)
        return result
    except Exception as exc:
        logger.error("Route discovery failed: %s", exc)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=BACKEND_PORT,
        reload=True,
    )
