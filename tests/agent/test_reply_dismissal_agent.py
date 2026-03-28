"""Tests for reply-dismissal agent factory and LLM text parsing."""

from unittest.mock import MagicMock, patch

from code_review.agent.reply_dismissal_agent import (
    REPLY_DISMISSAL_INSTRUCTION,
    create_reply_dismissal_agent,
    reply_dismissal_verdict_from_llm_text,
)


@patch("code_review.agent.reply_dismissal_agent.get_configured_model")
@patch("code_review.agent.reply_dismissal_agent.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_reply_dismissal_agent_is_tool_free(
    mock_agent_cls, mock_get_llm_cfg, mock_get_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        temperature=0.1,
        max_output_tokens=512,
    )
    inst = MagicMock()
    mock_agent_cls.return_value = inst

    out = create_reply_dismissal_agent()

    assert out is inst
    mock_agent_cls.assert_called_once()
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["tools"] == []
    assert kwargs["name"] == "reply_dismissal_agent"
    assert kwargs["instruction"] == REPLY_DISMISSAL_INSTRUCTION
    mock_get_model.assert_called_once()


def test_reply_dismissal_verdict_from_llm_raw_json():
    text = '{"verdict": "agreed", "reply_text": ""}'
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "agreed"


def test_reply_dismissal_verdict_from_llm_fenced():
    text = """Here you go:
```json
{"verdict": "disagreed", "reply_text": "Please add a null check."}
```
"""
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "disagreed"
    assert "null check" in v.reply_text


def test_reply_dismissal_verdict_from_llm_fenced_no_language_tag():
    text = """```
{"verdict": "agreed", "reply_text": ""}
```"""
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "agreed"


def test_reply_dismissal_verdict_from_llm_brace_substring():
    text = 'Prefix {"verdict": "agreed", "reply_text": ""} suffix'
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "agreed"


def test_reply_dismissal_verdict_with_extra_braces_before_object():
    text = 'Note {not json} then {"verdict": "agreed", "reply_text": ""}'
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "agreed"


def test_reply_dismissal_verdict_repairs_python_style_apostrophe_escape():
    text = """```json
{
  "verdict": "disagreed",
  "reply_text": "Use XML-safe escaping for characters like '<', '>', '&', '\\\"', or \\' before writing attributes."
}
```"""
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "disagreed"
    assert "XML-safe escaping" in v.reply_text
    assert "\\'" not in v.reply_text


def test_reply_dismissal_verdict_skips_unrelated_leading_json_object():
    text = (
        'Context {"trace_id": "x", "n": 1} then '
        '{"verdict": "agreed", "reply_text": ""}'
    )
    v = reply_dismissal_verdict_from_llm_text(text)
    assert v is not None
    assert v.verdict == "agreed"


def test_reply_dismissal_verdict_invalid_returns_none():
    assert reply_dismissal_verdict_from_llm_text("not json") is None
    assert reply_dismissal_verdict_from_llm_text('{"verdict": "disagreed"}') is None


def test_reply_dismissal_instruction_mentions_severity_guidance():
    assert "original automated review comment severity" in REPLY_DISMISSAL_INSTRUCTION
    assert "nit: be pragmatic and easy to satisfy" in REPLY_DISMISSAL_INSTRUCTION
    assert "high: require strong, specific evidence" in REPLY_DISMISSAL_INSTRUCTION
