import pytest
from unittest.mock import MagicMock, patch
from code_review.agent.summary_agent import generate_pr_summary
from code_review.schemas.findings import FindingV1

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
         patch("code_review.orchestration.runner_utils._run_agent_and_collect_response") as mock_run:
        
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
         patch("code_review.orchestration.runner_utils._run_agent_and_collect_response") as mock_run:
        
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
         patch("code_review.orchestration.runner_utils._run_agent_and_collect_response") as mock_run:
        
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
