from unittest.mock import MagicMock, patch

from code_review.context.distiller import (
    _distilled_text_from_content,
    _litellm_model_name,
    _raw_context_fallback,
    _text_from_content_block,
    distill_context_text,
)


def _make_completion_response(content):
    """Build a MagicMock that mimics a LiteLLM ModelResponse with attribute access."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@patch("code_review.context.distiller.get_llm_config")
@patch("code_review.context.distiller.get_configured_model")
@patch("code_review.context.distiller.litellm.completion")
def test_distiller_accepts_structured_message_content(
    mock_completion,
    mock_get_model,
    mock_get_llm,
):
    mock_get_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_get_model.return_value = "openai/gpt-4o-mini"
    mock_completion.return_value = _make_completion_response(
        [
            {"type": "text", "text": "Req A"},
            {"type": "text", "text": "Req B"},
        ]
    )

    out = distill_context_text("raw context", max_output_tokens=200)
    assert out == "Req A\nReq B"


# ---------------------------------------------------------------------------
# _litellm_model_name
# ---------------------------------------------------------------------------


def test_litellm_model_name_string_passthrough():
    assert _litellm_model_name("openai/gpt-4o", "fallback") == "openai/gpt-4o"


def test_litellm_model_name_object_with_model_attr():
    obj = MagicMock()
    obj.model = "anthropic/claude-3"
    assert _litellm_model_name(obj, "fallback") == "anthropic/claude-3"


def test_litellm_model_name_object_with_empty_model_attr():
    obj = MagicMock()
    obj.model = ""
    assert _litellm_model_name(obj, "fallback") == "fallback"


def test_litellm_model_name_empty_string_uses_fallback():
    assert _litellm_model_name("  ", "fallback") == "fallback"


def test_litellm_model_name_non_string_model_attr_uses_fallback():
    obj = MagicMock()
    obj.model = 42  # not a string
    assert _litellm_model_name(obj, "fallback") == "fallback"


# ---------------------------------------------------------------------------
# _text_from_content_block
# ---------------------------------------------------------------------------


def test_text_from_content_block_string():
    assert _text_from_content_block("  hello  ") == "hello"


def test_text_from_content_block_non_dict_non_str():
    assert _text_from_content_block(123) == ""
    assert _text_from_content_block(None) == ""


def test_text_from_content_block_dict_output_text_key():
    assert _text_from_content_block({"output_text": "summary here"}) == "summary here"


def test_text_from_content_block_dict_content_key():
    assert _text_from_content_block({"content": "nested content"}) == "nested content"


def test_text_from_content_block_dict_no_matching_keys():
    assert _text_from_content_block({"other": "value"}) == ""


def test_text_from_content_block_dict_all_empty():
    assert _text_from_content_block({"text": "", "output_text": "   "}) == ""


# ---------------------------------------------------------------------------
# _distilled_text_from_content
# ---------------------------------------------------------------------------


def test_distilled_text_from_content_string():
    assert _distilled_text_from_content("  brief text  ") == "brief text"


def test_distilled_text_from_content_not_list_not_str():
    assert _distilled_text_from_content(42) == ""
    assert _distilled_text_from_content(None) == ""


def test_distilled_text_from_content_list_of_strings():
    result = _distilled_text_from_content(["part one", "part two"])
    assert "part one" in result
    assert "part two" in result


# ---------------------------------------------------------------------------
# _raw_context_fallback
# ---------------------------------------------------------------------------


def test_raw_context_fallback_short_text():
    assert _raw_context_fallback("short") == "short"


def test_raw_context_fallback_truncates_long_text():
    text = "x" * 9000
    result = _raw_context_fallback(text)
    assert result.endswith("…")
    assert len(result) <= 8002  # 8000 chars + ellipsis


# ---------------------------------------------------------------------------
# distill_context_text — edge / error paths
# ---------------------------------------------------------------------------


@patch("code_review.context.distiller.get_llm_config")
@patch("code_review.context.distiller.get_configured_model")
@patch("code_review.context.distiller.litellm.completion")
def test_distill_empty_raw_returns_empty(mock_comp, mock_model, mock_llm):
    result = distill_context_text("", max_output_tokens=500)
    assert result == ""
    mock_comp.assert_not_called()


@patch("code_review.context.distiller.get_llm_config")
@patch("code_review.context.distiller.get_configured_model")
@patch("code_review.context.distiller.litellm.completion", side_effect=Exception("timeout"))
def test_distill_llm_failure_returns_fallback(mock_comp, mock_model, mock_llm):
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"
    result = distill_context_text("Some context here.", max_output_tokens=500)
    assert "Some context" in result


@patch("code_review.context.distiller.get_llm_config")
@patch("code_review.context.distiller.get_configured_model")
@patch("code_review.context.distiller.litellm.completion")
def test_distill_empty_choices_returns_fallback(mock_comp, mock_model, mock_llm):
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"
    mock_comp.return_value = {"choices": []}
    result = distill_context_text("Fallback needed.", max_output_tokens=500)
    assert "Fallback needed" in result


@patch("code_review.context.distiller.get_llm_config")
@patch("code_review.context.distiller.get_configured_model")
@patch("code_review.context.distiller.litellm.completion")
def test_distill_empty_content_returns_fallback(mock_comp, mock_model, mock_llm):
    mock_llm.return_value = MagicMock(model="gpt-4o-mini", temperature=0.0)
    mock_model.return_value = "openai/gpt-4o-mini"
    mock_comp.return_value = {"choices": [{"message": {"content": ""}}]}
    result = distill_context_text("Original context.", max_output_tokens=500)
    assert "Original context" in result
