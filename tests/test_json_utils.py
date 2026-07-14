"""Tests for JSON extraction helpers."""

from code_review.json_utils import iter_json_candidates


def test_iter_json_candidates_skips_non_json_fence_and_returns_later_json_fence():
    text = (
        "Before\n"
        "```python\n"
        "print('not json')\n"
        "```\n"
        "Between\n"
        "```json\n"
        '{"findings":[{"path":"a.py","line":1,"severity":"low","code":"x","message":"m"}]}'
        "\n```\n"
    )

    candidates = list(iter_json_candidates(text))

    assert candidates[0] == (
        '{"findings":[{"path":"a.py","line":1,"severity":"low","code":"x","message":"m"}]}'
    )


def test_iter_json_candidates_finds_unfenced_json_after_reasoning_prose():
    """Reasoning models (e.g. DeepSeek) often emit chain-of-thought prose
    followed by a bare, unfenced JSON object rather than JSON-only output or a
    fenced block. This is a real captured response from deepseek-v4-pro."""
    text = (
        "We need to review this diff for potential issues. The diff is a "
        "documentation change: in CONFIGURATION-REFERENCE.md, they added "
        "`deepseek` to the list of allowed values for LLM_PROVIDER. That's a "
        "simple doc update. There's no code change. No bugs, security, etc. "
        "So I'll output an empty findings array.\n"
        '{"findings": []}'
    )

    candidates = list(iter_json_candidates(text, include_embedded_objects=True))

    assert '{"findings": []}' in candidates


def test_iter_json_candidates_prefers_last_balanced_object():
    """When reasoning prose itself contains a JSON-shaped example, the actual
    final answer (last balanced object) should be tried first."""
    text = (
        'Consider an example like {"findings": [{"path": "x"}]} for context. '
        'Final answer: {"findings": []}'
    )

    candidates = list(iter_json_candidates(text, include_embedded_objects=True))

    assert candidates.index('{"findings": []}') < candidates.index(
        '{"findings": [{"path": "x"}]}'
    )


def test_iter_json_candidates_ignores_braces_inside_quoted_strings():
    text = 'Reasoning mentions a "{not json}" snippet in prose. {"findings": []}'

    candidates = list(iter_json_candidates(text, include_embedded_objects=True))

    assert '{"findings": []}' in candidates
    assert '{not json}' not in candidates


def test_iter_json_candidates_recovers_from_unmatched_brace_before_valid_object():
    """An earlier stray/unmatched '{' in prose (e.g. reasoning that mentions
    a brace without closing it) must not prevent a later, well-formed JSON
    object from being extracted."""
    text = 'Reasoning has a stray { brace here. Final answer: {"findings": []}'

    candidates = list(iter_json_candidates(text, include_embedded_objects=True))

    assert '{"findings": []}' in candidates


def test_iter_json_candidates_embedded_objects_off_by_default():
    """Callers like reply-dismissal verdict parsing deliberately want to reject
    messy prefixed/suffixed text rather than salvage an embedded object from it."""
    text = 'Prefix {"findings": []} suffix'

    candidates = list(iter_json_candidates(text))

    assert '{"findings": []}' not in candidates
