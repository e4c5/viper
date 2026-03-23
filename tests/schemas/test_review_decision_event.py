"""Tests for ReviewDecisionEventContext and env loading."""

import os
from unittest.mock import patch

from code_review.schemas.review_decision_event import (
    ReviewDecisionEventContext,
    event_allows_decision_only_skip_when_bot_not_blocking,
    review_decision_event_context_from_env,
)

_EVENT_ENV_KEYS = (
    "CODE_REVIEW_EVENT_NAME",
    "CODE_REVIEW_EVENT_ACTION",
    "CODE_REVIEW_EVENT_KIND",
    "CODE_REVIEW_EVENT_COMMENT_ID",
    "CODE_REVIEW_EVENT_THREAD_ID",
    "CODE_REVIEW_EVENT_ACTOR_LOGIN",
    "CODE_REVIEW_EVENT_ACTOR_ID",
    "CODE_REVIEW_EVENT_HEAD_SHA",
    "CODE_REVIEW_EVENT_SOURCE",
)


def test_context_normalizes_invalid_kind_and_source():
    c = ReviewDecisionEventContext(event_kind="not_a_kind", source="nope")
    assert c.event_kind == "other"
    assert c.source == "full_review"


def test_context_accepts_valid_kind():
    c = ReviewDecisionEventContext(event_kind="reply_added", source="webhook_comment")
    assert c.event_kind == "reply_added"
    assert c.source == "webhook_comment"


def test_context_normalizes_kind_and_source_case_insensitive():
    c = ReviewDecisionEventContext(event_kind="REPLY_ADDED", source="WEBHOOK_COMMENT")
    assert c.event_kind == "reply_added"
    assert c.source == "webhook_comment"


def test_has_audit_fields():
    assert not ReviewDecisionEventContext().has_audit_fields()
    assert ReviewDecisionEventContext(event_name="issue_comment").has_audit_fields()


def test_from_env_returns_none_when_unset():
    with patch.dict(os.environ, dict.fromkeys(_EVENT_ENV_KEYS, ""), clear=False):
        assert review_decision_event_context_from_env() is None


def test_from_env_builds_context():
    with patch.dict(
        os.environ,
        {
            "CODE_REVIEW_EVENT_NAME": "issue_comment",
            "CODE_REVIEW_EVENT_ACTION": "created",
            "CODE_REVIEW_EVENT_KIND": "reply_added",
            "CODE_REVIEW_EVENT_COMMENT_ID": "99",
            "CODE_REVIEW_EVENT_HEAD_SHA": "abc",
            "CODE_REVIEW_EVENT_SOURCE": "webhook_comment",
        },
        clear=False,
    ):
        ctx = review_decision_event_context_from_env()
        assert ctx is not None
        assert ctx.event_name == "issue_comment"
        assert ctx.comment_id == "99"
        assert ctx.head_sha == "abc"


def test_extra_keys_ignored_on_model_validate():
    ctx = ReviewDecisionEventContext.model_validate(
        {"event_name": "e", "unexpected": "ignored"}
    )
    assert ctx.event_name == "e"


def test_event_allows_skip_only_for_reply_added_with_audit():
    assert not event_allows_decision_only_skip_when_bot_not_blocking(None)
    assert not event_allows_decision_only_skip_when_bot_not_blocking(ReviewDecisionEventContext())
    assert not event_allows_decision_only_skip_when_bot_not_blocking(
        ReviewDecisionEventContext(event_kind="comment_deleted", comment_id="1")
    )
    assert event_allows_decision_only_skip_when_bot_not_blocking(
        ReviewDecisionEventContext(event_kind="reply_added", comment_id="1")
    )
    assert event_allows_decision_only_skip_when_bot_not_blocking(
        ReviewDecisionEventContext(event_kind="REPLY_ADDED", comment_id="1")
    )
