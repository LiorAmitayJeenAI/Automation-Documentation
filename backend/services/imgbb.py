"""
imgbb image hosting integration.

Uploads local image files to imgbb (https://api.imgbb.com/) to obtain
publicly accessible URLs that end with a recognized image extension.
This lets Gamma reliably embed the screenshots without requiring an
ngrok/cloudflared tunnel.
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

from backend.config import IMGBB_API_KEY

logger = logging.getLogger(__name__)

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def upload_images(file_paths: list[str]) -> list[dict]:
    """
    Upload local image files to imgbb.

    Returns a list of dicts with the same shape the pipeline expects::

        [{"name": "original.png", "url": "https://i.ibb.co/...",
          "downloadUrl": "https://i.ibb.co/.../original.png"}, ...]

    Skips files that fail to upload (logged as warnings).
    """
    if not IMGBB_API_KEY:
        logger.warning("IMGBB_API_KEY is not set — skipping imgbb upload")
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for fp in file_paths:
            if not os.path.exists(fp):
                logger.warning("Skipping missing file: %s", fp)
                continue

            file_name = os.path.basename(fp)
            with open(fp, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode()

            try:
                resp = await client.post(
                    IMGBB_UPLOAD_URL,
                    data={
                        "key": IMGBB_API_KEY,
                        "image": image_b64,
                        "name": os.path.splitext(file_name)[0],
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})

                display_url = data.get("display_url") or data.get("url", "")
                results.append({
                    "name": file_name,
                    "url": display_url,
                    "downloadUrl": display_url,
                })
                logger.info("Uploaded %s to imgbb: %s", file_name, display_url)

            except Exception as exc:
                logger.warning("Failed to upload %s to imgbb: %s", file_name, exc)

    return results
