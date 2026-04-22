"""Fetch normalized text from GitHub Issues, Jira, and Confluence."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from github.GithubException import GithubException, UnknownObjectException

from code_review.context.errors import ContextAwareAuthError, ContextAwareFatalError
from code_review.context.types import ContextReference, ReferenceType
from code_review.github_client import GitHubApiClient

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
BODY_LABEL = "Body:"


def _strip_html_to_text(raw: str) -> str:
    """Best-effort HTML to plain text for Confluence/Jira ADF-ish bodies."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub("\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


@dataclass
class FetchedDocument:
    external_id: str
    title: str
    body: str
    metadata: dict[str, Any]
    version: str | None
    external_updated_at: str | None  # ISO8601 from API when available


@dataclass(frozen=True)
class FetchReferenceConfig:
    github_api_base: str
    github_token: str
    gitlab_api_base: str
    gitlab_token: str
    jira_base: str
    confluence_base: str
    atlassian_email: str
    atlassian_token: str
    ctx_github_enabled: bool
    ctx_gitlab_enabled: bool
    ctx_jira_enabled: bool
    ctx_confluence_enabled: bool
    jira_extra_fields: tuple[str, ...] = ()


def _append_body(lines: list[str], body: str, label: str = BODY_LABEL) -> None:
    if body:
        lines.append(label)
        lines.append(body)


def _raise_auth(method: str, url: str, status: int, body: str) -> None:
    if status in (401, 403):
        snippet = body[:200]
        raise ContextAwareAuthError(
            f"Context fetch auth failed ({status}) for {method} {url}: {snippet}"
        )


def _response_preview(response: httpx.Response) -> str:
    text = (response.text or "").strip().replace("\n", "\\n")
    return text[:300] if text else "<empty body>"


def _json_or_context_error(response: httpx.Response, *, source: str, url: str) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        content_type = response.headers.get("content-type", "<missing>")
        raise ContextAwareFatalError(
            f"{source} returned non-JSON response ({response.status_code}, "
            f"content-type={content_type}) for {url}: {_response_preview(response)}"
        ) from exc


def _build_github_api_client(api_base: str, token: str, timeout: float) -> GitHubApiClient:
    return GitHubApiClient(api_base, token, timeout=timeout)


def _github_issue_updated_at(issue: Any) -> str | None:
    updated = getattr(issue, "updated_at", None)
    if isinstance(updated, str):
        return updated
    if isinstance(updated, datetime):
        return updated.isoformat().replace("+00:00", "Z")
    return None


def _github_exception_body(exc: GithubException) -> str:
    data = exc.data
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message:
            return message
    return str(exc)


def fetch_github_issue(
    api_base: str,
    token: str,
    owner: str,
    repo: str,
    issue_number: str,
    timeout: float = 30.0,
) -> FetchedDocument | None:
    path = f"{api_base.rstrip('/')}/repos/{owner}/{repo}/issues/{issue_number}"
    client = _build_github_api_client(api_base, token, timeout)
    try:
        issue = client.get_issue(owner, repo, issue_number)
    except UnknownObjectException:
        logger.warning("GitHub issue not found: %s/%s#%s", owner, repo, issue_number)
        return None
    except GithubException as exc:
        status = exc.status or 0
        body = _github_exception_body(exc)
        _raise_auth("GET", path, status, body)
        raise ContextAwareFatalError(f"GitHub issue fetch failed ({status}): {path}") from exc

    labels = [
        str(getattr(label, "name", label) or "") for label in (getattr(issue, "labels", None) or [])
    ]
    labels = [label for label in labels if label]
    meta = {
        "state": getattr(issue, "state", None),
        "labels": labels,
        "html_url": getattr(issue, "html_url", None),
    }
    title = str(getattr(issue, "title", "") or "").strip()
    body_raw = str(getattr(issue, "body", "") or "").strip()
    lines = [f"Title: {title}", f"State: {meta.get('state')}", f"Labels: {', '.join(labels)}"]
    _append_body(lines, body_raw)
    return FetchedDocument(
        external_id=f"{owner}/{repo}#{issue_number}",
        title=title,
        body="\n".join(lines),
        metadata=meta,
        version=str(getattr(issue, "id", "") or ""),
        external_updated_at=_github_issue_updated_at(issue),
    )


def _parse_github_issue_ref(ref: ContextReference) -> tuple[str, str, str]:
    if ref.ref_type != ReferenceType.GITHUB_ISSUE:
        raise ValueError("not a github issue ref")
    parts = ref.external_id.split("#", 1)
    if len(parts) != 2:
        raise ValueError(f"bad github ref {ref.external_id!r}")
    repo_part, num = parts
    owner, repo = repo_part.split("/", 1)
    return owner, repo, num


def _parse_gitlab_issue_ref(ref: ContextReference) -> tuple[str, str]:
    if ref.ref_type != ReferenceType.GITLAB_ISSUE:
        raise ValueError("not a gitlab issue ref")
    parts = ref.external_id.split("#", 1)
    if len(parts) != 2:
        raise ValueError(f"bad gitlab ref {ref.external_id!r}")
    project_path, issue_iid = parts
    return project_path, issue_iid


def fetch_gitlab_issue(
    api_base: str,
    token: str,
    project_path: str,
    issue_iid: str,
    timeout: float = 30.0,
) -> FetchedDocument | None:
    root = api_base.rstrip("/")
    proj = quote(project_path, safe="")
    path = f"{root}/projects/{proj}/issues/{issue_iid}"
    headers = {"PRIVATE-TOKEN": token} if token else {}
    with httpx.Client(timeout=timeout) as client:
        r = client.get(path, headers=headers)
        if r.status_code == 404:
            logger.warning("GitLab issue not found: %s#%s", project_path, issue_iid)
            return None
        if r.status_code != 200:
            _raise_auth("GET", path, r.status_code, r.text)
            raise ContextAwareFatalError(f"GitLab issue fetch failed ({r.status_code}): {path}")
        data = _json_or_context_error(r, source="GitLab issue", url=path)
    labels = [str(lb) for lb in (data.get("labels") or [])]
    title = (data.get("title") or "").strip()
    body_raw = (data.get("description") or "").strip()
    state = (data.get("state") or "").strip()
    updated = data.get("updated_at")
    lines = [f"Title: {title}", f"State: {state}", f"Labels: {', '.join(labels)}"]
    _append_body(lines, body_raw)
    return FetchedDocument(
        external_id=f"{project_path}#{issue_iid}",
        title=title,
        body="\n".join(lines),
        metadata={"state": state, "labels": labels, "web_url": data.get("web_url")},
        version=str(data.get("id", "")),
        external_updated_at=updated if isinstance(updated, str) else None,
    )


def _jira_description_text(desc: Any) -> str:
    if isinstance(desc, str):
        return desc
    if isinstance(desc, dict):
        return _adf_to_plain(desc)
    return ""


def _jira_field_name(field: Any) -> str:
    return field.get("name", "") if isinstance(field, dict) else str(field)


def _jira_extra_field_lines(fields_d: dict, extra: list[str]) -> list[str]:
    lines: list[str] = []
    for field_name in extra:
        value = fields_d.get(field_name)
        if value is None:
            continue
        if isinstance(value, dict):
            value = _adf_to_plain(value) or str(value)
        elif not isinstance(value, str):
            value = str(value)
        if value.strip():
            lines.append(f"{field_name}:")
            lines.append(value.strip())
    return lines


def _jira_remote_link_lines(remote_links: Any) -> list[str]:
    if not isinstance(remote_links, list):
        return []
    lines: list[str] = []
    for item in remote_links:
        if not isinstance(item, dict):
            continue
        obj = item.get("object")
        if not isinstance(obj, dict):
            continue
        url = str(obj.get("url") or "").strip()
        if not url:
            continue
        title = str(obj.get("title") or "").strip()
        lines.append(f"- {title}: {url}" if title else f"- {url}")
    return lines


def _fetch_jira_remote_links(
    client: httpx.Client,
    *,
    root: str,
    email: str,
    api_token: str,
    key: str,
) -> tuple[list[str], bool]:
    path = f"{root}/rest/api/3/issue/{key}/remotelink"
    r = client.get(path, auth=(email, api_token))
    if r.status_code != 200:
        logger.warning("Jira remote links fetch failed (%s): %s", r.status_code, path)
        return ([], False)
    try:
        lines = _jira_remote_link_lines(
            _json_or_context_error(r, source="Jira remote links", url=path)
        )
    except ContextAwareFatalError as exc:
        logger.warning("Skipping Jira remote links for %s: %s", key, exc)
        return ([], False)
    return (lines, True)


def fetch_jira_issue(
    base_url: str,
    email: str,
    api_token: str,
    key: str,
    timeout: float = 30.0,
    extra_fields: list[str] | None = None,
    include_remote_links: bool = False,
) -> FetchedDocument | None:
    root = base_url.rstrip("/")
    path = f"{root}/rest/api/3/issue/{key}"
    extra = [f.strip() for f in (extra_fields or []) if f.strip()]
    fields_param = ",".join(["summary", "description", "issuetype", "status", "updated"] + extra)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(path, params={"fields": fields_param}, auth=(email, api_token))
        if r.status_code == 400 and extra:
            logger.warning("Invalid Jira extra fields for %s; retrying with base fields only", key)
            extra = []
            base_param = ",".join(["summary", "description", "issuetype", "status", "updated"])
            r = client.get(path, params={"fields": base_param}, auth=(email, api_token))
        if r.status_code == 404:
            logger.warning("Jira issue not found: %s", key)
            return None
        if r.status_code != 200:
            _raise_auth("GET", path, r.status_code, r.text)
            raise ContextAwareFatalError(f"Jira fetch failed ({r.status_code}): {path}")
        data = _json_or_context_error(r, source="Jira issue", url=path)
        remote_link_lines, remote_links_included = (
            _fetch_jira_remote_links(
                client,
                root=root,
                email=email,
                api_token=api_token,
                key=key,
            )
            if include_remote_links
            else ([], False)
        )
    fields_d = data.get("fields") or {}
    summary = (fields_d.get("summary") or "").strip()
    issue_type = _jira_field_name(fields_d.get("issuetype") or {})
    status_name = _jira_field_name(fields_d.get("status") or {})
    updated = fields_d.get("updated")
    lines = [
        f"Key: {key}",
        f"Summary: {summary}",
        f"Issue type: {issue_type}",
        f"Status: {status_name}",
    ]
    desc_text = _jira_description_text(fields_d.get("description"))
    if desc_text:
        lines.append("Description:")
        lines.append(desc_text)
    lines.extend(_jira_extra_field_lines(fields_d, extra))
    if remote_link_lines:
        lines.append("Remote links:")
        lines.extend(remote_link_lines)
    return FetchedDocument(
        external_id=key.upper(),
        title=summary,
        body="\n".join(lines),
        metadata={
            "issuetype": issue_type,
            "status": status_name,
            "jira_remote_links_included": remote_links_included,
            "jira_remote_link_count": len(remote_link_lines),
        },
        version=str(data.get("id", "")),
        external_updated_at=updated if isinstance(updated, str) else None,
    )


def _extract_url_from_attrs(attrs: Any, *keys: str) -> list[str]:
    if not isinstance(attrs, dict):
        return []
    return [v.strip() for k in keys for v in [attrs.get(k) or ""] if v.strip()]


def _adf_link_urls(node: dict[str, Any]) -> list[str]:
    urls: list[str] = _extract_url_from_attrs(node.get("attrs"), "url", "href")
    for mark in node.get("marks") or []:
        if isinstance(mark, dict):
            urls.extend(_extract_url_from_attrs(mark.get("attrs"), "href"))
    return list(dict.fromkeys(urls))


def _adf_to_plain(node: Any) -> str:
    """Minimal Atlassian Document Format -> plain text, preserving link URLs."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []
    ntype = node.get("type")
    if ntype == "text":
        t = node.get("text")
        if t:
            text = str(t)
            urls = [url for url in _adf_link_urls(node) if url not in text]
            parts.append(" ".join([text, *urls]))
    if ntype in ("inlineCard", "blockCard", "embedCard"):
        urls = _adf_link_urls(node)
        if urls:
            parts.extend(urls)
    for child in node.get("content") or []:
        parts.append(_adf_to_plain(child))
    if ntype in ("paragraph", "heading", "listItem", "doc", "blockCard", "embedCard"):
        inner = "".join(parts).strip()
        if inner:
            return inner + "\n"
    return "".join(parts)


def fetch_confluence_page(
    base_url: str,
    email: str,
    api_token: str,
    page_id: str,
    timeout: float = 30.0,
) -> FetchedDocument | None:
    root = base_url.rstrip("/")
    # Cloud: https://tenant.atlassian.net → .../wiki/rest/api; already-/wiki base → .../rest/api
    api_root = f"{root}/rest/api" if root.rstrip("/").endswith("/wiki") else f"{root}/wiki/rest/api"
    path = f"{api_root}/content/{page_id}"
    params = {"expand": "body.storage,version,history.lastUpdated"}
    with httpx.Client(timeout=timeout) as client:
        r = client.get(path, params=params, auth=(email, api_token))
        if r.status_code == 404:
            logger.warning("Confluence page not found: %s", page_id)
            return None
        if r.status_code != 200:
            _raise_auth("GET", path, r.status_code, r.text)
            raise ContextAwareFatalError(f"Confluence fetch failed ({r.status_code}): {path}")
        data = _json_or_context_error(r, source="Confluence page", url=path)
    title = (data.get("title") or "").strip()
    body_storage = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
    plain = _strip_html_to_text(body_storage)
    ver = data.get("version") or {}
    version_num = str(ver.get("number", "")) if isinstance(ver, dict) else ""
    hist = data.get("history") or {}
    last_up = (hist.get("lastUpdated") or {}).get("when") if isinstance(hist, dict) else None
    lines = [f"Title: {title}"]
    _append_body(lines, plain)
    meta = {"type": data.get("type"), "status": data.get("status")}
    return FetchedDocument(
        external_id=page_id,
        title=title,
        body="\n".join(lines),
        metadata=meta,
        version=version_num or None,
        external_updated_at=last_up if isinstance(last_up, str) else None,
    )


def fetch_reference(
    ref: ContextReference,
    *,
    cfg: FetchReferenceConfig,
) -> FetchedDocument | None:
    """Fetch a single reference; returns None if that source is disabled."""
    try:
        if ref.ref_type == ReferenceType.GITHUB_ISSUE and cfg.ctx_github_enabled:
            o, r, n = _parse_github_issue_ref(ref)
            return fetch_github_issue(cfg.github_api_base, cfg.github_token, o, r, n)
        if ref.ref_type == ReferenceType.GITLAB_ISSUE and cfg.ctx_gitlab_enabled:
            proj, iid = _parse_gitlab_issue_ref(ref)
            return fetch_gitlab_issue(cfg.gitlab_api_base, cfg.gitlab_token, proj, iid)
        if ref.ref_type == ReferenceType.JIRA and cfg.ctx_jira_enabled:
            return fetch_jira_issue(
                cfg.jira_base,
                cfg.atlassian_email,
                cfg.atlassian_token,
                ref.external_id,
                extra_fields=list(cfg.jira_extra_fields),
                include_remote_links=cfg.ctx_confluence_enabled,
            )
        if ref.ref_type == ReferenceType.CONFLUENCE and cfg.ctx_confluence_enabled:
            return fetch_confluence_page(
                cfg.confluence_base, cfg.atlassian_email, cfg.atlassian_token, ref.external_id
            )
    except ContextAwareAuthError:
        # Auth/credential failures (401/403) are always fatal – re-raise so the runner
        # surfaces a clear misconfiguration message to the operator.
        raise
    except ContextAwareFatalError as e:
        # Non-auth fatal errors (e.g. unexpected HTTP status) are downgraded to a
        # warning so one unavailable external reference doesn't abort the whole review.
        logger.warning("Skipping context reference %s: %s", ref.display, e)
        return None
    except httpx.HTTPError as e:
        logger.warning("Context fetch HTTP error for %s (skipping): %s", ref.display, e)
        return None
    except Exception as e:
        logger.warning("Context fetch failed for %s (skipping): %s", ref.display, e)
        return None
    return None
