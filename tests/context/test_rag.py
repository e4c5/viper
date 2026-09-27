from unittest.mock import MagicMock, patch

import pytest

from code_review.context.rag import (
    _embedding_provider_matches_llm,
    build_semantic_query_from_diff,
    chunk_plain_text,
    embed_query_text,
    embed_texts,
)


def test_chunk_plain_text_validation_errors():
    with pytest.raises(ValueError, match="max_chunk_chars must be positive"):
        chunk_plain_text("abc", max_chunk_chars=0)
    with pytest.raises(ValueError, match="overlap must be non-negative"):
        chunk_plain_text("abc", max_chunk_chars=10, overlap=-1)
    with pytest.raises(ValueError, match="must be less than max_chunk_chars"):
        chunk_plain_text("abc", max_chunk_chars=10, overlap=10)


def test_chunk_empty_and_whitespace():
    assert chunk_plain_text("") == []
    assert chunk_plain_text("   \n\t  ") == []


def test_chunk_short_and_exact():
    assert chunk_plain_text("short") == ["short"]
    text = "a" * 1800
    assert chunk_plain_text(text, max_chunk_chars=1800) == [text]


def test_chunk_plain_text_splits_with_overlap():
    text = "".join(str(i % 10) for i in range(50))
    chunks = chunk_plain_text(text, max_chunk_chars=20, overlap=5)
    assert len(chunks) >= 3
    assert all(chunks)
    # Overlap implies neighboring chunks share trailing/leading content.
    assert chunks[0][-5:] == chunks[1][:5]


def test_semantic_query_empty_and_whitespace_diff():
    assert build_semantic_query_from_diff("") == "pull request code changes"
    assert build_semantic_query_from_diff("   \n  ") == "pull request code changes"


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("code_review.context.rag.log_llm_usage")
@patch("litellm.completion")
def test_build_semantic_query_from_diff_uses_llm_output(
    mock_completion, mock_log_llm_usage, mock_get_configured_model, mock_get_llm_config
):
    mock_get_llm_config.return_value = MagicMock(
        provider="openai", model="gpt-4o-mini", temperature=0.0
    )
    mock_get_configured_model.return_value = "openai/gpt-4o-mini"
    mock_completion.return_value = {
        "choices": [{"message": {"content": "Updates auth middleware validation flow."}}]
    }
    diff = "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n+validate()"
    out = build_semantic_query_from_diff(diff)
    assert out == "Updates auth middleware validation flow."
    assert mock_log_llm_usage.call_args.kwargs["model"] == "openai/gpt-4o-mini"


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("litellm.completion", side_effect=Exception("timeout"))
def test_semantic_query_falls_back_to_heuristic_on_llm_failure(
    mock_completion, mock_model, mock_llm
):
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"

    diff = "--- a/foo.py\n+++ b/foo.py\n+pass"
    result = build_semantic_query_from_diff(diff)
    assert "foo.py" in result


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("litellm.completion", side_effect=Exception("no creds"))
def test_semantic_query_heuristic_no_paths(mock_completion, mock_model, mock_llm):
    # Diff has no +++ / --- header lines, so the heuristic produces the generic fallback.
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"
    result = build_semantic_query_from_diff("no file headers here")
    assert result == "pull request code changes"


# ---------------------------------------------------------------------------
# embed_texts / embed_query_text
# ---------------------------------------------------------------------------


def _make_embedding_response(vectors: list[list[float]]) -> dict:
    return {
        "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
    }


@patch("litellm.embedding")
def test_embed_texts_returns_vectors_in_order(mock_embedding):
    vecs = [[0.1, 0.2], [0.3, 0.4]]
    mock_embedding.return_value = _make_embedding_response(vecs)

    result = embed_texts(["hello", "world"], "text-embedding-3-small")
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def _llm_config(provider: str, api_key: str | None):
    from code_review.config import LLMConfig

    return LLMConfig(provider=provider, api_key=api_key)


@patch("code_review.context.rag.get_llm_config")
@patch("litellm.embedding")
def test_embed_texts_forwards_api_key_for_matching_provider(mock_embedding, mock_llm):
    """Primary LLM key is forwarded only when the embedding provider matches."""
    mock_embedding.return_value = _make_embedding_response([[0.1]])
    mock_llm.return_value = _llm_config("openai", "sk-primary")

    # Bare model name → litellm resolves to OpenAI → matches provider.
    embed_texts(["x"], "text-embedding-3-small")
    assert mock_embedding.call_args.kwargs["api_key"] == "sk-primary"

    # Explicit provider prefix that matches also gets the key.
    mock_llm.return_value = _llm_config("gemini", "gem-key")
    embed_texts(["x"], "gemini/text-embedding-004")
    assert mock_embedding.call_args.kwargs["api_key"] == "gem-key"


@patch("code_review.context.rag.get_llm_config")
@patch("litellm.embedding")
def test_embed_texts_omits_api_key_for_other_provider(mock_embedding, mock_llm):
    """A Gemini key must never be sent to the OpenAI embeddings endpoint (and
    an explicit api_key must not shadow the embedding provider's env creds)."""
    mock_embedding.return_value = _make_embedding_response([[0.1]])
    mock_llm.return_value = _llm_config("gemini", "gem-key")

    embed_texts(["x"], "text-embedding-3-small")  # bare → openai ≠ gemini
    assert "api_key" not in mock_embedding.call_args.kwargs

    embed_texts(["x"], "openai/text-embedding-3-small")
    assert "api_key" not in mock_embedding.call_args.kwargs


@patch("code_review.context.rag.get_llm_config")
@patch("litellm.embedding")
def test_embed_texts_no_key_means_no_kwarg(mock_embedding, mock_llm):
    """Matching provider but unset key → api_key omitted entirely."""
    mock_embedding.return_value = _make_embedding_response([[0.1]])
    mock_llm.return_value = _llm_config("openai", None)

    embed_texts(["x"], "text-embedding-3-small")
    assert "api_key" not in mock_embedding.call_args.kwargs


@patch("litellm.embedding")
def test_embed_texts_empty_input(mock_embedding):
    result = embed_texts([], "text-embedding-3-small")
    assert result == []
    mock_embedding.assert_not_called()


@patch("litellm.embedding")
def test_embed_texts_count_mismatch_raises(mock_embedding):
    # API returns fewer embeddings than inputs — should raise.
    mock_embedding.return_value = _make_embedding_response([[0.1, 0.2]])
    with pytest.raises(RuntimeError, match="mismatch"):
        embed_texts(["a", "b"], "text-embedding-3-small")


@patch("litellm.embedding")
def test_embed_query_text_returns_single_vector(mock_embedding):
    mock_embedding.return_value = _make_embedding_response([[0.5, 0.6, 0.7]])
    result = embed_query_text("my query", "text-embedding-3-small")
    assert result == [0.5, 0.6, 0.7]


@pytest.mark.parametrize(
    ("model", "llm_provider", "expected"),
    [
        ("openai/text-embedding-3-small", "openai", True),
        ("openai/text-embedding-3-small", "gemini", False),
        # vertex_ai is an alias for vertex on both sides of the comparison.
        ("vertex_ai/text-embedding-004", "vertex", True),
        ("vertex_ai/text-embedding-004", "vertex_ai", True),
        ("vertex/text-embedding-004", "vertex_ai", True),
        # Bare model names default to OpenAI embeddings in litellm.
        ("text-embedding-3-small", "openai", True),
        ("text-embedding-3-small", "gemini", False),
    ],
)
def test_embedding_provider_matches_llm(model, llm_provider, expected):
    assert _embedding_provider_matches_llm(model, llm_provider) is expected
