"""Unit tests for GitHub/Gitea-style bot blocking from PR review lists."""

from code_review.providers.bot_blocking_common import blocking_state_from_github_style_reviews


def _review(rid: int, state: str) -> dict:
    return {"id": rid, "state": state, "user": {"login": "bot"}}


def test_pending_does_not_mask_earlier_changes_requested():
    reviews = [
        _review(1, "CHANGES_REQUESTED"),
        _review(2, "PENDING"),
    ]
    assert blocking_state_from_github_style_reviews(reviews, token_login_lower="bot") == "BLOCKING"


def test_only_pending_is_not_blocking():
    reviews = [_review(1, "PENDING")]
    assert (
        blocking_state_from_github_style_reviews(reviews, token_login_lower="bot")
        == "NOT_BLOCKING"
    )


def test_approved_after_pending_uses_approved():
    reviews = [
        _review(1, "CHANGES_REQUESTED"),
        _review(2, "PENDING"),
        _review(3, "APPROVED"),
    ]
    assert (
        blocking_state_from_github_style_reviews(reviews, token_login_lower="bot")
        == "NOT_BLOCKING"
    )
