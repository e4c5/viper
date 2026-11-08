import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from code_review.llm_telemetry import collect_review_usage, log_adk_llm_usage
from code_review.models import PRContext, get_effective_temperature_for_model
from code_review.orchestration.orchestrator import ReviewOrchestrator

logger = logging.getLogger("test_review_usage")


def _response(prompt=100, completion=20, cached=30, thoughts=5):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=completion,
            cached_content_token_count=cached,
            thoughts_token_count=thoughts,
        )
    )


def test_collects_review_batches_and_ignores_support_tasks():
    with collect_review_usage("anthropic", "claude-haiku-5-5") as usage:
        log_adk_llm_usage(logger, task="review_batch_1", response=_response())
        log_adk_llm_usage(logger, task="review_batch_2", response=_response(prompt=50, cached=None))
        log_adk_llm_usage(logger, task="verification", response=_response(prompt=999))

    assert usage.prompt_tokens == 150
    assert usage.completion_tokens == 40
    assert usage.cached_tokens == 30
    assert usage.thoughts_tokens == 10


def test_review_log_line_names_the_collecting_model():
    with (
        patch("code_review.llm_telemetry.log_llm_usage") as log_usage,
        patch(
            "code_review.llm_telemetry.effective_llm_identity",
            return_value=("anthropic", "claude-sonnet-5-5"),
        ),
        collect_review_usage("anthropic", "claude-haiku-5-5"),
    ):
        log_adk_llm_usage(logger, task="review_batch_1", response=_response())
        log_adk_llm_usage(logger, task="verification", response=_response())

    assert log_usage.call_args_list[0].kwargs["model"] == "claude-haiku-5-5"
    assert log_usage.call_args_list[1].kwargs["model"] == "claude-sonnet-5-5"


def test_usage_is_not_collected_outside_the_block():
    with collect_review_usage() as usage:
        pass
    log_adk_llm_usage(logger, task="review_batch_1", response=_response())
    assert usage.prompt_tokens == 0


def test_haiku_5_5_omits_temperature():
    assert get_effective_temperature_for_model("anthropic", "claude-haiku-5-5", 0.0) is None


def test_shadow_uses_the_primary_idempotency_key():
    scm_cfg = SimpleNamespace(provider="github", url="https://api.github.com")
    primary = SimpleNamespace(provider="anthropic", model="claude-sonnet-5-5")
    shadow = SimpleNamespace(provider="anthropic", model="claude-haiku-5-5")
    primary_key = PRContext("o", "r", 1, "abc").idempotency_key(scm_cfg, primary, "")
    marker_comment = {"body": "marker"}
    observability = MagicMock()

    orchestrator = ReviewOrchestrator(
        "o", "r", 1, "abc", app_config=MagicMock(disable_idempotency=False),
        idempotency_llm_config=primary,
    )
    with patch(
        "code_review.orchestration.orchestrator._idempotency_key_seen_in_comments",
        side_effect=lambda _comments, key: key == primary_key,
    ):
        result = orchestrator._compute_idempotency_and_maybe_short_circuit(
            scm_cfg, shadow, [marker_comment], observability
        )

    assert result == []
