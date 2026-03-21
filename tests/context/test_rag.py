from unittest.mock import MagicMock, patch

import pytest

from code_review.context.rag import build_semantic_query_from_diff, chunk_plain_text


def test_chunk_plain_text_validation_errors():
    with pytest.raises(ValueError, match="max_chunk_chars must be positive"):
        chunk_plain_text("abc", max_chunk_chars=0)
    with pytest.raises(ValueError, match="overlap must be non-negative"):
        chunk_plain_text("abc", max_chunk_chars=10, overlap=-1)
    with pytest.raises(ValueError, match="must be less than max_chunk_chars"):
        chunk_plain_text("abc", max_chunk_chars=10, overlap=10)


def test_chunk_plain_text_splits_with_overlap():
    text = "x" * 50
    chunks = chunk_plain_text(text, max_chunk_chars=20, overlap=5)
    assert len(chunks) >= 3
    assert all(chunks)
    # Overlap implies neighboring chunks share trailing/leading content.
    assert chunks[0][-5:] == chunks[1][:5]


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("code_review.context.rag.litellm.completion")
def test_build_semantic_query_from_diff_uses_llm_output(
    mock_completion, mock_get_configured_model, mock_get_llm_config
):
    mock_get_llm_config.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_get_configured_model.return_value = "openai/gpt-4o-mini"
    mock_completion.return_value = {
        "choices": [{"message": {"content": "Updates auth middleware validation flow."}}]
    }
    diff = "diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n+validate()"
    out = build_semantic_query_from_diff(diff)
    assert out == "Updates auth middleware validation flow."


@patch("code_review.context.rag.get_llm_config")
@patch("code_review.context.rag.get_configured_model")
@patch("code_review.context.rag.litellm.completion")
def test_build_semantic_query_from_diff_falls_back_to_heuristic_on_failure(
    mock_completion, mock_get_configured_model, mock_get_llm_config
):
    mock_get_llm_config.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_get_configured_model.return_value = "openai/gpt-4o-mini"
    mock_completion.side_effect = RuntimeError("llm unavailable")
    diff = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "+validate()\n"
    )
    out = build_semantic_query_from_diff(diff)
    assert "src/auth.py" in out
