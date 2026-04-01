"""Shared HTTP provider helpers for SCM adapters backed by httpx."""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from code_review.providers.base import (
    FileInfo,
    PRInfo,
    ProviderInterface,
    _log_pr_info_warning,
    pr_info_from_api_dict,
)

PaginationMode = Literal["page", "next", "start"]
PageToken = str | int | None
FetchPage = Callable[[str, dict[str, Any] | None], Any]
NextPage = Callable[[Any, PageToken], PageToken]
RepeatHook = Callable[[PageToken], None]


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
            return path
        return f"{self._base_url}{self._api_prefix()}{path}"

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
        with self._httpx_module.Client(timeout=self._timeout) as client:
            response = getattr(client, method.lower())(self._build_url(path), **request_kwargs)
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
