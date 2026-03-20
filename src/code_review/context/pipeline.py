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


def _get_source_tokens(ctx: ContextAwareReviewConfig) -> tuple[str, str, str, str]:
    jira_tok = ctx.jira_token.get_secret_value() if ctx.jira_token else ""
    conf_tok = ctx.confluence_token.get_secret_value() if ctx.confluence_token else ""
    return (ctx.jira_email.strip(), jira_tok, ctx.confluence_email.strip(), conf_tok)


def _load_context_documents(
    *,
    store: ContextStore,
    scm: SCMConfig,
    ctx: ContextAwareReviewConfig,
    applicable: list[ContextReference],
    gh_api: str,
    gh_tok: str,
    jira_email: str,
    jira_tok: str,
    conf_email: str,
    conf_tok: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, object]]]:
    docs_for_distill: list[tuple[str, str]] = []
    doc_ids_for_rag: list[tuple[str, object]] = []
    conn = store.connect()
    store.ensure_schema(conn)
    try:
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
            docs_for_distill.append((ref.display, content))
            doc_ids_for_rag.append((ref.display, doc_id))
    finally:
        conn.close()
    return (docs_for_distill, doc_ids_for_rag)


def _build_retrieved_context_text(
    *,
    store: ContextStore,
    ctx: ContextAwareReviewConfig,
    full_diff: str,
    docs_for_distill: list[tuple[str, str]],
    doc_ids_for_rag: list[tuple[str, object]],
    combined: str,
) -> str:
    conn = store.connect()
    try:
        store.ensure_schema(conn)
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
        retrieved = store.search_chunks(conn, q_emb, limit=16)
        if not retrieved:
            return combined[: ctx.max_bytes] + "\n…(truncated)"
        return "\n\n".join(retrieved)
    finally:
        conn.close()


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
    jira_email, jira_tok, conf_email, conf_tok = _get_source_tokens(ctx)

    store = ContextStore(ctx.db_url or "", ctx.embedding_dimensions)
    documents_for_distill, doc_ids_for_rag = _load_context_documents(
        store=store,
        scm=scm,
        ctx=ctx,
        applicable=applicable,
        gh_api=gh_api,
        gh_tok=gh_tok,
        jira_email=jira_email,
        jira_tok=jira_tok,
        conf_email=conf_email,
        conf_tok=conf_tok,
    )

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
        raw_for_distill = _build_retrieved_context_text(
            store=store,
            ctx=ctx,
            full_diff=full_diff,
            docs_for_distill=documents_for_distill,
            doc_ids_for_rag=doc_ids_for_rag,
            combined=combined,
        )

    brief = distill_context_text(raw_for_distill, max_output_tokens=ctx.distilled_max_tokens)
    if not brief.strip():
        return None
    return f"<context>\n{brief}\n</context>"
