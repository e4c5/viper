"""Tests for resolved status and fingerprint-based ignore (Phase 2)."""

from code_review.providers.base import ProviderCapabilities, ReviewComment


def test_review_comment_has_resolved():
    c = ReviewComment(id="1", path="foo.py", line=10, body="[Critical] Bug.", resolved=False)
    assert c.resolved is False
    c2 = ReviewComment(id="2", path="a.py", line=1, body="Done", resolved=True)
    assert c2.resolved is True


def test_provider_capabilities_resolvable():
    caps = ProviderCapabilities(resolvable_comments=True, supports_suggestions=False)
    assert caps.resolvable_comments is True
    caps2 = ProviderCapabilities(resolvable_comments=False, supports_suggestions=False)
    assert caps2.resolvable_comments is False
