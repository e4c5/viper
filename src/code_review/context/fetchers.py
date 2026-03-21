"""Fetch normalized text from GitHub Issues, Jira, and Confluence."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from code_review.context.errors import ContextAwareAuthError, ContextAwareFatalError
from code_review.context.types import ContextReference, ReferenceType

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
    jira_email: str
    jira_token: str
    confluence_base: str
    confluence_email: str
    confluence_token: str
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


def fetch_github_issue(
    api_base: str,
    token: str,
    owner: str,
    repo: str,
    issue_number: str,
    timeout: float = 30.0,
) -> FetchedDocument | None:
    path = f"{api_base.rstrip('/')}/repos/{owner}/{repo}/issues/{issue_number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.get(path, headers=headers)
        if r.status_code == 404:
            logger.warning("GitHub issue not found: %s/%s#%s", owner, repo, issue_number)
            return None
        if r.status_code != 200:
            _raise_auth("GET", path, r.status_code, r.text)
            raise ContextAwareFatalError(f"GitHub issue fetch failed ({r.status_code}): {path}")
        data = r.json()
    labels = [
        (lb.get("name") if isinstance(lb, dict) else str(lb))
        for lb in (data.get("labels") or [])
    ]
    meta = {
        "state": data.get("state"),
        "labels": labels,
        "html_url": data.get("html_url"),
    }
    title = (data.get("title") or "").strip()
    body_raw = (data.get("body") or "").strip()
    updated = data.get("updated_at")
    lines = [f"Title: {title}", f"State: {meta.get('state')}", f"Labels: {', '.join(labels)}"]
    _append_body(lines, body_raw)
    return FetchedDocument(
        external_id=f"{owner}/{repo}#{issue_number}",
        title=title,
        body="\n".join(lines),
        metadata=meta,
        version=str(data.get("id", "")),
        external_updated_at=updated if isinstance(updated, str) else None,
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
        data = r.json()
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


def fetch_jira_issue(
    base_url: str,
    email: str,
    api_token: str,
    key: str,
    timeout: float = 30.0,
    extra_fields: list[str] | None = None,
) -> FetchedDocument | None:
    root = base_url.rstrip("/")
    path = f"{root}/rest/api/3/issue/{key}"
    base_fields = ["summary", "description", "issuetype", "status", "updated"]
    extra = [f.strip() for f in (extra_fields or []) if f.strip()]
    fields_param = ",".join(base_fields + extra)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(path, params={"fields": fields_param}, auth=(email, api_token))
        if r.status_code == 404:
            logger.warning("Jira issue not found: %s", key)
            return None
        if r.status_code != 200:
            _raise_auth("GET", path, r.status_code, r.text)
            raise ContextAwareFatalError(f"Jira fetch failed ({r.status_code}): {path}")
        data = r.json()
    fields_d = data.get("fields") or {}
    summary = (fields_d.get("summary") or "").strip()
    desc = fields_d.get("description")
    desc_text = ""
    if isinstance(desc, str):
        desc_text = desc
    elif isinstance(desc, dict):
        desc_text = _adf_to_plain(desc)
    it = fields_d.get("issuetype") or {}
    st = fields_d.get("status") or {}
    issue_type = it.get("name", "") if isinstance(it, dict) else str(it)
    status_name = st.get("name", "") if isinstance(st, dict) else str(st)
    updated = fields_d.get("updated")
    lines = [
        f"Key: {key}",
        f"Summary: {summary}",
        f"Issue type: {issue_type}",
        f"Status: {status_name}",
    ]
    if desc_text:
        lines.append("Description:")
        lines.append(desc_text)
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
    meta = {"issuetype": issue_type, "status": status_name}
    return FetchedDocument(
        external_id=key.upper(),
        title=summary,
        body="\n".join(lines),
        metadata=meta,
        version=str(data.get("id", "")),
        external_updated_at=updated if isinstance(updated, str) else None,
    )


def _adf_to_plain(node: Any) -> str:
    """Minimal Atlassian Document Format → plain text (paragraphs, text nodes)."""
    if not isinstance(node, dict):
        return ""
    parts: list[str] = []
    ntype = node.get("type")
    if ntype == "text":
        t = node.get("text")
        if t:
            parts.append(str(t))
    for child in node.get("content") or []:
        parts.append(_adf_to_plain(child))
    if ntype in ("paragraph", "heading", "listItem", "doc"):
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
        data = r.json()
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
                cfg.jira_email,
                cfg.jira_token,
                ref.external_id,
                extra_fields=list(cfg.jira_extra_fields),
            )
        if ref.ref_type == ReferenceType.CONFLUENCE and cfg.ctx_confluence_enabled:
            return fetch_confluence_page(
                cfg.confluence_base, cfg.confluence_email, cfg.confluence_token, ref.external_id
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
