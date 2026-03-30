"""Tests for deterministic false-positive filters applied before posting findings."""

from code_review.refinement.filters.contradiction import (
    _message_describes_syntax_or_missing_token_issue,
    filter_obviously_contradicted_findings as _filter_obviously_contradicted_findings,
)
from code_review.schemas.findings import FindingV1

SAMPLE_DIFF = """\
diff --git a/src/main/java/Example.java b/src/main/java/Example.java
--- a/src/main/java/Example.java
+++ b/src/main/java/Example.java
@@ -1148,6 +1148,12 @@
+        String javaType = findJavaType(cm.viewColumn(), sourceProfile);
+        String fieldName = toCamelCase(cm.targetColumn());
+
+        fields.append("    @Column(name = \\"").append(cm.targetColumn()).append("\\"");
+        if (!nullable) fields.append(", nullable = false");
+        fields.append(")\\n");
+        return fieldName;
"""


def _finding(**overrides) -> FindingV1:
    payload = {
        "path": "src/main/java/Example.java",
        "line": 1152,
        "severity": "medium",
        "code": "invalid-java-code",
        "message": "Default message",
    }
    payload.update(overrides)
    return FindingV1(**payload)


def test_identical_syntax_fix_is_dropped_as_contradicted():
    finding = _finding(
        line=1152,
        message=(
            "The @Column annotation is missing a comma before `nullable = false`, "
            "which will result in invalid Java code."
        ),
        suggested_patch='if (!nullable) fields.append(", nullable = false");',
    )

    result = _filter_obviously_contradicted_findings([finding], SAMPLE_DIFF)

    assert result == []


def test_missing_comma_before_fragment_dropped_when_fragment_already_prefixed():
    finding = _finding(
        line=1153,
        message="The builder is missing a comma before `nullable = false`.",
    )

    result = _filter_obviously_contradicted_findings([finding], SAMPLE_DIFF)

    assert result == []


def test_identical_patch_is_stripped_for_non_syntax_message():
    finding = _finding(
        line=1151,
        message="Use a constant for the annotation prefix to avoid repetition.",
        suggested_patch=(
            'fields.append("    @Column(name = \\"").append(cm.targetColumn()).append("\\"");'
        ),
    )

    result = _filter_obviously_contradicted_findings([finding], SAMPLE_DIFF)

    assert len(result) == 1
    assert result[0].suggested_patch is None


def test_multiline_patch_with_matching_first_line_is_not_treated_as_contradicted():
    finding = _finding(
        line=1151,
        message="The annotation builder is malformed and will not compile.",
        suggested_patch=(
            'fields.append("    @Column(name = \\"").append(cm.targetColumn()).append("\\"");\n'
            'fields.append(", nullable = false");'
        ),
    )

    result = _filter_obviously_contradicted_findings([finding], SAMPLE_DIFF)

    assert len(result) == 1
    assert result[0] == finding


def test_syntax_patch_with_different_string_literal_spacing_is_not_dropped():
    diff_text = """\
diff --git a/src/main/java/Example.java b/src/main/java/Example.java
--- a/src/main/java/Example.java
+++ b/src/main/java/Example.java
@@ -10,1 +10,1 @@
+logger.info("user deleted");
"""
    finding = _finding(
        line=10,
        message='This string literal is malformed and will not compile.',
        suggested_patch='logger.info("userdeleted");',
    )

    result = _filter_obviously_contradicted_findings([finding], diff_text)

    assert len(result) == 1
    assert result[0] == finding


def test_message_describes_missing_quoted_fragment_before_keyword():
    assert _message_describes_syntax_or_missing_token_issue(
        'The statement is missing ")" before "{" and will not compile.'
    )


def test_message_describes_missing_backticked_fragment_after_keyword():
    assert _message_describes_syntax_or_missing_token_issue(
        "This is missing `]` after the generic type declaration."
    )


def test_message_ignores_missing_quoted_fragment_without_position_keyword():
    assert not _message_describes_syntax_or_missing_token_issue(
        'The parser is missing ")" near the next token.'
    )
