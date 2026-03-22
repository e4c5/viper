"""One-shot httpx GET/POST/PUT/DELETE helpers shared by SCM providers."""

from __future__ import annotations

from typing import Any

import httpx


def _json_or_text_response(r: httpx.Response) -> Any:
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        return r.json()
    return r.text


def http_get_json_or_text(url: str, *, headers: dict[str, str], timeout: float) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return _json_or_text_response(r)


def http_get_bytes(url: str, *, headers: dict[str, str], timeout: float) -> bytes:
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.content


def http_post_json(
    url: str, body: dict[str, Any], *, headers: dict[str, str], timeout: float
) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        return r.json() if r.content else None


def http_put_json(
    url: str, body: dict[str, Any], *, headers: dict[str, str], timeout: float
) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.put(url, headers=headers, json=body)
        r.raise_for_status()
        return r.json() if r.content else None


def http_delete(url: str, *, headers: dict[str, str], timeout: float) -> None:
    with httpx.Client(timeout=timeout) as client:
        r = client.delete(url, headers=headers)
        r.raise_for_status()
