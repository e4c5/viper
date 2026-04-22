"""Resolve references, cache in PostgreSQL, distill (direct or RAG)."""

from __future__ import annotations

import logging

from code_review.config import ContextAwareReviewConfig, SCMConfig
from code_review.context.distiller import distill_context_text
from code_review.context.errors import ContextAwareFatalError
from code_review.context.extract import extract_confluence_refs
from code_review.context.fetchers import FetchReferenceConfig, fetch_reference
from code_review.context.rag import (
    build_semantic_query_from_diff,
    chunk_plain_text,
    embed_query_text,
    embed_texts,
)
from code_review.context.store import ContextStore
from code_review.context.types import ContextReference, ExternalCredentials, ReferenceType

logger = logging.getLogger(__name__)

# Module-level ContextStore cache keyed by (db_url, embedding_dimensions).
# Avoids re-running schema DDL on every review call.
_store_cache: dict[tuple[str, int], ContextStore] = {}
_MAX_TRANSITIVE_CONFLUENCE_REFS = 20


def _clamp_context_text(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "\n…(truncated)"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker


def _github_api_and_token(scm: SCMConfig, ctx: ContextAwareReviewConfig) -> tuple[str, str]:
    if scm.provider == "github":
        tok = scm.token
        token = tok.get_secret_value() if hasattr(tok, "get_secret_value") else str(tok)
        return scm.url.rstrip("/"), token
    tok = ctx.github_token.get_secret_value() if ctx.github_token else ""
    base = (ctx.github_api_url or "https://api.github.com").rstrip("/")
    return base, tok


def _gitlab_api_and_token(scm: SCMConfig, ctx: ContextAwareReviewConfig) -> tuple[str, str]:
    if scm.provider == "gitlab":
        tok = scm.token
        token = tok.get_secret_value() if hasattr(tok, "get_secret_value") else str(tok)
        return scm.url.rstrip("/"), token
    tok = ctx.gitlab_token.get_secret_value() if ctx.gitlab_token else ""
    base = (ctx.gitlab_api_url or "").rstrip("/")
    return base, tok


def _ref_applicable(ref: ContextReference, ctx: ContextAwareReviewConfig) -> bool:
    if ref.ref_type == ReferenceType.GITHUB_ISSUE:
        return ctx.github_issues_enabled
    if ref.ref_type == ReferenceType.GITLAB_ISSUE:
        return ctx.gitlab_issues_enabled
    if ref.ref_type == ReferenceType.JIRA:
        return ctx.jira_enabled
    if ref.ref_type == ReferenceType.CONFLUENCE:
        return ctx.confluence_enabled
    return False


def _source_name_and_base(
    ref: ContextReference,
    ctx: ContextAwareReviewConfig,
    scm: SCMConfig,
) -> tuple[str, str]:
    if ref.ref_type == ReferenceType.GITHUB_ISSUE:
        api, _ = _github_api_and_token(scm, ctx)
        return ("github", api)
    if ref.ref_type == ReferenceType.GITLAB_ISSUE:
        api, _ = _gitlab_api_and_token(scm, ctx)
        return ("gitlab", api)
    if ref.ref_type == ReferenceType.JIRA:
        return ("jira", ctx.atlassian_url)
    if ref.ref_type == ReferenceType.CONFLUENCE:
        return ("confluence", ctx.atlassian_url)
    return ("unknown", "")


def _get_external_credentials(scm: SCMConfig, ctx: ContextAwareReviewConfig) -> ExternalCredentials:
    gh_api, gh_tok = _github_api_and_token(scm, ctx)
    gl_api, gl_tok = _gitlab_api_and_token(scm, ctx)
    atlassian_tok = ctx.atlassian_token.get_secret_value() if ctx.atlassian_token else ""
    return ExternalCredentials(
        github_api=gh_api,
        github_token=gh_tok,
        gitlab_api=gl_api,
        gitlab_token=gl_tok,
        atlassian_email=ctx.atlassian_email.strip(),
        atlassian_token=atlassian_tok,
    )


def _build_fetch_reference_config(
    *,
    ctx: ContextAwareReviewConfig,
    creds: ExternalCredentials,
) -> FetchReferenceConfig:
    extra_fields = tuple(f.strip() for f in (ctx.jira_extra_fields or "").split(",") if f.strip())
    return FetchReferenceConfig(
        github_api_base=creds.github_api,
        github_token=creds.github_token,
        gitlab_api_base=creds.gitlab_api,
        gitlab_token=creds.gitlab_token,
        jira_base=ctx.atlassian_url,
        confluence_base=ctx.atlassian_url,
        atlassian_email=creds.atlassian_email,
        atlassian_token=creds.atlassian_token,
        ctx_github_enabled=ctx.github_issues_enabled,
        ctx_gitlab_enabled=ctx.gitlab_issues_enabled,
        ctx_jira_enabled=ctx.jira_enabled,
        ctx_confluence_enabled=ctx.confluence_enabled,
        jira_extra_fields=extra_fields,
    )


def _extract_transitive_confluence_refs(
    fetched_body: str,
    *,
    ctx: ContextAwareReviewConfig,
    seen_ids: set[str],
) -> list[ContextReference]:
    """If Confluence is enabled, extract Confluence page refs from fetched text."""
    if not ctx.confluence_enabled:
        return []
    if not fetched_body.strip():
        logger.warning("Fetched body is empty or whitespace-only.")
        return []
    refs = extract_confluence_refs(fetched_body, exclude_ids=seen_ids)
    if len(refs) > _MAX_TRANSITIVE_CONFLUENCE_REFS:
        logger.info(
            "context_aware: limiting transitive Confluence links from Jira to %s of %s",
            _MAX_TRANSITIVE_CONFLUENCE_REFS,
            len(refs),
        )
    return refs[:_MAX_TRANSITIVE_CONFLUENCE_REFS]


def _confluence_seen_ids(
    seen_refs: set[tuple[ReferenceType, str]],
) -> set[str]:
    return {
        external_id for ref_type, external_id in seen_refs if ref_type == ReferenceType.CONFLUENCE
    }


def _load_context_documents_without_store(
    *,
    ctx: ContextAwareReviewConfig,
    applicable: list[ContextReference],
    creds: ExternalCredentials,
) -> list[tuple[str, str]]:
    docs_for_distill: list[tuple[str, str]] = []
    fetch_cfg = _build_fetch_reference_config(ctx=ctx, creds=creds)
    seen_refs = {(r.ref_type, r.external_id) for r in applicable}
    # Two-pass: first fetch the original refs, then any transitive Confluence refs.
    transitive: list[ContextReference] = []
    for ref in applicable:
        fetched = fetch_reference(ref, cfg=fetch_cfg)
        if fetched is None:
            continue
        docs_for_distill.append((ref.display, fetched.body))
        if ref.ref_type == ReferenceType.JIRA:
            transitive.extend(
                _extract_transitive_confluence_refs(
                    fetched.body,
                    ctx=ctx,
                    seen_ids=_confluence_seen_ids(seen_refs),
                )
            )
    for ref in transitive:
        ref_key = (ref.ref_type, ref.external_id)
        if ref_key in seen_refs:
            continue
        seen_refs.add(ref_key)
        logger.info("context_aware: following transitive Confluence link %s from Jira", ref.display)
        fetched = fetch_reference(ref, cfg=fetch_cfg)
        if fetched is None:
            continue
        docs_for_distill.append((ref.display, fetched.body))
    return docs_for_distill


def _load_or_fetch_document_content(
    *,
    store: ContextStore,
    conn,
    source_id,
    ref: ContextReference,
    fetch_cfg: FetchReferenceConfig,
) -> tuple[str, object] | None:
    row = store.load_document(conn, source_id, ref.external_id)
    if row is not None and row[3]:
        logger.debug("context cache hit %s", ref.display)
        return (row[1], row[0])

    fetched = fetch_reference(ref, cfg=fetch_cfg)
    if fetched is None:
        return None
    doc_id = store.upsert_document(conn, source_id, fetched)
    return (fetched.body, doc_id)


def _load_context_documents(
    *,
    store: ContextStore,
    conn,
    scm: SCMConfig,
    ctx: ContextAwareReviewConfig,
    applicable: list[ContextReference],
    creds: ExternalCredentials,
) -> tuple[list[tuple[str, str]], list[tuple[str, object]]]:
    docs_for_distill: list[tuple[str, str]] = []
    doc_ids_for_rag: list[tuple[str, object]] = []
    fetch_cfg = _build_fetch_reference_config(ctx=ctx, creds=creds)
    seen_refs = {(r.ref_type, r.external_id) for r in applicable}
    transitive: list[ContextReference] = []

    def _fetch_and_record(ref: ContextReference) -> str | None:
        src_name, base = _source_name_and_base(ref, ctx, scm)
        if not base and ref.ref_type != ReferenceType.GITHUB_ISSUE:
            return None
        source_id = store.get_or_create_source(conn, src_name, base)
        loaded = _load_or_fetch_document_content(
            store=store,
            conn=conn,
            source_id=source_id,
            ref=ref,
            fetch_cfg=fetch_cfg,
        )
        if loaded is None:
            return None
        content, doc_id = loaded
        docs_for_distill.append((ref.display, content))
        doc_ids_for_rag.append((ref.display, doc_id))
        return content

    for ref in applicable:
        content = _fetch_and_record(ref)
        if content is not None and ref.ref_type == ReferenceType.JIRA:
            transitive.extend(
                _extract_transitive_confluence_refs(
                    content,
                    ctx=ctx,
                    seen_ids=_confluence_seen_ids(seen_refs),
                )
            )

    for ref in transitive:
        ref_key = (ref.ref_type, ref.external_id)
        if ref_key in seen_refs:
            continue
        seen_refs.add(ref_key)
        logger.info("context_aware: following transitive Confluence link %s from Jira", ref.display)
        _fetch_and_record(ref)

    return (docs_for_distill, doc_ids_for_rag)


def _build_retrieved_context_text(
    *,
    store: ContextStore,
    conn,
    ctx: ContextAwareReviewConfig,
    full_diff: str,
    docs_for_distill: list[tuple[str, str]],
    doc_ids_for_rag: list[tuple[str, object]],
    combined: str,
) -> str:
    try:
        q_emb = embed_query_text(
            build_semantic_query_from_diff(full_diff),
            ctx.embedding_model,
        )
    except Exception as e:
        raise ContextAwareFatalError(f"Context embedding (query) failed: {e}") from e
    for (label, did), (_, body_text) in zip(doc_ids_for_rag, docs_for_distill, strict=True):
        if store.count_chunks_for_document(conn, did) > 0:
            continue
        chunks = chunk_plain_text(body_text)
        if not chunks:
            continue
        try:
            embs = embed_texts(chunks, ctx.embedding_model)
        except Exception as e:
            raise ContextAwareFatalError(f"Context embedding (chunks) failed: {e}") from e
        payload = [
            (i, ch, embs[i], {"document": label}) for i, ch in enumerate(chunks) if i < len(embs)
        ]
        store.replace_chunks(conn, did, payload)
    doc_ids = [did for _, did in doc_ids_for_rag]
    retrieved = store.search_chunks(conn, q_emb, limit=16, document_ids=doc_ids)
    text = "\n\n".join(retrieved) if retrieved else combined
    return _clamp_context_text(text, ctx.max_bytes)


def build_context_brief_for_pr(
    ctx: ContextAwareReviewConfig,
    scm: SCMConfig,
    refs: list[ContextReference],
    full_diff: str,
) -> str | None:
    """
    Fetch/cache linked context and distill it to a requirements-focused brief.

    Returns None when there are no applicable references or all fetches are skipped.
    Raises ContextAwareFatalError on misconfigured remotes (handled by runner).
    """
    if not ctx.enabled or not refs:
        return None

    applicable = [r for r in refs if _ref_applicable(r, ctx)]
    if not applicable:
        logger.info("context_aware: no references for enabled sources")
        return None

    creds = _get_external_credentials(scm, ctx)

    db_url = (ctx.db_url or "").strip()
    if not db_url:
        documents_for_distill = _load_context_documents_without_store(
            ctx=ctx,
            applicable=applicable,
            creds=creds,
        )
        if not documents_for_distill:
            logger.info("context_aware: no document bodies resolved")
            return None
        raw_for_distill = "\n\n---\n\n".join(
            f"## {label}\n{text}" for label, text in documents_for_distill
        )
        logger.info(
            "context_aware: resolved %d document(s), ~%d bytes before distillation "
            "(direct mode, no DB cache/RAG)",
            len(documents_for_distill),
            len(raw_for_distill.encode("utf-8")),
        )
        raw_for_distill = _clamp_context_text(raw_for_distill, ctx.max_bytes)
        brief = distill_context_text(
            raw_for_distill,
            max_output_tokens=ctx.distilled_max_tokens,
        )
        if not brief.strip():
            return None
        return brief

    cache_key = (db_url, ctx.embedding_dimensions)
    store = _store_cache.get(cache_key)
    if store is None:
        store = ContextStore(db_url, ctx.embedding_dimensions)
        _store_cache[cache_key] = store

    conn = store.connect()
    try:
        store.ensure_schema(conn)
        documents_for_distill, doc_ids_for_rag = _load_context_documents(
            store=store,
            conn=conn,
            scm=scm,
            ctx=ctx,
            applicable=applicable,
            creds=creds,
        )

        if not documents_for_distill:
            logger.info("context_aware: no document bodies resolved")
            return None

        combined = "\n\n---\n\n".join(
            f"## {label}\n{text}" for label, text in documents_for_distill
        )
        total_bytes = len(combined.encode("utf-8"))
        logger.info(
            "context_aware: resolved %d document(s), ~%d bytes before distillation",
            len(documents_for_distill),
            total_bytes,
        )

        raw_for_distill = combined
        if total_bytes > ctx.max_bytes:
            logger.info(
                "context_aware: over byte budget (%d > %d), running retrieval",
                total_bytes,
                ctx.max_bytes,
            )
            raw_for_distill = _build_retrieved_context_text(
                store=store,
                conn=conn,
                ctx=ctx,
                full_diff=full_diff,
                docs_for_distill=documents_for_distill,
                doc_ids_for_rag=doc_ids_for_rag,
                combined=combined,
            )
    finally:
        conn.close()

    brief = distill_context_text(raw_for_distill, max_output_tokens=ctx.distilled_max_tokens)
    if not brief.strip():
        return None
    return brief
