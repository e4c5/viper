from unittest.mock import MagicMock, patch

import pytest

from code_review.agent.summary_agent import (
    SUMMARY_INSTRUCTION,
    create_summary_agent,
    generate_pr_summary,
    split_summary_for_pr_description,
)
from code_review.schemas.findings import FindingV1


@patch("code_review.agent.summary_agent.get_configured_summary_model")
@patch("code_review.agent.summary_agent.get_summary_llm_config")
@patch("code_review.agent.summary_agent.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_summary_agent_uses_summary_model_helper(
    mock_agent_cls, mock_get_llm_cfg, mock_get_summary_cfg, mock_get_summary_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=2048
    )
    mock_get_summary_cfg.return_value = MagicMock(provider=None, model=None)
    mock_get_summary_model.return_value = "cheap-summary-model"
    inst = MagicMock()
    mock_agent_cls.return_value = inst

    out = create_summary_agent()

    assert out is inst
    _, kwargs = mock_agent_cls.call_args
    assert kwargs["model"] == "cheap-summary-model"
    assert kwargs["name"] == "summary_agent"
    assert kwargs["instruction"] == SUMMARY_INSTRUCTION
    assert kwargs["generate_content_config"].temperature == pytest.approx(0.2)
    mock_get_summary_model.assert_called_once()


@patch("code_review.agent.summary_agent.log_adk_llm_usage")
@patch("code_review.agent.summary_agent.get_configured_summary_model")
@patch("code_review.agent.summary_agent.get_summary_llm_config")
@patch("code_review.agent.summary_agent.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_summary_agent_after_model_callback_accepts_adk_keywords(
    mock_agent_cls,
    mock_get_llm_cfg,
    mock_get_summary_cfg,
    mock_get_summary_model,
    mock_log_usage,
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=2048
    )
    mock_get_summary_cfg.return_value = MagicMock(provider=None, model=None)
    mock_get_summary_model.return_value = "cheap-summary-model"

    create_summary_agent()

    _, kwargs = mock_agent_cls.call_args
    response = MagicMock()
    kwargs["after_model_callback"](callback_context=MagicMock(), llm_response=response)

    mock_log_usage.assert_called_once()
    _, log_kwargs = mock_log_usage.call_args
    assert log_kwargs["task"] == "summary"
    assert log_kwargs["response"] is response
    assert log_kwargs["provider"] == "gemini"
    assert log_kwargs["model"] == "gemini-3.1"


@patch("code_review.agent.summary_agent.get_configured_summary_model")
@patch("code_review.agent.summary_agent.get_summary_llm_config")
@patch("code_review.agent.summary_agent.get_llm_config")
@patch("google.adk.agents.Agent")
def test_create_summary_agent_omits_temperature_for_summary_override_fixed_temperature_model(
    mock_agent_cls, mock_get_llm_cfg, mock_get_summary_cfg, mock_get_summary_model
):
    mock_get_llm_cfg.return_value = MagicMock(
        provider="gemini", model="gemini-3.1", max_output_tokens=2048
    )
    mock_get_summary_cfg.return_value = MagicMock(provider="openai", model="gpt-5.4")
    mock_get_summary_model.return_value = "openai/gpt-5.4"

    create_summary_agent()

    _, kwargs = mock_agent_cls.call_args
    assert kwargs["generate_content_config"].temperature is None


def test_generate_pr_summary_incremental_prompt():
    agent = MagicMock()
    pr_info = MagicMock()
    pr_info.title = "Test PR"
    pr_info.description = "Test Desc"
    # Ensure FindingV1 has all required fields for validation if needed,
    # though here we are just passing a list of them.
    findings = [
        FindingV1(path="file.py", line=10, message="Issue", severity="high", code="test-code")
    ]
    changed_paths = ["file.py"]
    
    # Patch both the Runner and the collection helper for robust isolation
    with patch("google.adk.runners.Runner") as mock_runner_cls, \
         patch(
             "code_review.orchestration.runner_utils._run_agent_and_collect_response"
         ) as mock_run:
        
        mock_runner = mock_runner_cls.return_value
        # Mock Runner.run to return a final response event as requested
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.author = "summary_agent"
        mock_event.content.parts = [MagicMock(text="Summary Text")]
        mock_runner.run.return_value = [mock_event]
        # Also mock run_async for completeness if it's used elsewhere
        mock_runner.run_async.return_value = MagicMock()

        mock_run.return_value = "Summary Text"
        
        generate_pr_summary(
            agent, 
            pr_info, 
            findings, 
            changed_paths, 
            incremental_base_sha="abcdef1234567890",
            incremental_commits=["Fixed bug A", "Added feature B"]
        )
        
        # Check the prompt
        args, _ = mock_run.call_args
        content = args[2]
        prompt = content.parts[0].text
        
        print(f"PROMPT:\n{prompt}")
        
        assert "Incremental Review Context: from abcdef123456" in prompt
        assert "Incremental commits in this update:" in prompt
        assert "- Fixed bug A" in prompt
        assert "- Added feature B" in prompt
        assert "Changed Files: file.py" in prompt
        # In the new logic, the agent includes the description if it's there.
        # The Orchestrator is responsible for scrubbing it for incremental reviews.
        # So here, since we provided it in the mock, it should be there.
        assert "PR Description: Test Desc" in prompt

def test_generate_pr_summary_incremental_unknown_base():
    agent = MagicMock()
    pr_info = MagicMock()
    pr_info.title = "Test PR"
    pr_info.description = ""
    findings = []
    changed_paths = ["file.py"]
    
    with patch("google.adk.runners.Runner") as _, \
         patch(
             "code_review.orchestration.runner_utils._run_agent_and_collect_response"
         ) as mock_run:
        
        mock_run.return_value = "Summary Text"
        
        generate_pr_summary(
            agent, 
            pr_info, 
            findings, 
            changed_paths, 
            incremental_base_sha="", # Unknown base
            incremental_commits=["New commit"]
        )
        
        # Check the prompt
        args, _ = mock_run.call_args
        content = args[2]
        prompt = content.parts[0].text
        
        assert "Incremental Review Context: from unknown base" in prompt
        assert "Incremental commits in this update:" in prompt
        assert "- New commit" in prompt

def test_generate_pr_summary_non_incremental_prompt():
    agent = MagicMock()
    pr_info = MagicMock()
    pr_info.title = "Test PR"
    pr_info.description = "Test Desc"
    findings = []
    changed_paths = ["file.py"]
    
    with patch("google.adk.runners.Runner") as mock_runner_cls, \
         patch(
             "code_review.orchestration.runner_utils._run_agent_and_collect_response"
         ) as mock_run:
        
        mock_runner = mock_runner_cls.return_value
        # Mock Runner.run to return a final response event as requested
        mock_event = MagicMock()
        mock_event.is_final_response.return_value = True
        mock_event.author = "summary_agent"
        mock_event.content.parts = [MagicMock(text="Summary Text")]
        mock_runner.run.return_value = [mock_event]
        # Also mock run_async for completeness if it's used elsewhere
        mock_runner.run_async.return_value = MagicMock()

        mock_run.return_value = "Summary Text"
        
        generate_pr_summary(
            agent, 
            pr_info, 
            findings, 
            changed_paths
        )
        
        # Check the prompt
        args, _ = mock_run.call_args
        content = args[2]
        prompt = content.parts[0].text
        
        assert "Incremental Review Context" not in prompt
        assert "Changed Files: file.py" in prompt
        assert "PR Description: Test Desc" in prompt

def test_split_summary_for_pr_description_atx():
    text = "## Summary\nSum\n\n## Description\nDesc\n\n## Walkthrough\nWalk"
    desc, comment = split_summary_for_pr_description(text)
    assert desc == "## Summary\nSum\n\n## Description\nDesc"
    assert comment == "## Walkthrough\nWalk"

def test_split_summary_for_pr_description_numbered_bold():
    text = "1. **Summary**\nSum\n\n2. **Description**\nDesc\n\n3. **Walkthrough**\nWalk"
    desc, comment = split_summary_for_pr_description(text)
    assert desc == "1. **Summary**\nSum\n\n2. **Description**\nDesc"
    assert comment == "3. **Walkthrough**\nWalk"

def test_split_summary_for_pr_description_bold():
    text = "**Summary**\nSum\n\n**Description**\nDesc\n\n**Walkthrough**\nWalk"
    desc, comment = split_summary_for_pr_description(text)
    assert desc == "**Summary**\nSum\n\n**Description**\nDesc"
    assert comment == "**Walkthrough**\nWalk"
