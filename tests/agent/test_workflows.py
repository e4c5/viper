"""Tests for ADK workflow prototypes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents import BaseAgent
from google.genai import types
from pydantic import Field

from code_review.agent.workflows import (
    BatchReviewWorkflowAgent,
    build_prepared_batch_user_message,
    create_sequential_batch_review_agent,
)
from code_review.batching import ReviewBatch, ReviewSegment


class _FakeReviewAgent(BaseAgent):
    instruction: str = "base instruction"
    include_contents: str = "default"
    disallow_transfer_to_parent: bool = False
    disallow_transfer_to_peers: bool = False
    seen_user_messages: list[str] = Field(default_factory=list)

    async def run_async(self, ctx):
        self.seen_user_messages.append(ctx.user_content.parts[0].text)
        if False:
            yield None


@patch("code_review.agent.workflows.create_review_agent")
def test_create_sequential_batch_review_agent_builds_one_sub_agent_per_batch(
    mock_create_review_agent,
) -> None:
    provider = MagicMock()
    sub_agent_a = _FakeReviewAgent(name="sub_agent_a")
    sub_agent_b = _FakeReviewAgent(name="sub_agent_b")
    mock_create_review_agent.side_effect = [sub_agent_a, sub_agent_b]

    batches = [
        ReviewBatch(
            batch_index=0,
            estimated_tokens=10,
            paths=("a.py", "b.py"),
            segments=(
                ReviewSegment(
                    path="a.py",
                    diff_text=(
                        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                        "@@ -1,1 +1,2 @@\n old\n+new\n"
                    ),
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
                    diff_text=(
                        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n"
                        "@@ -10,1 +10,2 @@\n old\n+newer\n"
                    ),
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

    assert mock_create_review_agent.call_count == 2
    mock_create_review_agent.assert_any_call(
        provider,
        "review standards",
        context_brief_attached=True,
        review_visible_lines=None,
        slim_output=True,
        output_key=None,
    )
    assert result.name == "sequential_batch_review_agent"
    assert len(result.sub_agents) == 2
    assert result.sub_agents[0].name == "batch_review_0"
    assert result.sub_agents[1].name == "batch_review_1"
    assert "review exactly one prepared batch" in result.sub_agents[0].instruction
    assert "Prepared batch segments" not in result.sub_agents[0].instruction
    assert "Segment: full-file segment for a.py" not in result.sub_agents[0].instruction
    assert "2:+new" not in result.sub_agents[0].instruction
    assert result.sub_agents[0].include_contents == "none"
    assert result.sub_agents[1].include_contents == "none"


def test_build_prepared_batch_user_message_contains_batch_payload() -> None:
    batch = ReviewBatch(
        batch_index=0,
        estimated_tokens=10,
        paths=("a.py",),
        segments=(
            ReviewSegment(
                path="a.py",
                diff_text=(
                    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                    "@@ -1,1 +1,2 @@\n old\n+new\n"
                ),
                estimated_tokens=5,
                segment_index=0,
                total_segments=1,
                split_strategy="whole_file",
            ),
        ),
    )

    message = build_prepared_batch_user_message(
        batch=batch,
        owner="o",
        repo="r",
        pr_number=1,
        head_sha="sha1",
        prompt_suffix="extra context",
    )

    assert "owner=o, repo=r, pr_number=1" in message
    assert "head_sha=sha1" in message
    assert "extra context" in message
    assert "Prepared batch segments" in message
    assert "Segment: full-file segment for a.py" in message
    assert "2:+new" in message


@pytest.mark.asyncio
async def test_batch_review_workflow_passes_distinct_user_messages_to_sub_agents() -> None:
    sub_agent_a = _FakeReviewAgent(name="batch_review_0")
    sub_agent_b = _FakeReviewAgent(name="batch_review_1")
    workflow = BatchReviewWorkflowAgent(
        name="sequential_batch_review_agent",
        sub_agents=[sub_agent_a, sub_agent_b],
        batch_user_messages=[
            types.Content(role="user", parts=[types.Part(text="batch A")]),
            types.Content(role="user", parts=[types.Part(text="batch B")]),
        ],
    )

    class _Ctx:
        def __init__(self, user_content):
            self.user_content = user_content
            self.session_service = MagicMock()
            self.session_service.append_event = AsyncMock()
            self.session = MagicMock()
            self.invocation_id = "invocation-1"
            self.branch = "branch-1"

        def model_copy(self, update):
            copied = _Ctx(update.get("user_content", self.user_content))
            copied.session_service = self.session_service
            copied.session = self.session
            copied.invocation_id = self.invocation_id
            copied.branch = self.branch
            return copied

        def should_pause_invocation(self, _event):
            return False

    ctx = _Ctx(types.Content(role="user", parts=[types.Part(text="root")]))

    async for _event in workflow._run_async_impl(ctx):
        pass

    assert sub_agent_a.seen_user_messages == ["batch A"]
    assert sub_agent_b.seen_user_messages == ["batch B"]
    appended_events = [call.kwargs["event"] for call in ctx.session_service.append_event.call_args_list]
    assert [event.content.parts[0].text for event in appended_events] == ["batch A", "batch B"]

@pytest.mark.asyncio
async def test_batch_review_workflow_resumes_from_correct_sub_agent() -> None:
    class _FakePausableAgent(BaseAgent):
        pause_on_call: bool = False
        call_count: int = 0

        async def run_async(self, ctx):
            self.call_count += 1
            yield None
            if self.pause_on_call:
                pass  # We yield an event that causes pause

    sub_agent_a = _FakePausableAgent(name="batch_review_0", pause_on_call=True)
    sub_agent_b = _FakePausableAgent(name="batch_review_1")
    workflow = BatchReviewWorkflowAgent(
        name="sequential_batch_review_agent",
        sub_agents=[sub_agent_a, sub_agent_b],
    )

    class _Ctx:
        def __init__(self):
            self.user_content = types.Content(role="user", parts=[types.Part(text="root")])
            self.session_service = MagicMock()
            self.session_service.append_event = AsyncMock()
            self.session = MagicMock()
            self.invocation_id = "invocation-1"
            self.branch = "branch-1"

        def model_copy(self, update):
            copied = _Ctx()
            return copied

        def should_pause_invocation(self, _event):
            # Pause on the first agent
            return sub_agent_a.pause_on_call

    ctx = _Ctx()

    # Run 1: Should pause on sub_agent_a
    async for _event in workflow._run_async_impl(ctx):
        pass

    assert workflow.current_index == 0
    assert sub_agent_a.call_count == 1
    assert sub_agent_b.call_count == 0

    # Resume: turn off pause so it can proceed
    sub_agent_a.pause_on_call = False
    
    # Run 2: Should finish sub_agent_a, then run sub_agent_b
    async for _event in workflow._run_async_impl(ctx):
        pass

    assert workflow.current_index == 2
    assert sub_agent_a.call_count == 2
    assert sub_agent_b.call_count == 1

