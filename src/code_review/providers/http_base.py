"""Shared HTTP provider helpers for SCM adapters backed by httpx."""

from __future__ import annotations

import logging
import random
import time
from abc import abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from code_review.providers.base import (
    FileInfo,
    PRInfo,
    ProviderInterface,
    RateLimitError,
    _log_pr_info_warning,
    pr_info_from_api_dict,
)
from code_review.providers.http_retry import parse_retry_after

PaginationMode = Literal["page", "next", "start"]
PageToken = str | int | None
FetchPage = Callable[[str, dict[str, Any] | None], Any]
NextPage = Callable[[Any, PageToken], PageToken]
RepeatHook = Callable[[PageToken], None]

logger = logging.getLogger(__name__)

# Module-level so tests can patch it; retry sleeps must never really sleep.
_sleep = time.sleep

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})
# Never wait longer than this on a server-supplied Retry-After; beyond it the
# request fails fast as RateLimitError so callers can move on.
_MAX_RETRY_AFTER_SECONDS = 30.0


def _backoff_seconds(attempt: int) -> float:
    return min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.25)


@dataclass
class _PaginationState:
    path: str
    params: dict[str, Any]
    token: PageToken
    seen_tokens: set[str] = field(default_factory=set)


class HttpXProvider(ProviderInterface):
    """Intermediate base class for SCM providers that talk to HTTP APIs via httpx."""

    _httpx_module = httpx

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @abstractmethod
    def _auth_header(self) -> dict[str, str]:
        """Return auth headers for this provider."""

    def _default_headers(self) -> dict[str, str]:
        return {}

    def _headers(self) -> dict[str, str]:
        return {**self._default_headers(), **self._auth_header()}

    def _api_prefix(self) -> str:
        return ""

    def _build_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            # Absolute URLs come from API responses (e.g. pagination `next`
            # links). Sending the bearer token to a different origin would leak
            # it, so cross-origin URLs are refused outright.
            base = urlparse(self._base_url)
            target = urlparse(path)
            if (target.scheme, target.hostname, target.port) != (
                base.scheme,
                base.hostname,
                base.port,
            ):
                raise ValueError(
                    f"refusing cross-origin URL {path!r}: scheme/host/port must "
                    f"match configured SCM base URL {self._base_url!r}"
                )
            return path
        return f"{self._base_url}{self._api_prefix()}{path}"

    # Retry policy (class attributes so providers/tests can tune):
    #  - 502/503/504 and read timeouts: retry only for idempotent methods.
    #  - ConnectError/ConnectTimeout: the request never reached the server, so
    #    any method may be retried (never re-POST after a response or a read
    #    timeout — that would risk duplicate comments).
    #  - 429 (any method): honour Retry-After (seconds or HTTP-date), capped at
    #    _MAX_RETRY_AFTER_SECONDS; a longer advertised wait fails fast.
    _max_http_retries = 3
    _retry_statuses = frozenset({502, 503, 504})

    def _send_once(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
    ) -> httpx.Response:
        return getattr(client, method.lower())(url, **request_kwargs)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = self._headers()
        if headers:
            request_headers = {**request_headers, **headers}
        request_kwargs: dict[str, Any] = {"headers": request_headers}
        if params:
            request_kwargs["params"] = params
        if json is not None:
            request_kwargs["json"] = json
        url = self._build_url(path)
        idempotent = method.upper() in _IDEMPOTENT_METHODS
        attempt = 0
        with self._httpx_module.Client(timeout=self._timeout) as client:
            while True:
                try:
                    response = self._send_once(client, method, url, request_kwargs)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # Never reached the server: safe to retry any method.
                    if attempt < self._max_http_retries:
                        _sleep(_backoff_seconds(attempt))
                        attempt += 1
                        continue
                    raise
                except (httpx.TimeoutException, httpx.TransportError):
                    # A response may have been written (or timed out mid-read):
                    # only idempotent methods may be retried.
                    if idempotent and attempt < self._max_http_retries:
                        _sleep(_backoff_seconds(attempt))
                        attempt += 1
                        continue
                    raise
                if response.status_code == 429:
                    delay = parse_retry_after(response.headers.get("retry-after"))
                    if delay is not None and delay > _MAX_RETRY_AFTER_SECONDS:
                        raise RateLimitError(
                            f"Rate limit exceeded (HTTP 429) for {method} {url}; "
                            f"server asked for {delay:.0f}s, above the "
                            f"{_MAX_RETRY_AFTER_SECONDS:.0f}s cap."
                        )
                    if attempt < self._max_http_retries:
                        _sleep(
                            delay if delay is not None else _backoff_seconds(attempt)
                        )
                        attempt += 1
                        continue
                    raise RateLimitError(
                        f"Rate limit exceeded (HTTP 429) for {method} {url}: "
                        f"{response.text}"
                    )
                if (
                    response.status_code in self._retry_statuses
                    and idempotent
                    and attempt < self._max_http_retries
                ):
                    logger.debug(
                        "retrying %s %s after HTTP %s (attempt %d/%d)",
                        method,
                        url,
                        response.status_code,
                        attempt + 1,
                        self._max_http_retries,
                    )
                    _sleep(_backoff_seconds(attempt))
                    attempt += 1
                    continue
                response.raise_for_status()
                return response

    @staticmethod
    def _json_or_text_response(response: httpx.Response) -> Any:
        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type or "+json" in content_type:
            return response.json()
        return response.text

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._json_or_text_response(self._request("GET", path, params=params))

    def _get_text(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        return self._request("GET", path, params=params, headers=headers).text

    def _get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        return self._request("GET", path, params=params).content

    def _post(self, path: str, json: Any) -> Any:
        response = self._request("POST", path, json=json)
        return response.json() if response.content else None

    def _patch(self, path: str, json: Any) -> Any:
        response = self._request("PATCH", path, json=json)
        return response.json() if response.content else None

    def _put(self, path: str, json: Any) -> Any:
        response = self._request("PUT", path, json=json)
        return response.json() if response.content else None

    def _delete(self, path: str) -> None:
        self._request("DELETE", path)

    def _get_pr_info_from_path(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        path: str,
        logger: logging.Logger,
        description_key: str = "body",
    ) -> PRInfo | None:
        try:
            data = self._get(path)
            return pr_info_from_api_dict(data, description_key) if isinstance(data, dict) else None
        except Exception as e:
            _log_pr_info_warning(logger, owner, repo, pr_number, e)
            return None

    def _patch_pr_description(
        self,
        *,
        path: str,
        description: str,
        title: str | None = None,
        description_key: str = "body",
    ) -> None:
        payload: dict[str, str] = {description_key: description}
        if title is not None:
            payload["title"] = title
        self._patch(path, payload)

    @staticmethod
    def _sha_guard_passes(base_sha: str, head_sha: str) -> bool:
        base = (base_sha or "").strip()
        head = (head_sha or "").strip()
        return bool(base and head and base != head)

    def _get_incremental_pr_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        return super().get_incremental_pr_diff(owner, repo, pr_number, base_sha, head_sha)

    def get_incremental_pr_diff(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> str:
        if not self._sha_guard_passes(base_sha, head_sha):
            return self.get_pr_diff(owner, repo, pr_number)
        return self._get_incremental_pr_diff(owner, repo, pr_number, base_sha, head_sha)

    def _get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        return super().get_incremental_pr_files(owner, repo, pr_number, base_sha, head_sha)

    def get_incremental_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        base_sha: str,
        head_sha: str,
    ) -> list[FileInfo]:
        if not self._sha_guard_passes(base_sha, head_sha):
            return self.get_pr_files(owner, repo, pr_number)
        return self._get_incremental_pr_files(owner, repo, pr_number, base_sha, head_sha)

    @staticmethod
    def _next_page_url(data: Any, _current: PageToken = None) -> str | None:
        if not isinstance(data, dict):
            return None
        nxt = data.get("next")
        return nxt.strip() if isinstance(nxt, str) and nxt.strip() else None

    @staticmethod
    def _init_pagination_state(
        path: str,
        params: dict[str, Any] | None,
        mode: PaginationMode,
        page_size: int | None,
    ) -> _PaginationState:
        current_params = dict(params or {})
        if mode == "page":
            token = int(current_params.get("page", 1) or 1)
            current_params["page"] = token
            if page_size is not None:
                current_params.setdefault("per_page", page_size)
            return _PaginationState(path=path, params=current_params, token=token)
        if mode == "start":
            token = int(current_params.get("start", 0) or 0)
            current_params["start"] = token
            if page_size is not None:
                current_params.setdefault("limit", page_size)
            return _PaginationState(path=path, params=current_params, token=token)
        return _PaginationState(path=path, params=current_params, token=path)

    @staticmethod
    def _stop_on_repeat(token: PageToken, on_repeat: RepeatHook | None) -> bool:
        if on_repeat is not None:
            on_repeat(token)
        return False

    def _load_paginated_data(
        self,
        fetch: FetchPage,
        state: _PaginationState,
        mode: PaginationMode,
        on_repeat: RepeatHook | None,
    ) -> tuple[Any, bool]:
        if mode != "next":
            return fetch(state.path, dict(state.params)), True

        token_str = str(state.token)
        if token_str in state.seen_tokens:
            return None, self._stop_on_repeat(state.token, on_repeat)

        state.seen_tokens.add(token_str)
        params_arg = dict(state.params) if state.params else None
        data = fetch(state.path, params_arg)
        state.params = {}
        return data, True

    @staticmethod
    def _default_page_next_token(
        data: Any,
        current_token: PageToken,
        page_size: int | None,
    ) -> int | None:
        if not isinstance(data, list) or page_size is None or len(data) < page_size:
            return None
        return int(current_token) + 1

    def _advance_page_mode(
        self,
        state: _PaginationState,
        data: Any,
        page_size: int | None,
        next_page: NextPage | None,
        on_repeat: RepeatHook | None,
    ) -> bool:
        next_token = (
            next_page(data, state.token)
            if next_page is not None
            else self._default_page_next_token(data, state.token, page_size)
        )
        if next_token is None:
            return False
        if next_token == state.token:
            return self._stop_on_repeat(state.token, on_repeat)
        state.token = next_token
        state.params["page"] = next_token
        return True

    def _advance_start_mode(
        self,
        state: _PaginationState,
        data: Any,
        next_page: NextPage | None,
        on_repeat: RepeatHook | None,
    ) -> bool:
        if next_page is None:
            raise ValueError("start pagination requires a next_page callback")
        next_token = next_page(data, state.token)
        if next_token is None:
            return False
        if next_token == state.token:
            return self._stop_on_repeat(state.token, on_repeat)
        state.token = next_token
        state.params["start"] = next_token
        return True

    def _advance_next_mode(
        self,
        state: _PaginationState,
        data: Any,
        next_page: NextPage | None,
    ) -> bool:
        next_token = (
            next_page(data, state.token)
            if next_page is not None
            else self._next_page_url(data)
        )
        if next_token is None:
            return False
        next_url = str(next_token).strip()
        if not next_url:
            return False
        state.path = next_url
        state.token = next_url
        return True

    def _advance_pagination(
        self,
        state: _PaginationState,
        data: Any,
        *,
        mode: PaginationMode,
        page_size: int | None,
        next_page: NextPage | None,
        on_repeat: RepeatHook | None,
    ) -> bool:
        if mode == "page":
            return self._advance_page_mode(state, data, page_size, next_page, on_repeat)
        if mode == "start":
            return self._advance_start_mode(state, data, next_page, on_repeat)
        return self._advance_next_mode(state, data, next_page)

    def _paginate_list(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_pages: int = 500,
        page_size: int | None = None,
        mode: PaginationMode = "next",
        initial_data: Any = None,
        fetch_page: FetchPage | None = None,
        next_page: NextPage | None = None,
        on_repeat: RepeatHook | None = None,
    ) -> Iterator[Any]:
        """Yield paginated API pages while centralizing page-token progression."""

        fetch = fetch_page or self._get
        state = self._init_pagination_state(path, params, mode, page_size)
        data = initial_data
        for _ in range(max_pages):
            if data is None:
                data, should_continue = self._load_paginated_data(fetch, state, mode, on_repeat)
                if not should_continue:
                    break

            yield data

            if not self._advance_pagination(
                state,
                data,
                mode=mode,
                page_size=page_size,
                next_page=next_page,
                on_repeat=on_repeat,
            ):
                break

            data = None
