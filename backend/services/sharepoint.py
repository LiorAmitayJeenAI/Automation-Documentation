"""
SharePoint integration via Microsoft Graph API.

Ported from the Langflow custom components (GammaPDFToSharePoint,
SharePointBatchUploader, SharePointImageFolderReader).
Uses device-flow OAuth with a file-based token cache.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from backend.config import SP_TENANT_ID, SP_CLIENT_ID, SP_SITE_URL

logger = logging.getLogger(__name__)

GRAPH_V1 = "https://graph.microsoft.com/v1.0"
CACHE_PATH = os.environ.get(
    "SP_TOOL_CACHE",
    str(Path.home() / ".langflow" / "sp_tool_cache.json"),
)
SCOPES = [
    "openid",
    "profile",
    "offline_access",
    "https://graph.microsoft.com/Sites.ReadWrite.All",
    "https://graph.microsoft.com/Files.ReadWrite.All",
]

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


# ── Token cache helpers ──────────────────────────────────────

def _load_cache() -> dict:
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f)


def _get_session(session_id: str) -> dict:
    return _load_cache().get("sessions", {}).get(session_id, {})


def _put_session(session_id: str, session: dict) -> None:
    root = _load_cache()
    root.setdefault("sessions", {})[session_id] = session
    _save_cache(root)


def _is_valid(session: dict) -> bool:
    expires_at = session.get("expires_at")
    return bool(expires_at and time.time() < expires_at - 60)


def _authority(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0"


def _scope_str() -> str:
    return " ".join(SCOPES)


def _identify_user_key(token: str) -> str | None:
    try:
        resp = httpx.get(
            f"{GRAPH_V1}/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            return (data.get("userPrincipalName") or data.get("mail") or "").strip() or None
    except Exception:
        pass
    return None


def _save_tokens(session_id: str, access_token: str, refresh_token: str | None, expires_in: int) -> str:
    root = _load_cache()
    expires_at = time.time() + int(expires_in or 3600)

    root.setdefault("sessions", {})[session_id] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    user_key = _identify_user_key(access_token) or "me"
    root.setdefault("users", {})[user_key] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    root["last_user_key_global"] = user_key
    _save_cache(root)
    return user_key


def _load_best_token() -> dict | None:
    root = _load_cache()
    user_key = root.get("last_user_key_global")
    if not user_key:
        return None
    entry = root.get("users", {}).get(user_key)
    if entry and _is_valid(entry):
        return entry
    return None


def _ensure_token(
    tenant_id: str = SP_TENANT_ID,
    client_id: str = SP_CLIENT_ID,
    session_id: str = "default_session",
) -> str:
    """
    Return a valid access token, refreshing or starting device flow as needed.
    Raises RuntimeError if auth flow is required (interactive).
    """
    session = _get_session(session_id)

    # 1. Valid token in session
    if session.get("access_token") and _is_valid(session):
        return session["access_token"]

    # 2. Valid token in user store
    best = _load_best_token()
    if best and best.get("access_token"):
        _put_session(session_id, best)
        return best["access_token"]

    # 3. Try refresh
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        root = _load_cache()
        user_key = root.get("last_user_key_global")
        if user_key:
            entry = root.get("users", {}).get(user_key, {})
            refresh_token = entry.get("refresh_token")

    if refresh_token:
        resp = httpx.post(
            f"{_authority(tenant_id)}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": _scope_str(),
            },
            timeout=20,
        )
        body = resp.json()
        if body.get("access_token"):
            _save_tokens(
                session_id,
                body["access_token"],
                body.get("refresh_token", refresh_token),
                body.get("expires_in", 3600),
            )
            return body["access_token"]

    # 4. Try polling existing device code
    device = session.get("device")
    if device:
        resp = httpx.post(
            f"{_authority(tenant_id)}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            },
            timeout=20,
        )
        body = resp.json()
        if body.get("access_token"):
            _save_tokens(
                session_id,
                body["access_token"],
                body.get("refresh_token"),
                body.get("expires_in", 3600),
            )
            return body["access_token"]

        raise RuntimeError(
            f"SharePoint auth pending — visit {device['verification_uri']} and enter code: {device['user_code']}"
        )

    # 5. Start new device flow
    resp = httpx.post(
        f"{_authority(tenant_id)}/devicecode",
        data={"client_id": client_id, "scope": _scope_str()},
        timeout=20,
    )
    body = resp.json()
    if "device_code" in body:
        session["device"] = body
        _put_session(session_id, session)
        raise RuntimeError(
            f"SharePoint auth required — visit {body['verification_uri']} and enter code: {body['user_code']}"
        )

    raise RuntimeError("SharePoint authentication failed entirely")


# ── Graph API helpers ────────────────────────────────────────

def _get_drive_id(access_token: str, site_url: str = SP_SITE_URL) -> tuple[str, str]:
    """Return (site_id, drive_id) for the default document library."""
    headers = {"Authorization": f"Bearer {access_token}"}
    parsed = urlparse(site_url)

    resp = httpx.get(
        f"{GRAPH_V1}/sites/{parsed.hostname}:/{parsed.path.strip('/')}",
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    site_id = resp.json()["id"]

    resp = httpx.get(
        f"{GRAPH_V1}/sites/{site_id}/drives",
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    drives = resp.json()["value"]
    if not drives:
        raise RuntimeError("No document libraries found on SharePoint site")

    return site_id, drives[0]["id"]


# ── Public API ───────────────────────────────────────────────

async def upload_file_bytes(
    file_content: bytes,
    file_name: str,
    folder_path: str,
    session_id: str = "default_session",
) -> dict:
    """Upload raw bytes to SharePoint. Returns {"name", "webUrl", "size"}."""
    token = _ensure_token(session_id=session_id)
    _, drive_id = _get_drive_id(token)

    folder_path = folder_path.strip("/")
    upload_url = (
        f"{GRAPH_V1}/drives/{drive_id}"
        f"/root:/{quote(folder_path)}/{quote(file_name)}:/content"
    )

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        resp = await client.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=file_content,
        )
        resp.raise_for_status()

    uploaded = resp.json()
    return {
        "name": uploaded.get("name"),
        "webUrl": uploaded.get("webUrl"),
        "size": uploaded.get("size"),
    }


async def upload_local_files(
    file_paths: list[str],
    folder_path: str,
    session_id: str = "default_session",
) -> list[dict]:
    """Upload multiple local files to SharePoint."""
    token = _ensure_token(session_id=session_id)
    _, drive_id = _get_drive_id(token)
    folder_path = folder_path.strip("/")

    results = []
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        for fp in file_paths:
            if not os.path.exists(fp):
                logger.warning("Skipping missing file: %s", fp)
                continue

            file_name = os.path.basename(fp)
            with open(fp, "rb") as f:
                content = f.read()

            upload_url = (
                f"{GRAPH_V1}/drives/{drive_id}"
                f"/root:/{quote(folder_path)}/{quote(file_name)}:/content"
            )
            resp = await client.put(
                upload_url,
                headers={
                    "Authorization": f"Bearer {token}",
                },
                content=content,
            )
            resp.raise_for_status()
            uploaded = resp.json()
            results.append({
                "name": uploaded.get("name"),
                "webUrl": uploaded.get("webUrl"),
                "size": uploaded.get("size"),
            })
            logger.info("Uploaded %s (%d bytes)", file_name, len(content))

    return results


async def read_image_urls(
    folder_path: str,
    session_id: str = "default_session",
) -> list[dict]:
    """
    List all image files in a SharePoint folder.
    Returns [{"name": ..., "url": ..., "downloadUrl": ...}, ...].
    """
    token = _ensure_token(session_id=session_id)
    _, drive_id = _get_drive_id(token)
    folder_path = folder_path.strip("/")

    url = f"{GRAPH_V1}/drives/{drive_id}/root:/{quote(folder_path)}:/children"
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()

    items = resp.json().get("value", [])
    images = []
    for item in items:
        name = item.get("name", "")
        ext = os.path.splitext(name)[1].lower()
        if ext in image_exts:
            images.append({
                "name": name,
                "url": item.get("webUrl"),
                "downloadUrl": item.get("@microsoft.graph.downloadUrl"),
            })

    return images


async def download_file_bytes(
    folder_path: str,
    file_name: str,
    session_id: str = "default_session",
) -> tuple[bytes, str]:
    """
    Download a single file's raw bytes from a SharePoint folder.
    Returns (content, content_type). Used by the backend proxy route that
    serves screenshots to Gamma with a recognized image extension.
    """
    token = _ensure_token(session_id=session_id)
    _, drive_id = _get_drive_id(token)
    folder_path = folder_path.strip("/")

    url = (
        f"{GRAPH_V1}/drives/{drive_id}"
        f"/root:/{quote(folder_path)}/{quote(file_name)}:/content"
    )

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, content_type


async def download_url_and_upload(
    download_url: str,
    file_name: str,
    folder_path: str,
    session_id: str = "default_session",
) -> dict:
    """Download a file from a URL and upload it to SharePoint."""
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT) as client:
        resp = await client.get(download_url)
        resp.raise_for_status()
        content = resp.content

    if not file_name.endswith(".pdf"):
        file_name = "presentation.pdf"

    return await upload_file_bytes(content, file_name, folder_path, session_id)
