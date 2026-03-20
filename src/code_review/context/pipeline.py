"""Resolve references, cache in PostgreSQL, distill (direct or RAG)."""

from __future__ import annotations

import logging

from code_review.config import ContextAwareReviewConfig, SCMConfig
from code_review.context.distiller import distill_context_text
from code_review.context.errors import ContextAwareFatalError
from code_review.context.fetchers import fetch_reference
from code_review.context.rag import (
    build_semantic_query_from_diff,
    chunk_plain_text,
    embed_query_text,
    embed_texts,
)
from code_review.context.store import ContextStore
from code_review.context.types import ContextReference, ReferenceType

logger = logging.getLogger(__name__)


def _github_api_and_token(scm: SCMConfig, ctx: ContextAwareReviewConfig) -> tuple[str, str]:
    if scm.provider == "github":
        tok = scm.token
        token = tok.get_secret_value() if hasattr(tok, "get_secret_value") else str(tok)
        return scm.url.rstrip("/"), token
    tok = ctx.github_token.get_secret_value() if ctx.github_token else ""
    base = (ctx.github_api_url or "https://api.github.com").rstrip("/")
    return base, tok


def _ref_applicable(ref: ContextReference, ctx: ContextAwareReviewConfig) -> bool:
    if ref.ref_type == ReferenceType.GITHUB_ISSUE:
        return ctx.github_issues_enabled
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
    if ref.ref_type == ReferenceType.JIRA:
        return ("jira", ctx.jira_url)
    if ref.ref_type == ReferenceType.CONFLUENCE:
        return ("confluence", ctx.confluence_url)
    return ("unknown", "")


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

    gh_api, gh_tok = _github_api_and_token(scm, ctx)
    jira_email = ctx.jira_email.strip()
    jira_tok = ctx.jira_token.get_secret_value() if ctx.jira_token else ""
    conf_email = ctx.confluence_email.strip()
    conf_tok = ctx.confluence_token.get_secret_value() if ctx.confluence_token else ""

    store = ContextStore(ctx.db_url or "", ctx.embedding_dimensions)
    conn = store.connect()
    store.ensure_schema(conn)

    documents_for_distill: list[tuple[str, str]] = []  # (label, content)
    doc_ids_for_rag: list[tuple[str, object]] = []  # (label, uuid)

    try:
        for ref in applicable:
            src_name, base = _source_name_and_base(ref, ctx, scm)
            if not base and ref.ref_type != ReferenceType.GITHUB_ISSUE:
                continue
            source_id = store.get_or_create_source(conn, src_name, base)

            row = store.load_document(conn, source_id, ref.external_id)
            use_cached = row is not None and row[3]
            content: str
            doc_id = row[0] if row else None

            if use_cached and row:
                content = row[1]
                logger.debug("context cache hit %s", ref.display)
            else:
                fetched = fetch_reference(
                    ref,
                    github_api_base=gh_api,
                    github_token=gh_tok,
                    jira_base=ctx.jira_url,
                    jira_email=jira_email,
                    jira_token=jira_tok,
                    confluence_base=ctx.confluence_url,
                    confluence_email=conf_email,
                    confluence_token=conf_tok,
                    ctx_github_enabled=ctx.github_issues_enabled,
                    ctx_jira_enabled=ctx.jira_enabled,
                    ctx_confluence_enabled=ctx.confluence_enabled,
                )
                if fetched is None:
                    continue
                doc_id = store.upsert_document(conn, source_id, fetched)
                content = fetched.body

            documents_for_distill.append((ref.display, content))
            if doc_id is not None:
                doc_ids_for_rag.append((ref.display, doc_id))
    finally:
        conn.close()

    if not documents_for_distill:
        logger.info("context_aware: no document bodies resolved")
        return None

    combined = "\n\n---\n\n".join(f"## {label}\n{text}" for label, text in documents_for_distill)
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
        conn2 = store.connect()
        try:
            store.ensure_schema(conn2)
            try:
                q_emb = embed_query_text(
                    build_semantic_query_from_diff(full_diff),
                    ctx.embedding_model,
                )
            except Exception as e:
                raise ContextAwareFatalError(f"Context embedding (query) failed: {e}") from e

            for (dlabel, did), (_, body_text) in zip(
                doc_ids_for_rag, documents_for_distill, strict=True
            ):
                if store.count_chunks_for_document(conn2, did) > 0:
                    continue
                chunks = chunk_plain_text(body_text)
                if not chunks:
                    continue
                try:
                    embs = embed_texts(chunks, ctx.embedding_model)
                except Exception as e:
                    raise ContextAwareFatalError(f"Context embedding (chunks) failed: {e}") from e
                payload = [
                    (i, ch, embs[i], {"document": dlabel})
                    for i, ch in enumerate(chunks)
                    if i < len(embs)
                ]
                store.replace_chunks(conn2, did, payload)

            retrieved = store.search_chunks(conn2, q_emb, limit=16)
            if not retrieved:
                raw_for_distill = combined[: ctx.max_bytes] + "\n…(truncated)"
            else:
                raw_for_distill = "\n\n".join(retrieved)
        finally:
            conn2.close()

    brief = distill_context_text(raw_for_distill, max_output_tokens=ctx.distilled_max_tokens)
    if not brief.strip():
        return None
    return f"<context>\n{brief}\n</context>"
