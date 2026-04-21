"""Unit tests for pipeline.py — mocked store, fetchers, and distiller."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

import code_review.context.pipeline as pipeline_module
from code_review.context.errors import ContextAwareFatalError
from code_review.context.pipeline import build_context_brief_for_pr
from code_review.context.types import ContextReference, ExternalCredentials, ReferenceType


@pytest.fixture(autouse=True)
def _clear_store_cache():
    """Reset the module-level ContextStore cache so each test gets a fresh mock."""
    pipeline_module._store_cache.clear()
    yield
    pipeline_module._store_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GITHUB_REF = ContextReference(
    ref_type=ReferenceType.GITHUB_ISSUE,
    external_id="org/repo#1",
    display="org/repo#1",
)
_JIRA_REF = ContextReference(
    ref_type=ReferenceType.JIRA,
    external_id="PROJ-42",
    display="PROJ-42",
)


def _make_ctx(
    enabled=True,
    github_issues_enabled=True,
    jira_enabled=False,
    gitlab_issues_enabled=False,
    confluence_enabled=False,
    db_url="postgresql://u:p@host/db",
    max_bytes=20000,
    distilled_max_tokens=4000,
    embedding_model="text-embedding-3-small",
    embedding_dimensions=1536,
):
    ctx = MagicMock()
    ctx.enabled = enabled
    ctx.github_issues_enabled = github_issues_enabled
    ctx.jira_enabled = jira_enabled
    ctx.gitlab_issues_enabled = gitlab_issues_enabled
    ctx.confluence_enabled = confluence_enabled
    ctx.db_url = db_url
    ctx.max_bytes = max_bytes
    ctx.distilled_max_tokens = distilled_max_tokens
    ctx.embedding_model = embedding_model
    ctx.embedding_dimensions = embedding_dimensions
    ctx.github_token = None
    ctx.gitlab_token = None
    ctx.jira_url = ""
    ctx.jira_email = ""
    ctx.jira_token = None
    ctx.confluence_url = ""
    ctx.confluence_email = ""
    ctx.confluence_token = None
    ctx.github_api_url = None
    ctx.gitlab_api_url = None
    ctx.jira_extra_fields = ""
    return ctx


def _make_scm(provider="github", url="https://api.github.com", token="tok"):
    scm = MagicMock()
    scm.provider = provider
    scm.url = url
    tok = MagicMock()
    tok.get_secret_value.return_value = token
    scm.token = tok
    return scm


def _make_fetched_doc(external_id="org/repo#1", body="Issue body text", title="Issue title"):
    from code_review.context.fetchers import FetchedDocument

    return FetchedDocument(
        external_id=external_id,
        title=title,
        body=body,
        metadata={},
        version="1",
        external_updated_at=None,
    )


def test_get_external_credentials_builds_single_value_object():
    ctx = _make_ctx(gitlab_issues_enabled=True, jira_enabled=True, confluence_enabled=True)
    ctx.github_api_url = "https://api.github.example.com"
    ctx.github_token = MagicMock()
    ctx.github_token.get_secret_value.return_value = "gh-token"
    ctx.gitlab_api_url = "https://gitlab.example.com/api/v4"
    ctx.gitlab_token = MagicMock()
    ctx.gitlab_token.get_secret_value.return_value = "gl-token"
    ctx.jira_email = "jira@example.com"
    ctx.jira_token = MagicMock()
    ctx.jira_token.get_secret_value.return_value = "jira-token"
    ctx.confluence_email = "conf@example.com"
    ctx.confluence_token = MagicMock()
    ctx.confluence_token.get_secret_value.return_value = "conf-token"

    creds = pipeline_module._get_external_credentials(_make_scm(provider="gitea"), ctx)

    assert creds == ExternalCredentials(
        github_api="https://api.github.example.com",
        github_token="gh-token",
        gitlab_api="https://gitlab.example.com/api/v4",
        gitlab_token="gl-token",
        jira_email="jira@example.com",
        jira_token="jira-token",
        confluence_email="conf@example.com",
        confluence_token="conf-token",
    )


# ---------------------------------------------------------------------------
# Disabled / no refs
# ---------------------------------------------------------------------------


def test_returns_none_when_context_disabled():
    ctx = _make_ctx(enabled=False)
    result = build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff")
    assert result is None


def test_returns_none_when_no_refs():
    ctx = _make_ctx(enabled=True)
    result = build_context_brief_for_pr(ctx, _make_scm(), [], "diff")
    assert result is None


def test_returns_none_when_no_applicable_refs():
    # GitHub issues enabled=False, so the GitHub ref is not applicable.
    ctx = _make_ctx(enabled=True, github_issues_enabled=False)
    result = build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff")
    assert result is None


# ---------------------------------------------------------------------------
# Under-budget direct distillation path
# ---------------------------------------------------------------------------


def _patch_store_and_fetcher(fetched_doc, fresh=False):
    """Return a pair of context managers that mock ContextStore and fetch_reference."""
    store_instance = MagicMock()
    store_instance.connect.return_value = MagicMock()
    store_instance.ensure_schema.return_value = None
    doc_id = uuid.uuid4()
    store_instance.get_or_create_source.return_value = uuid.uuid4()
    # load_document returns (id, content, metadata, fresh)
    store_instance.load_document.return_value = (
        doc_id,
        fetched_doc.body,
        {},
        fresh,
    )
    store_instance.upsert_document.return_value = doc_id

    patch_store = patch(
        "code_review.context.pipeline.ContextStore",
        return_value=store_instance,
    )
    patch_fetch = patch(
        "code_review.context.pipeline.fetch_reference",
        return_value=fetched_doc,
    )
    return patch_store, patch_fetch, store_instance


@patch("code_review.context.pipeline.distill_context_text", return_value="Distilled brief.")
def test_under_budget_returns_distilled_brief(mock_distill):
    doc = _make_fetched_doc(body="x" * 100)  # well under 20 KB
    patch_store, patch_fetch, _ = _patch_store_and_fetcher(doc, fresh=False)
    ctx = _make_ctx()
    scm = _make_scm()

    with patch_store, patch_fetch:
        result = build_context_brief_for_pr(ctx, scm, [_GITHUB_REF], "diff text")

    assert result is not None
    assert result == "Distilled brief."


@patch("code_review.context.pipeline.ContextStore")
@patch("code_review.context.pipeline.distill_context_text", return_value="Direct brief.")
def test_missing_db_url_uses_direct_fetch_and_distillation(mock_distill, mock_store_cls):
    doc = _make_fetched_doc(body="Jira acceptance criteria")
    ctx = _make_ctx(db_url="")

    with patch("code_review.context.pipeline.fetch_reference", return_value=doc) as mock_fetch:
        result = build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff text")

    assert result is not None
    assert "Direct brief." in result
    mock_fetch.assert_called_once()
    mock_store_cls.assert_not_called()
    raw_context = mock_distill.call_args.args[0]
    assert "Jira acceptance criteria" in raw_context


@patch("code_review.context.pipeline.ContextStore")
@patch("code_review.context.pipeline.distill_context_text", return_value="Direct brief.")
def test_missing_whitespace_db_url_uses_direct_fetch_and_distillation(
    mock_distill, mock_store_cls
):
    doc = _make_fetched_doc(body="Issue body")
    ctx = _make_ctx(db_url="   ")

    with patch("code_review.context.pipeline.fetch_reference", return_value=doc):
        result = build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff text")

    assert result is not None
    assert "Direct brief." in result
    mock_store_cls.assert_not_called()


@patch("code_review.context.pipeline.distill_context_text", return_value="Direct brief.")
def test_missing_db_url_clamps_direct_distillation_input(mock_distill):
    doc = _make_fetched_doc(body="x" * 200)
    ctx = _make_ctx(db_url="", max_bytes=80)

    with patch("code_review.context.pipeline.fetch_reference", return_value=doc):
        result = build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff text")

    assert result is not None
    raw_context = mock_distill.call_args.args[0]
    assert len(raw_context.encode("utf-8")) <= ctx.max_bytes
    assert raw_context.endswith("…(truncated)")


@patch("code_review.context.pipeline.distill_context_text", return_value="")
def test_empty_distillation_returns_none(mock_distill):
    doc = _make_fetched_doc(body="content")
    patch_store, patch_fetch, _ = _patch_store_and_fetcher(doc, fresh=False)
    ctx = _make_ctx()

    with patch_store, patch_fetch:
        result = build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff")

    assert result is None


@patch("code_review.context.pipeline.distill_context_text", return_value="Brief.")
def test_cache_hit_skips_fetch(mock_distill):
    doc = _make_fetched_doc()
    patch_store, patch_fetch, _ = _patch_store_and_fetcher(doc, fresh=True)
    ctx = _make_ctx()

    with patch_store, patch_fetch as mock_fetch:
        build_context_brief_for_pr(ctx, _make_scm(), [_GITHUB_REF], "diff")

    # fetch_reference should NOT have been called — the cache was fresh.
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Fetch failure / None result
# ---------------------------------------------------------------------------


@patch("code_review.context.pipeline.distill_context_text", return_value="Brief.")
def test_fetch_returns_none_skips_ref(mock_distill):
    store_instance = MagicMock()
    store_instance.connect.return_value = MagicMock()
    store_instance.get_or_create_source.return_value = uuid.uuid4()
    store_instance.load_document.return_value = None  # cache miss

    with patch("code_review.context.pipeline.ContextStore", return_value=store_instance):
        with patch("code_review.context.pipeline.fetch_reference", return_value=None):
            result = build_context_brief_for_pr(_make_ctx(), _make_scm(), [_GITHUB_REF], "diff")

    assert result is None


# ---------------------------------------------------------------------------
# Over-budget RAG path
# ---------------------------------------------------------------------------


@patch("code_review.context.pipeline.distill_context_text", return_value="RAG brief.")
@patch("code_review.context.pipeline.embed_query_text", return_value=[0.1] * 1536)
@patch("code_review.context.pipeline.embed_texts", return_value=[[0.1] * 1536])
@patch("code_review.context.pipeline.build_semantic_query_from_diff", return_value="query")
@patch("code_review.context.pipeline.chunk_plain_text", return_value=["chunk1"])
def test_over_budget_uses_rag_path(
    mock_chunk, mock_query, mock_embed_texts, mock_embed_query, mock_distill
):
    large_body = "y" * 25000  # exceeds default max_bytes=20000
    doc = _make_fetched_doc(body=large_body)

    store_instance = MagicMock()
    store_instance.connect.return_value = MagicMock()
    doc_id = uuid.uuid4()
    store_instance.get_or_create_source.return_value = uuid.uuid4()
    store_instance.load_document.return_value = (doc_id, large_body, {}, False)
    store_instance.upsert_document.return_value = doc_id
    store_instance.count_chunks_for_document.return_value = 0
    store_instance.search_chunks.return_value = ["relevant chunk text"]

    with patch("code_review.context.pipeline.ContextStore", return_value=store_instance):
        with patch("code_review.context.pipeline.fetch_reference", return_value=doc):
            result = build_context_brief_for_pr(
                _make_ctx(max_bytes=20000), _make_scm(), [_GITHUB_REF], "diff"
            )

    assert result is not None
    assert "RAG brief." in result
    store_instance.search_chunks.assert_called_once()
    # Verify document_ids filter is passed through.
    _, kwargs = store_instance.search_chunks.call_args
    assert kwargs["document_ids"] == [doc_id]


# ---------------------------------------------------------------------------
# Fatal error propagation
# ---------------------------------------------------------------------------


def test_fatal_error_from_embedding_propagates():
    large_body = "z" * 25000

    store_instance = MagicMock()
    store_instance.connect.return_value = MagicMock()
    doc_id = uuid.uuid4()
    store_instance.get_or_create_source.return_value = uuid.uuid4()
    store_instance.load_document.return_value = (doc_id, large_body, {}, False)
    store_instance.upsert_document.return_value = doc_id
    store_instance.count_chunks_for_document.return_value = 0

    doc = _make_fetched_doc(body=large_body)

    with patch("code_review.context.pipeline.ContextStore", return_value=store_instance):
        with patch("code_review.context.pipeline.fetch_reference", return_value=doc):
            with patch(
                "code_review.context.pipeline.embed_query_text",
                side_effect=Exception("embed fail"),
            ):
                with pytest.raises(ContextAwareFatalError, match="embedding"):
                    build_context_brief_for_pr(
                        _make_ctx(max_bytes=20000), _make_scm(), [_GITHUB_REF], "diff"
                    )


# ---------------------------------------------------------------------------
# Alternative ref types (GitLab, Jira, Confluence)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _simple_store():
    store = MagicMock()
    store.connect.return_value = MagicMock()
    doc_id = uuid.uuid4()
    store.get_or_create_source.return_value = uuid.uuid4()
    store.load_document.return_value = (doc_id, "doc body", {}, False)
    store.upsert_document.return_value = doc_id
    return store


@patch("code_review.context.pipeline.distill_context_text", return_value="Brief.")
def test_jira_ref_resolved(_mock_distill, _simple_store):
    jira_ref = ContextReference(
        ref_type=ReferenceType.JIRA, external_id="PROJ-42", display="PROJ-42"
    )
    ctx = _make_ctx(jira_enabled=True)
    ctx.jira_url = "https://jira.example.com"

    with patch("code_review.context.pipeline.ContextStore", return_value=_simple_store):
        with patch(
            "code_review.context.pipeline.fetch_reference",
            return_value=_make_fetched_doc(external_id="PROJ-42"),
        ):
            result = build_context_brief_for_pr(ctx, _make_scm(), [jira_ref], "diff")

    assert result is not None
    assert "Brief." in result


@patch("code_review.context.pipeline.distill_context_text", return_value="Brief.")
def test_gitlab_ref_resolved(_mock_distill, _simple_store):
    gitlab_ref = ContextReference(
        ref_type=ReferenceType.GITLAB_ISSUE,
        external_id="group/repo#5",
        display="group/repo#5",
    )
    ctx = _make_ctx(gitlab_issues_enabled=True)
    scm = _make_scm(provider="gitlab", url="https://gitlab.com/api/v4")

    with patch("code_review.context.pipeline.ContextStore", return_value=_simple_store):
        with patch(
            "code_review.context.pipeline.fetch_reference",
            return_value=_make_fetched_doc(external_id="group/repo#5"),
        ):
            result = build_context_brief_for_pr(ctx, scm, [gitlab_ref], "diff")

    assert result is not None
    assert "Brief." in result


@patch("code_review.context.pipeline.distill_context_text", return_value="Brief.")
def test_confluence_ref_resolved(_mock_distill, _simple_store):
    conf_ref = ContextReference(
        ref_type=ReferenceType.CONFLUENCE,
        external_id="99999",
        display="99999",
    )
    ctx = _make_ctx(confluence_enabled=True)
    ctx.confluence_url = "https://wiki.example.com"

    with patch("code_review.context.pipeline.ContextStore", return_value=_simple_store):
        with patch(
            "code_review.context.pipeline.fetch_reference",
            return_value=_make_fetched_doc(external_id="99999"),
        ):
            result = build_context_brief_for_pr(ctx, _make_scm(), [conf_ref], "diff")

    assert result is not None
    assert "Brief." in result


# ---------------------------------------------------------------------------
# Transitive Confluence following from Jira tickets
# ---------------------------------------------------------------------------


@patch("code_review.context.pipeline.distill_context_text", return_value="Transitive brief.")
def test_jira_ticket_with_confluence_link_follows_transitively_no_store(mock_distill):
    """When a Jira body contains a Confluence URL, the pipeline fetches that page too (no DB)."""
    jira_body = (
        "Key: PROJ-42\nSummary: Design\nDescription:\n"
        "See https://wiki.example.com/wiki/spaces/ENG/pages/555/Spec for details"
    )
    jira_ref = ContextReference(
        ref_type=ReferenceType.JIRA, external_id="PROJ-42", display="PROJ-42"
    )

    jira_doc = _make_fetched_doc(external_id="PROJ-42", body=jira_body, title="Design")
    confluence_doc = _make_fetched_doc(
        external_id="555", body="Title: Spec\nBody:\nThe spec content", title="Spec"
    )

    def _side_effect(ref, *, cfg):
        if ref.ref_type == ReferenceType.JIRA:
            return jira_doc
        if ref.ref_type == ReferenceType.CONFLUENCE:
            return confluence_doc
        return None

    ctx = _make_ctx(jira_enabled=True, confluence_enabled=True, db_url="")
    ctx.jira_url = "https://jira.example.com"
    ctx.confluence_url = "https://wiki.example.com"

    with patch("code_review.context.pipeline.fetch_reference", side_effect=_side_effect) as mf:
        result = build_context_brief_for_pr(ctx, _make_scm(), [jira_ref], "diff")

    assert result is not None
    # fetch_reference should have been called twice: once for Jira, once for Confluence
    assert mf.call_count == 2
    ref_types = [call.args[0].ref_type for call in mf.call_args_list]
    assert ReferenceType.JIRA in ref_types
    assert ReferenceType.CONFLUENCE in ref_types
    # The distiller should have received both documents
    raw = mock_distill.call_args.args[0]
    assert "PROJ-42" in raw
    assert "Spec" in raw


@patch("code_review.context.pipeline.distill_context_text", return_value="Transitive brief.")
def test_jira_transitive_skips_already_listed_confluence_ref(mock_distill):
    """If a Confluence ref from the Jira body is already in the original refs, don't fetch twice."""
    jira_body = "See https://wiki.example.com/wiki/spaces/ENG/pages/555/Spec"
    jira_ref = ContextReference(
        ref_type=ReferenceType.JIRA, external_id="PROJ-42", display="PROJ-42"
    )
    conf_ref = ContextReference(
        ref_type=ReferenceType.CONFLUENCE, external_id="555", display="confluence-page:555"
    )

    jira_doc = _make_fetched_doc(external_id="PROJ-42", body=jira_body, title="Design")
    confluence_doc = _make_fetched_doc(external_id="555", body="Spec body", title="Spec")

    def _side_effect(ref, *, cfg):
        if ref.ref_type == ReferenceType.JIRA:
            return jira_doc
        if ref.ref_type == ReferenceType.CONFLUENCE:
            return confluence_doc
        return None

    ctx = _make_ctx(jira_enabled=True, confluence_enabled=True, db_url="")
    ctx.jira_url = "https://jira.example.com"
    ctx.confluence_url = "https://wiki.example.com"

    with patch("code_review.context.pipeline.fetch_reference", side_effect=_side_effect) as mf:
        result = build_context_brief_for_pr(ctx, _make_scm(), [jira_ref, conf_ref], "diff")

    assert result is not None
    # Confluence page 555 should be fetched only once (from the original ref list), not transitively
    conf_calls = [c for c in mf.call_args_list if c.args[0].ref_type == ReferenceType.CONFLUENCE]
    assert len(conf_calls) == 1


@patch("code_review.context.pipeline.distill_context_text", return_value="Transitive brief.")
def test_jira_transitive_does_not_follow_when_confluence_disabled(mock_distill):
    """Even if Jira body has Confluence links, don't follow when confluence_enabled=False."""
    jira_body = "See https://wiki.example.com/wiki/spaces/ENG/pages/555/Spec"
    jira_ref = ContextReference(
        ref_type=ReferenceType.JIRA, external_id="PROJ-42", display="PROJ-42"
    )

    jira_doc = _make_fetched_doc(external_id="PROJ-42", body=jira_body, title="Design")

    ctx = _make_ctx(jira_enabled=True, confluence_enabled=False, db_url="")
    ctx.jira_url = "https://jira.example.com"

    with patch("code_review.context.pipeline.fetch_reference", return_value=jira_doc) as mf:
        result = build_context_brief_for_pr(ctx, _make_scm(), [jira_ref], "diff")

    assert result is not None
    # Only the Jira fetch should have happened
    assert mf.call_count == 1
    assert mf.call_args_list[0].args[0].ref_type == ReferenceType.JIRA


def test_extract_transitive_confluence_refs_empty_body():
    ctx = MagicMock(confluence_enabled=True)
    seen_ids = set()

    with patch.object(pipeline_module.logger, "warning") as mock_warn:
        refs = pipeline_module._extract_transitive_confluence_refs(" ", ctx=ctx, seen_ids=seen_ids)

    assert refs == []
    mock_warn.assert_called_once_with("Fetched body is empty or whitespace-only.")
