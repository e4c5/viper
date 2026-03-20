from unittest.mock import MagicMock, patch

from code_review.context.distiller import distill_context_text


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
