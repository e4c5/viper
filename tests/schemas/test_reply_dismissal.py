"""Tests for ReplyDismissalVerdictV1."""

import pytest
from pydantic import ValidationError

from code_review.schemas.reply_dismissal import ReplyDismissalVerdictV1


def test_agreed_allows_empty_reply_text():
    v = ReplyDismissalVerdictV1(verdict="agreed", reply_text="")
    assert v.verdict == "agreed"
    assert v.reply_text == ""


def test_disagreed_requires_non_empty_reply_text():
    with pytest.raises(ValueError, match="reply_text"):
        ReplyDismissalVerdictV1(verdict="disagreed", reply_text="")
    v = ReplyDismissalVerdictV1(verdict="disagreed", reply_text="  still open  ")
    assert v.reply_text == "  still open  "


def test_extra_keys_ignored():
    v = ReplyDismissalVerdictV1.model_validate(
        {"verdict": "agreed", "reply_text": "", "noise": 1}
    )
    assert v.verdict == "agreed"


def test_version_literal_rejects_other_strings():
    v = ReplyDismissalVerdictV1.model_validate(
        {"version": "1", "verdict": "agreed", "reply_text": ""}
    )
    assert v.version == "1"
    with pytest.raises(ValidationError):
        ReplyDismissalVerdictV1.model_validate(
            {"version": "2", "verdict": "agreed", "reply_text": ""}
        )
