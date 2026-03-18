"""Fetch Confluence page content for context enrichment."""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_MAX_BODY_CHARS = 4_000


class ConfluenceFetcher:
    """Fetch Confluence page content using the Confluence REST API.

    Supports both Confluence Cloud (atlassian.net) and Server/DC instances.
    Authentication: HTTP Basic Auth with email and API token (Cloud) or
    username and password (Server/DC).
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        timeout: float = 30.0,
    ) -> None:
        # base_url may include /wiki suffix or not — normalise it away so we
        # can build canonical REST API paths ourselves.
        self._base_url = base_url.rstrip("/")
        if self._base_url.endswith("/wiki"):
            self._base_url = self._base_url[: -len("/wiki")]
        self._auth = (email, token)
        self._timeout = timeout

    def fetch_page_by_id(self, page_id: str) -> str:
        """Return a human-readable summary of the Confluence page.

        Returns an empty string on any error so the review can proceed without context.
        """
        try:
            return self._fetch_page_by_id(page_id)
        except Exception as exc:
            logger.debug("ConfluenceFetcher: failed to fetch page %s: %s", page_id, exc)
            return ""

    def fetch_page_by_url(self, url: str) -> str:
        """Fetch a Confluence page given its full URL.

        Tries to extract a numeric page ID from the URL first; falls back to a
        title-based search using the space key and page title from the URL.
        Returns an empty string on any error.
        """
        try:
            # Try to extract a page ID from the URL
            page_id = _extract_page_id_from_url(url)
            if page_id:
                return self._fetch_page_by_id(page_id)

            # Try title-based lookup
            space_key, title = _extract_space_and_title_from_url(url)
            if space_key and title:
                return self._fetch_page_by_title(space_key, title)
        except Exception as exc:
            logger.debug("ConfluenceFetcher: failed to fetch page at %s: %s", url, exc)
        return ""

    def _fetch_page_by_id(self, page_id: str) -> str:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(
                f"{self._base_url}/wiki/rest/api/content/{page_id}",
                auth=self._auth,
                params={"expand": "body.storage,title,space"},
            )
            r.raise_for_status()
            data = r.json()
        return _format_page(data)

    def _fetch_page_by_title(self, space_key: str, title: str) -> str:
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(
                f"{self._base_url}/wiki/rest/api/content",
                auth=self._auth,
                params={
                    "spaceKey": space_key,
                    "title": title,
                    "expand": "body.storage,title,space",
                },
            )
            r.raise_for_status()
            data = r.json()

        results = data.get("results", [])
        if not results:
            return ""
        return _format_page(results[0])


def _format_page(data: dict) -> str:
    title = data.get("title", "")
    space = (data.get("space") or {}).get("name", "")
    body_storage = (data.get("body") or {}).get("storage") or {}
    html_body = body_storage.get("value", "")

    plain_body = _html_to_plain_text(html_body)
    if len(plain_body) > _MAX_BODY_CHARS:
        plain_body = plain_body[:_MAX_BODY_CHARS] + " [truncated]"

    lines = [f"Confluence Page: {title}"]
    if space:
        lines.append(f"Space: {space}")
    if plain_body:
        lines.append(f"\n{plain_body}")
    return "\n".join(lines)


def _html_to_plain_text(html: str) -> str:
    """Very light HTML → plain text conversion (strips tags, decodes common entities)."""
    if not html:
        return ""
    # Replace common block elements with newlines
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_page_id_from_url(url: str) -> str:
    """Extract a numeric page ID from a Confluence URL, or return empty string."""
    # Pattern: /pages/{id} or /pages/{id}/
    m = re.search(r"/pages/(\d+)", url)
    if m:
        return m.group(1)
    # Pattern: pageId={id}
    m = re.search(r"[?&]pageId=(\d+)", url)
    if m:
        return m.group(1)
    return ""


def _extract_space_and_title_from_url(url: str) -> tuple[str, str]:
    """Extract (space_key, page_title) from a Confluence display URL.

    Handles URLs like: /wiki/spaces/SPACE/pages/{id}/Title+Here
    and legacy: /display/SPACE/Title+Here
    """
    # /wiki/spaces/SPACEKEY/pages/{id}/Page+Title
    m = re.search(r"/wiki/spaces/([^/]+)/pages/\d+/([^?#]+)", url)
    if m:
        space = m.group(1)
        title = m.group(2).replace("+", " ").replace("%20", " ")
        return space, title

    # /display/SPACEKEY/Page+Title
    m = re.search(r"/display/([^/]+)/([^?#]+)", url)
    if m:
        space = m.group(1)
        title = m.group(2).replace("+", " ").replace("%20", " ")
        return space, title

    return "", ""
