"""Tests for reference extraction from PR text."""

import pytest

from code_review.context.extractor import ReferenceType, extract_references


class TestGitHubIssues:
    def test_hash_shorthand(self):
        refs = extract_references("Fixes #42 and #100", owner="acme", repo="myapp")
        keys = {r.key for r in refs}
        assert "github_issue:acme/myapp/42" in keys
        assert "github_issue:acme/myapp/100" in keys

    def test_gh_prefix(self):
        refs = extract_references("GH-7 is related", owner="acme", repo="myapp")
        assert any(r.identifier == "acme/myapp/7" for r in refs)

    def test_hash_shorthand_no_context(self):
        # Without owner/repo, shorthand #N should be ignored (can't build identifier)
        refs = extract_references("See #42 for details")
        assert not any(r.ref_type == ReferenceType.GITHUB_ISSUE for r in refs)

    def test_full_github_url(self):
        text = "See https://github.com/org/project/issues/99 for details."
        refs = extract_references(text)
        assert any(r.identifier == "org/project/99" for r in refs)

    def test_deduplication(self):
        text = "Fixes #5, also #5 again"
        refs = extract_references(text, owner="o", repo="r")
        issue_refs = [r for r in refs if r.ref_type == ReferenceType.GITHUB_ISSUE]
        assert len(issue_refs) == 1

    def test_url_and_shorthand_same_issue_dedup(self):
        text = "#10 and https://github.com/o/r/issues/10"
        refs = extract_references(text, owner="o", repo="r")
        issue_refs = [r for r in refs if r.ref_type == ReferenceType.GITHUB_ISSUE]
        assert len(issue_refs) == 1


class TestJiraKeys:
    def test_bare_key(self):
        refs = extract_references("Implements PROJ-123")
        jira = [r for r in refs if r.ref_type == ReferenceType.JIRA]
        assert any(r.identifier == "PROJ-123" for r in jira)

    def test_multi_word_key(self):
        refs = extract_references("Fix MYAPP-42 and CORE-7")
        jira = [r for r in refs if r.ref_type == ReferenceType.JIRA]
        keys = {r.identifier for r in jira}
        assert "MYAPP-42" in keys
        assert "CORE-7" in keys

    def test_browse_url(self):
        text = "https://company.atlassian.net/browse/PROJ-456"
        refs = extract_references(text)
        jira = [r for r in refs if r.ref_type == ReferenceType.JIRA]
        assert any(r.identifier == "PROJ-456" for r in jira)

    def test_project_key_filter_allows(self):
        refs = extract_references("PROJ-10 CORE-5", jira_project_keys=["PROJ"])
        jira = [r for r in refs if r.ref_type == ReferenceType.JIRA]
        keys = {r.identifier for r in jira}
        assert "PROJ-10" in keys
        assert "CORE-5" not in keys

    def test_project_key_filter_blocks(self):
        refs = extract_references("RANDOM-99", jira_project_keys=["PROJ"])
        assert not any(r.ref_type == ReferenceType.JIRA for r in refs)

    def test_deduplication(self):
        refs = extract_references("PROJ-1 https://x.atlassian.net/browse/PROJ-1")
        jira = [r for r in refs if r.ref_type == ReferenceType.JIRA]
        assert len(jira) == 1

    def test_lowercase_not_matched(self):
        # Jira keys must start with uppercase letter
        refs = extract_references("proj-10 should not match")
        assert not any(r.ref_type == ReferenceType.JIRA for r in refs)


class TestConfluenceUrls:
    def test_spaces_url_with_page_id(self):
        url = "https://company.atlassian.net/wiki/spaces/ENG/pages/12345678/My+Page"
        refs = extract_references(url)
        conf = [r for r in refs if r.ref_type == ReferenceType.CONFLUENCE]
        assert len(conf) == 1
        assert conf[0].identifier == "12345678"

    def test_display_url(self):
        url = "https://company.atlassian.net/wiki/display/ENG/My+Page"
        refs = extract_references(url)
        conf = [r for r in refs if r.ref_type == ReferenceType.CONFLUENCE]
        assert len(conf) == 1

    def test_deduplication(self):
        url = "https://company.atlassian.net/wiki/spaces/ENG/pages/99/Page"
        refs = extract_references(f"{url} and also {url}")
        conf = [r for r in refs if r.ref_type == ReferenceType.CONFLUENCE]
        assert len(conf) == 1

    def test_no_false_positives(self):
        refs = extract_references("https://github.com/org/repo/issues/5")
        assert not any(r.ref_type == ReferenceType.CONFLUENCE for r in refs)


class TestEmptyAndEdgeCases:
    def test_empty_string(self):
        assert extract_references("") == []

    def test_no_references(self):
        refs = extract_references("Just a normal commit message with no references.")
        assert refs == []

    def test_mixed_sources(self):
        text = (
            "Implements PROJ-10, see #5 and "
            "https://company.atlassian.net/wiki/spaces/ENG/pages/111/Spec"
        )
        refs = extract_references(text, owner="acme", repo="api", jira_project_keys=["PROJ"])
        types_found = {r.ref_type for r in refs}
        assert ReferenceType.JIRA in types_found
        assert ReferenceType.GITHUB_ISSUE in types_found
        assert ReferenceType.CONFLUENCE in types_found
