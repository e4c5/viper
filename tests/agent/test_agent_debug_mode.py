"""Tests for LLM_DISABLE_TOOL_CALLS debug mode in create_review_agent (Phase 1.1)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_review.agent import (
    FINDINGS_ONLY_INSTRUCTION,
    SINGLE_SHOT_INSTRUCTION,
    create_review_agent,
)
from code_review.agent.agent import (
    _TOOL_RESULT_CHAR_LIMIT,
    _after_tool_callback,
    _before_model_callback,
    _before_tool_callback,
)
from code_review.schemas.findings import FindingsBatchV1


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
    assert kwargs["output_schema"] is FindingsBatchV1
    assert kwargs["before_model_callback"] is _before_model_callback
    assert kwargs["after_tool_callback"] is _after_tool_callback
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
    assert kwargs["output_schema"] is FindingsBatchV1


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
    assert kwargs["output_schema"] is FindingsBatchV1
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
    assert kwargs["output_schema"] is FindingsBatchV1
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
    assert kwargs["output_schema"] is FindingsBatchV1
    assert "get_pr_diff_for_file" in kwargs["instruction"]


# --- Tests for improved instruction content ---


def test_findings_only_instruction_contains_line_number_guidance():
    """FINDINGS_ONLY_INSTRUCTION must contain line number guidance based on <L{n}> annotations.

    In file-by-file mode the agent reads a diff returned from get_pr_diff_for_file.
    The diff is pre-annotated with <L{n}> prefixes on visible new-file lines.
    The instruction must tell the agent to use these annotations as the 'line'
    value in findings so it does not have to compute line numbers from hunk headers.
    """
    assert "<L{n}>" in FINDINGS_ONLY_INSTRUCTION or "<L" in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must explain the <L{n}> line number annotation format"
    )
    assert "annotation" in FINDINGS_ONLY_INSTRUCTION.lower(), (
        "FINDINGS_ONLY_INSTRUCTION must reference the line number annotations"
    )


def test_findings_only_instruction_restricts_to_visible_diff_lines():
    """FINDINGS_ONLY_INSTRUCTION must tell the agent to only report lines visible in the diff."""
    instr = FINDINGS_ONLY_INSTRUCTION
    # Must tell the agent to drop findings for lines with no annotation
    assert "annotation" in instr.lower() or "annotated" in instr.lower(), (
        "FINDINGS_ONLY_INSTRUCTION must describe the <L{n}> annotation mechanism"
    )
    # Must distinguish added (+) vs context lines
    assert "+" in instr, "FINDINGS_ONLY_INSTRUCTION must mention '+' for added lines"


def test_findings_only_instruction_head_sha_ref_guidance():
    """FINDINGS_ONLY_INSTRUCTION must tell the agent to use head_sha as ref for get_file_lines."""
    assert "head_sha" in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must guide the agent to use head_sha as the ref parameter "
        "for get_file_lines and get_file_content so it reads the correct revision"
    )


def test_findings_only_instruction_category_field():
    """FINDINGS_ONLY_INSTRUCTION must mention the category field in the output format."""
    assert "category" in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must mention the optional 'category' field "
        "so the agent populates it with values like Correctness, Security, etc."
    )
    # Must provide example values so the agent knows what to put there
    assert "Correctness" in FINDINGS_ONLY_INSTRUCTION or "Security" in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must list example category values"
    )


def test_findings_only_instruction_mentions_evidence_and_confidence():
    """FINDINGS_ONLY_INSTRUCTION should bias the model toward evidence-backed findings."""
    lowered = FINDINGS_ONLY_INSTRUCTION.lower()
    assert "evidence" in lowered
    assert "confidence" in lowered
    assert "reconstruct the" in lowered and "builder" in lowered


def test_single_shot_instruction_category_field():
    """SINGLE_SHOT_INSTRUCTION must mention the category field with example values."""
    assert "category" in SINGLE_SHOT_INSTRUCTION, (
        "SINGLE_SHOT_INSTRUCTION must mention the optional 'category' field"
    )
    assert "Correctness" in SINGLE_SHOT_INSTRUCTION or "Security" in SINGLE_SHOT_INSTRUCTION, (
        "SINGLE_SHOT_INSTRUCTION must list example category values"
    )


def test_single_shot_instruction_prefers_omission_over_weak_speculation():
    """SINGLE_SHOT_INSTRUCTION should tell the model to omit contradicted or weak findings."""
    lowered = SINGLE_SHOT_INSTRUCTION.lower()
    assert "omit the finding" in lowered
    assert "prefer omission over weak speculation" in lowered


def test_instructions_consistent_category_guidance():
    """Both instructions should describe the category field consistently."""
    # Both must mention the same set of category values
    for category in ("Correctness", "Security", "Performance", "Maintainability"):
        assert category in FINDINGS_ONLY_INSTRUCTION, (
            f"FINDINGS_ONLY_INSTRUCTION missing category example: {category}"
        )
        assert category in SINGLE_SHOT_INSTRUCTION, (
            f"SINGLE_SHOT_INSTRUCTION missing category example: {category}"
        )


# --- Shared fragment consistency tests ---


def test_shared_line_number_rules_appear_in_both_instructions():
    """Both instructions must contain the shared <L{n}> line-number rule bullets.

    These bullets are extracted into ``_SHARED_LINE_NUMBER_RULES`` to avoid
    duplication.  Verifying both instructions contain the shared text ensures
    the composition is correct and a change to the shared fragment propagates
    to both modes automatically.
    """
    from code_review.agent.agent import _SHARED_LINE_NUMBER_RULES

    assert _SHARED_LINE_NUMBER_RULES in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must contain the shared line-number rules fragment"
    )
    assert _SHARED_LINE_NUMBER_RULES in SINGLE_SHOT_INSTRUCTION, (
        "SINGLE_SHOT_INSTRUCTION must contain the shared line-number rules fragment"
    )


def test_shared_format_and_placement_appear_in_both_instructions():
    """Both instructions must contain the shared output-format and placement rules.

    ``_SHARED_FORMAT_AND_PLACEMENT`` covers output format, finding schema,
    anchor guidance, and placement rules — all identical in both modes.
    """
    from code_review.agent.agent import _SHARED_FORMAT_AND_PLACEMENT

    assert _SHARED_FORMAT_AND_PLACEMENT in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must contain the shared format/placement fragment"
    )
    assert _SHARED_FORMAT_AND_PLACEMENT in SINGLE_SHOT_INSTRUCTION, (
        "SINGLE_SHOT_INSTRUCTION must contain the shared format/placement fragment"
    )


def test_shared_agent_fix_and_examples_appear_in_both_instructions():
    """Both instructions must contain the shared agent_fix_prompt guidance and examples."""
    from code_review.agent.agent import _SHARED_AGENT_FIX_AND_EXAMPLES

    assert _SHARED_AGENT_FIX_AND_EXAMPLES in FINDINGS_ONLY_INSTRUCTION, (
        "FINDINGS_ONLY_INSTRUCTION must contain the shared agent_fix_prompt/examples fragment"
    )
    assert _SHARED_AGENT_FIX_AND_EXAMPLES in SINGLE_SHOT_INSTRUCTION, (
        "SINGLE_SHOT_INSTRUCTION must contain the shared agent_fix_prompt/examples fragment"
    )


class _FakeLlmRequest:
    def __init__(self, tools_dict: dict[str, object]) -> None:
        self.tools_dict = tools_dict
        self.added_instructions: list[list[str]] = []

    def append_instructions(self, instructions: list[str]) -> None:
        self.added_instructions.append(instructions)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("tools_dict", "expected_fragments"),
    [
        (
            {
                "get_pr_diff_for_file": object(),
                "get_file_content": object(),
                "get_file_lines": object(),
            },
            [
                "Only call registered tools",
                "get_pr_diff_for_file",
                "get_file_content",
                "get_file_lines",
                "required structured schema",
            ],
        ),
        (
            {},
            [
                "No tools are available for this run",
                "use only the prompt context",
                "required structured schema",
            ],
        ),
    ],
)
def test_before_model_callback_adds_runtime_guardrails(
    tools_dict: dict[str, object], expected_fragments: list[str]
) -> None:
    llm_request = _FakeLlmRequest(tools_dict)

    _run(_before_model_callback(SimpleNamespace(agent_name="code_review_agent"), llm_request))

    assert len(llm_request.added_instructions) == 1
    rendered = "\n".join(llm_request.added_instructions[0])
    for fragment in expected_fragments:
        assert fragment in rendered


@pytest.mark.parametrize(
    ("tool_name", "args", "expected"),
    [
        (
            "get_pr_diff_for_file",
            {"path": "   "},
            {"error": "get_pr_diff_for_file: path must be a non-empty string."},
        ),
        (
            "get_file_content",
            {"path": "README.md", "ref": ""},
            {"error": "get_file_content: ref must be a non-empty string."},
        ),
        (
            "get_file_lines",
            {"path": "src/app.py", "ref": "abc123", "start_line": 20, "end_line": 10},
            {"error": "get_file_lines: end_line must be greater than or equal to start_line."},
        ),
    ],
)
def test_before_tool_callback_rejects_invalid_calls(
    tool_name: str, args: dict[str, object], expected: dict[str, str]
) -> None:
    result = _run(_before_tool_callback(SimpleNamespace(name=tool_name), args, SimpleNamespace()))
    assert result == expected


def test_after_tool_callback_normalizes_crlf() -> None:
    result = _run(
        _after_tool_callback(
            SimpleNamespace(name="get_file_content"),
            {"path": "README.md", "ref": "abc123"},
            tool_context=SimpleNamespace(),
            tool_response="line1\r\nline2\r\n",
        )
    )

    assert result == "line1\nline2\n"


def test_after_tool_callback_truncates_oversized_string_results() -> None:
    oversized = "x" * (_TOOL_RESULT_CHAR_LIMIT + 50)

    result = _run(
        _after_tool_callback(
            SimpleNamespace(name="get_file_content"),
            {"path": "README.md", "ref": "abc123"},
            tool_context=SimpleNamespace(),
            tool_response=oversized,
        )
    )

    assert result is not None
    assert result.endswith("\n...[truncated by callback]")
    assert len(result) > _TOOL_RESULT_CHAR_LIMIT
