"""Fetch Confluence page content and convert to markdown."""

from __future__ import annotations

import re
from base64 import b64encode

import httpx
from markdownify import markdownify as md

from backend.config import CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_BASE_URL

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _auth_header() -> str:
    creds = b64encode(f"{CONFLUENCE_EMAIL}:{CONFLUENCE_API_TOKEN}".encode()).decode()
    return f"Basic {creds}"


def extract_page_id(url: str) -> str | None:
    m = re.search(r"/pages/(\d+)", url)
    return m.group(1) if m else None


async def fetch_page_body_html(page_id: str) -> str:
    """Return the rendered HTML body of a Confluence page."""
    url = (
        f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
        "?expand=body.storage,body.view"
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": _auth_header(),
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()

    data = resp.json()
    html = (
        data.get("body", {}).get("view", {}).get("value")
        or data.get("body", {}).get("storage", {}).get("value")
        or ""
    )
    return html


async def fetch_page_title(page_id: str) -> str:
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{page_id}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": _auth_header(),
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
    return resp.json().get("title", "")


async def fetch_page_as_markdown(confluence_url: str) -> tuple[str, str]:
    """
    Given a Confluence page URL, return (title, markdown_content).
    """
    page_id = extract_page_id(confluence_url)
    if not page_id:
        raise ValueError(f"Cannot extract page ID from URL: {confluence_url}")

    title, html = await fetch_page_title(page_id), await fetch_page_body_html(page_id)
    markdown = md(html, heading_style="ATX", strip=["img"])
    markdown = f"# {title}\n\n{markdown}".strip()
    return title, markdown
