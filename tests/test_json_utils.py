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
