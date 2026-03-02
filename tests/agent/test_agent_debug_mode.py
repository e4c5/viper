"""Tests for LLM_DISABLE_TOOL_CALLS debug mode in create_review_agent (Phase 1.1)."""

from unittest.mock import MagicMock, patch

from code_review.agent import create_review_agent


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.create_findings_only_tools")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_tools_enabled_by_default(
    mock_get_llm_config, mock_create_tools, mock_agent_cls
) -> None:
    """When disable_tool_calls is False, agent receives tools from factory."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(
        temperature=0.0,
        max_output_tokens=1024,
        disable_tool_calls=False,
    )
    tools = [MagicMock(name="tool1"), MagicMock(name="tool2")]
    mock_create_tools.return_value = tools
    agent_instance = MagicMock()
    mock_agent_cls.return_value = agent_instance

    result = create_review_agent(provider, review_standards="", findings_only=True)

    assert result is agent_instance
    assert mock_agent_cls.call_count == 1
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["tools"] == tools


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.create_findings_only_tools")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_disable_tool_calls_uses_no_tools(
    mock_get_llm_config, mock_create_tools, mock_agent_cls
) -> None:
    """When disable_tool_calls is True, agent is constructed with no tools."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(
        temperature=0.0,
        max_output_tokens=1024,
        disable_tool_calls=True,
    )
    mock_create_tools.return_value = [MagicMock(name="tool1")]
    agent_instance = MagicMock()
    mock_agent_cls.return_value = agent_instance

    result = create_review_agent(provider, review_standards="", findings_only=True)

    assert result is agent_instance
    assert mock_agent_cls.call_count == 1
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["tools"] == []

