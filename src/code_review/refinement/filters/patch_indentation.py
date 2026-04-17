"""Patch indentation normalization filter.

GitHub (and GitLab, Gitea, Bitbucket) 'Suggested change' blocks replace the anchored
line(s) verbatim.  For the replacement to be syntactically correct the patch must carry
the *same* leading whitespace as the original line — otherwise Python, YAML, and any
other indentation-sensitive language will break.

LLMs frequently omit leading whitespace from ``suggested_patch`` values, especially for
code that lives inside a class, function, or control-flow block.  This filter detects
that mismatch and re-prefixes each patch line with the correct indent, derived from the
actual diff line stored in the line index.

Only single-line patches and multiline patches whose first content line has zero leading
whitespace (while the actual diff line has non-zero leading whitespace) are adjusted.
Patches that already carry correct indentation (or whose indentation exceeds the actual
line — an unusual but valid case, e.g. the fix adds an extra nesting level) are left
untouched.

Strategy
--------
1. Look up the actual diff content for ``finding.line`` from the pre-built line index.
2. Measure the indent of the first non-empty line of the actual diff content
   (``actual_indent``).
3. Measure the indent of the first non-empty line of ``suggested_patch``
   (``patch_indent``).
4. If ``actual_indent`` is non-empty and ``patch_indent`` is empty (the common
   failure mode), prepend ``actual_indent`` to every line of the patch.

Edge-cases that are deliberately *not* corrected:
- ``patch_indent`` is already non-empty (assume the LLM got it right or intentionally
  changed the nesting level).
- The actual diff line has no leading whitespace (top-level code; nothing to fix).
- The patch is ``None`` or empty.
"""

from __future__ import annotations

import logging

from code_review.diff.line_index import build_diff_line_index
from code_review.diff.utils import normalize_path
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)


def _leading_whitespace(line: str) -> str:
    """Return the leading whitespace characters of *line*."""
    return line[: len(line) - len(line.lstrip())]


def _first_nonempty_line(text: str) -> str | None:
    """Return the first non-empty line of *text*, or ``None``."""
    for ln in text.splitlines():
        if ln.strip():
            return ln
    return None


def _apply_indent_prefix(patch: str, prefix: str) -> str:
    """Prepend *prefix* to every non-empty line of *patch*.

    Empty lines (blank lines) are left as-is so we do not add invisible trailing
    whitespace inside suggestion blocks.
    """
    result: list[str] = []
    for ln in patch.splitlines():
        if ln.strip():
            result.append(prefix + ln)
        else:
            result.append(ln)
    return "\n".join(result)


def normalize_patch_indentation(
    findings: list[FindingV1],
    diff_text: str,
) -> list[FindingV1]:
    """Re-indent ``suggested_patch`` values that are missing their leading whitespace.

    This is a lossless, conservative correction: we only add missing indent (prefix)
    when the actual diff line has leading whitespace and the patch's first non-empty
    line has *none*.  The patch content itself is never otherwise modified.

    Findings without a ``suggested_patch`` are returned unchanged.
    """
    if not diff_text or not findings:
        return findings

    line_index = build_diff_line_index(diff_text)
    result: list[FindingV1] = []

    for f in findings:
        if not f.suggested_patch:
            result.append(f)
            continue

        norm_path = normalize_path(f.path)
        actual_content = line_index.get((norm_path, f.line))
        if actual_content is None:
            result.append(f)
            continue

        actual_first = _first_nonempty_line(actual_content)
        if actual_first is None:
            result.append(f)
            continue

        actual_indent = _leading_whitespace(actual_first)
        if not actual_indent:
            # Top-level code — nothing to fix.
            result.append(f)
            continue

        patch_first = _first_nonempty_line(f.suggested_patch)
        if patch_first is None:
            result.append(f)
            continue

        patch_indent = _leading_whitespace(patch_first)
        if patch_indent:
            # Patch already has leading whitespace — trust the LLM.
            result.append(f)
            continue

        # The patch is missing its indent prefix.  Re-add it.
        fixed_patch = _apply_indent_prefix(f.suggested_patch, actual_indent)
        logger.info(
            "normalize_patch_indentation: fixed missing indent (%r) on %s:%d",
            actual_indent,
            f.path,
            f.line,
        )
        result.append(f.model_copy(update={"suggested_patch": fixed_patch}))

    return result
