"""Tests for ADK workflow prototypes."""

from unittest.mock import MagicMock, patch

from code_review.agent.workflows import create_sequential_batch_review_agent
from code_review.batching import ReviewBatch, ReviewSegment


@patch("google.adk.agents.SequentialAgent")
@patch("code_review.agent.workflows.create_review_agent")
def test_create_sequential_batch_review_agent_builds_one_sub_agent_per_batch(
    mock_create_review_agent, mock_sequential_agent_cls
) -> None:
    provider = MagicMock()
    sub_agent_a = MagicMock(name="sub_agent_a")
    sub_agent_a.instruction = "base instruction"
    sub_agent_b = MagicMock(name="sub_agent_b")
    sub_agent_b.instruction = "base instruction"
    mock_create_review_agent.side_effect = [sub_agent_a, sub_agent_b]
    sequential_instance = MagicMock()
    mock_sequential_agent_cls.return_value = sequential_instance

    batches = [
        ReviewBatch(
            batch_index=0,
            estimated_tokens=10,
            paths=("a.py", "b.py"),
            segments=(
                ReviewSegment(
                    path="a.py",
                    diff_text="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n old\n+new\n",
                    estimated_tokens=5,
                    segment_index=0,
                    total_segments=1,
                    split_strategy="whole_file",
                ),
            ),
        ),
        ReviewBatch(
            batch_index=1,
            estimated_tokens=8,
            paths=("c.py",),
            segments=(
                ReviewSegment(
                    path="c.py",
                    diff_text="diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -10,1 +10,2 @@\n old\n+newer\n",
                    estimated_tokens=8,
                    segment_index=0,
                    total_segments=1,
                    split_strategy="whole_file",
                ),
            ),
        ),
    ]

    result = create_sequential_batch_review_agent(
        provider,
        "review standards",
        batches,
        head_sha="sha1",
        context_brief_attached=True,
    )

    assert result is sequential_instance
    assert mock_create_review_agent.call_count == 2
    mock_create_review_agent.assert_any_call(
        provider,
        "review standards",
        findings_only=True,
        disable_tools=True,
        context_brief_attached=True,
    )
    _, kwargs = mock_sequential_agent_cls.call_args
    assert kwargs["name"] == "sequential_batch_review_agent"
    assert len(kwargs["sub_agents"]) == 2
    assert kwargs["sub_agents"][0].name == "batch_review_0"
    assert kwargs["sub_agents"][1].name == "batch_review_1"
    assert "Review exactly one prepared batch" in kwargs["sub_agents"][0].instruction
    assert "a.py, b.py" in kwargs["sub_agents"][0].instruction
    assert "Segment: full-file segment for a.py" in kwargs["sub_agents"][0].instruction
    assert "<L2>+new" in kwargs["sub_agents"][0].instruction
