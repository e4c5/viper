"""Unit tests for rag.py: chunking, semantic query generation, embedding helpers."""

from unittest.mock import MagicMock, patch

import pytest

from code_review.context.rag import (
    build_semantic_query_from_diff,
    chunk_plain_text,
    embed_query_text,
    embed_texts,
)


# ---------------------------------------------------------------------------
# chunk_plain_text
# ---------------------------------------------------------------------------


def test_chunk_empty_string():
    assert chunk_plain_text("") == []


def test_chunk_whitespace_only():
    assert chunk_plain_text("   \n\t  ") == []


def test_chunk_short_text_returns_single_chunk():
    text = "short text"
    chunks = chunk_plain_text(text, max_chunk_chars=1800)
    assert chunks == [text]


def test_chunk_text_exactly_max_returns_single_chunk():
    text = "a" * 1800
    assert chunk_plain_text(text, max_chunk_chars=1800) == [text]


def test_chunk_long_text_produces_multiple_chunks():
    text = "x" * 4000
    chunks = chunk_plain_text(text, max_chunk_chars=1800, overlap=200)
    assert len(chunks) > 1


def test_chunk_overlap_gives_shared_content():
    text = "a" * 1800 + "b" * 1800
    chunks = chunk_plain_text(text, max_chunk_chars=1800, overlap=200)
    # The second chunk should start 200 chars before the end of the first chunk,
    # so it should contain some 'a' characters.
    assert "a" in chunks[1]


def test_chunk_all_chunks_non_empty():
    text = "word " * 1000
    chunks = chunk_plain_text(text, max_chunk_chars=300, overlap=50)
    assert all(c.strip() for c in chunks)


def test_chunk_invalid_overlap_raises():
    with pytest.raises(ValueError, match="overlap"):
        chunk_plain_text("text", max_chunk_chars=100, overlap=100)


def test_chunk_negative_overlap_raises():
    with pytest.raises(ValueError, match="overlap"):
        chunk_plain_text("text", max_chunk_chars=100, overlap=-1)


def test_chunk_zero_max_chunk_chars_raises():
    with pytest.raises(ValueError, match="max_chunk_chars"):
        chunk_plain_text("text", max_chunk_chars=0)


# ---------------------------------------------------------------------------
# build_semantic_query_from_diff
# ---------------------------------------------------------------------------


def _make_llm_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_semantic_query_empty_diff():
    result = build_semantic_query_from_diff("")
    assert result == "pull request code changes"


def test_semantic_query_whitespace_diff():
    result = build_semantic_query_from_diff("   \n  ")
    assert result == "pull request code changes"


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("code_review.context.rag.litellm.completion")
def test_semantic_query_uses_llm_response(mock_completion, mock_model, mock_llm):
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"
    mock_completion.return_value = _make_llm_response("Adds JWT validation to auth module.")

    result = build_semantic_query_from_diff("--- a/auth.py\n+++ b/auth.py\n+def validate(): pass")
    assert result == "Adds JWT validation to auth module."


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("code_review.context.rag.litellm.completion", side_effect=Exception("timeout"))
def test_semantic_query_falls_back_to_heuristic_on_llm_failure(mock_completion, mock_model, mock_llm):
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"

    diff = "--- a/foo.py\n+++ b/foo.py\n+pass"
    result = build_semantic_query_from_diff(diff)
    assert "foo.py" in result


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("code_review.context.rag.litellm.completion", side_effect=Exception("no creds"))
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


@patch("code_review.context.rag.litellm.embedding")
def test_embed_texts_returns_vectors_in_order(mock_embedding):
    vecs = [[0.1, 0.2], [0.3, 0.4]]
    mock_embedding.return_value = _make_embedding_response(vecs)

    result = embed_texts(["hello", "world"], "text-embedding-3-small")
    assert result == [[0.1, 0.2], [0.3, 0.4]]


@patch("code_review.context.rag.litellm.embedding")
def test_embed_texts_empty_input(mock_embedding):
    result = embed_texts([], "text-embedding-3-small")
    assert result == []
    mock_embedding.assert_not_called()


@patch("code_review.context.rag.litellm.embedding")
def test_embed_texts_count_mismatch_raises(mock_embedding):
    # API returns fewer embeddings than inputs — should raise.
    mock_embedding.return_value = _make_embedding_response([[0.1, 0.2]])
    with pytest.raises(RuntimeError, match="mismatch"):
        embed_texts(["a", "b"], "text-embedding-3-small")


@patch("code_review.context.rag.litellm.embedding")
def test_embed_query_text_returns_single_vector(mock_embedding):
    mock_embedding.return_value = _make_embedding_response([[0.5, 0.6, 0.7]])
    result = embed_query_text("my query", "text-embedding-3-small")
    assert result == [0.5, 0.6, 0.7]
