"""Resolve references, cache in PostgreSQL, distill (direct or RAG)."""

from __future__ import annotations

import logging

from code_review.config import ContextAwareReviewConfig, SCMConfig
from code_review.context.distiller import distill_context_text
from code_review.context.errors import ContextAwareFatalError
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
        return ("jira", ctx.jira_url)
    if ref.ref_type == ReferenceType.CONFLUENCE:
        return ("confluence", ctx.confluence_url)
    return ("unknown", "")


def _get_external_credentials(
    scm: SCMConfig, ctx: ContextAwareReviewConfig
) -> ExternalCredentials:
    gh_api, gh_tok = _github_api_and_token(scm, ctx)
    gl_api, gl_tok = _gitlab_api_and_token(scm, ctx)
    jira_tok = ctx.jira_token.get_secret_value() if ctx.jira_token else ""
    conf_tok = ctx.confluence_token.get_secret_value() if ctx.confluence_token else ""
    return ExternalCredentials(
        github_api=gh_api,
        github_token=gh_tok,
        gitlab_api=gl_api,
        gitlab_token=gl_tok,
        jira_email=ctx.jira_email.strip(),
        jira_token=jira_tok,
        confluence_email=ctx.confluence_email.strip(),
        confluence_token=conf_tok,
    )


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
    extra_fields = tuple(f.strip() for f in (ctx.jira_extra_fields or "").split(",") if f.strip())
    fetch_cfg = FetchReferenceConfig(
        github_api_base=creds.github_api,
        github_token=creds.github_token,
        gitlab_api_base=creds.gitlab_api,
        gitlab_token=creds.gitlab_token,
        jira_base=ctx.jira_url,
        jira_email=creds.jira_email,
        jira_token=creds.jira_token,
        confluence_base=ctx.confluence_url,
        confluence_email=creds.confluence_email,
        confluence_token=creds.confluence_token,
        ctx_github_enabled=ctx.github_issues_enabled,
        ctx_gitlab_enabled=ctx.gitlab_issues_enabled,
        ctx_jira_enabled=ctx.jira_enabled,
        ctx_confluence_enabled=ctx.confluence_enabled,
        jira_extra_fields=extra_fields,
    )
    for ref in applicable:
        src_name, base = _source_name_and_base(ref, ctx, scm)
        if not base and ref.ref_type != ReferenceType.GITHUB_ISSUE:
            continue
        source_id = store.get_or_create_source(conn, src_name, base)
        row = store.load_document(conn, source_id, ref.external_id)
        if row is not None and row[3]:
            content = row[1]
            doc_id = row[0]
            logger.debug("context cache hit %s", ref.display)
        else:
            fetched = fetch_reference(
                ref,
                cfg=fetch_cfg,
            )
            if fetched is None:
                continue
            doc_id = store.upsert_document(conn, source_id, fetched)
            content = fetched.body
        docs_for_distill.append((ref.display, content))
        doc_ids_for_rag.append((ref.display, doc_id))
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
    encoded = text.encode("utf-8")
    if len(encoded) > ctx.max_bytes:
        return encoded[: ctx.max_bytes].decode("utf-8", errors="ignore") + "\n…(truncated)"
    return text


def build_context_brief_for_pr(
    ctx: ContextAwareReviewConfig,
    scm: SCMConfig,
    refs: list[ContextReference],
    full_diff: str,
) -> str | None:
    """
    Fetch/cache linked context, distill to a brief, wrap in ``<context>...</context>``.

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

    db_url = ctx.db_url or ""
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
    return f"<context>\n{brief}\n</context>"
