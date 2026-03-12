"""Tests for LLM_DISABLE_TOOL_CALLS debug mode in create_review_agent (Phase 1.1)."""

from unittest.mock import MagicMock, patch

from code_review.agent import (
    FINDINGS_ONLY_INSTRUCTION,
    SINGLE_SHOT_INSTRUCTION,
    create_review_agent,
)


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
    # File-by-file mode: must use FINDINGS_ONLY_INSTRUCTION (has tool references)
    assert kwargs["instruction"] == FINDINGS_ONLY_INSTRUCTION


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


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.create_findings_only_tools")
@patch("code_review.agent.agent.get_llm_config")
def test_create_review_agent_disable_tools_param_overrides_factory(
    mock_get_llm_config, mock_create_tools, mock_agent_cls
) -> None:
    """disable_tools=True creates agent with no tools even if disable_tool_calls is False.

    This is the single-shot mode path: the full diff is already in the user message
    so there is nothing to fetch.  Giving the agent tools in this mode causes it to
    call get_pr_diff_for_file / get_file_content for every file, leading to triangular
    token accumulation and multi-million-token usage on large PRs.
    """
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(
        temperature=0.0,
        max_output_tokens=1024,
        disable_tool_calls=False,  # env flag not set
    )
    mock_create_tools.return_value = [MagicMock(name="tool1")]
    agent_instance = MagicMock()
    mock_agent_cls.return_value = agent_instance

    result = create_review_agent(
        provider, review_standards="", findings_only=True, disable_tools=True
    )

    assert result is agent_instance
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["tools"] == [], (
        "single-shot mode must create the agent with no tools to prevent triangular "
        "token accumulation"
    )
    # Tools factory must NOT be called when tools are disabled.
    mock_create_tools.assert_not_called()


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.create_findings_only_tools")
@patch("code_review.agent.agent.get_llm_config")
def test_single_shot_uses_single_shot_instruction(
    mock_get_llm_config, mock_create_tools, mock_agent_cls
) -> None:
    """Single-shot mode (disable_tools=True) must use SINGLE_SHOT_INSTRUCTION.

    FINDINGS_ONLY_INSTRUCTION references tools (get_file_content, get_file_lines,
    detect_language_context) that are absent in single-shot mode.  When Gemini sees
    those references but the tools aren't registered, it infers it cannot complete
    the workflow and returns [] (no findings).  SINGLE_SHOT_INSTRUCTION is clean and
    tool-free, so the LLM reviews the embedded diff and returns real findings.
    """
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(
        temperature=0.0,
        max_output_tokens=65_000,
        disable_tool_calls=False,
    )
    mock_create_tools.return_value = []
    agent_instance = MagicMock()
    mock_agent_cls.return_value = agent_instance

    create_review_agent(provider, review_standards="", findings_only=True, disable_tools=True)

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["instruction"] == SINGLE_SHOT_INSTRUCTION, (
        "single-shot mode must use SINGLE_SHOT_INSTRUCTION (no tool references) "
        "to avoid Gemini returning [] when referenced tools are absent"
    )
    assert "get_file_content" not in kwargs["instruction"], (
        "SINGLE_SHOT_INSTRUCTION must not reference tools that are not available"
    )
    assert "get_pr_diff_for_file" not in kwargs["instruction"], (
        "SINGLE_SHOT_INSTRUCTION must not reference tools that are not available"
    )


@patch("google.adk.agents.Agent")
@patch("code_review.agent.agent.create_findings_only_tools")
@patch("code_review.agent.agent.get_llm_config")
def test_file_by_file_uses_findings_only_instruction(
    mock_get_llm_config, mock_create_tools, mock_agent_cls
) -> None:
    """File-by-file mode (disable_tools=False) must use FINDINGS_ONLY_INSTRUCTION."""
    provider = MagicMock()
    mock_get_llm_config.return_value = MagicMock(
        temperature=0.0,
        max_output_tokens=4096,
        disable_tool_calls=False,
    )
    tools = [MagicMock()]
    mock_create_tools.return_value = tools
    agent_instance = MagicMock()
    mock_agent_cls.return_value = agent_instance

    create_review_agent(provider, review_standards="", findings_only=True, disable_tools=False)

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["instruction"] == FINDINGS_ONLY_INSTRUCTION
    assert "get_pr_diff_for_file" in kwargs["instruction"]
