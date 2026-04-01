"""Suggested patch validation filter."""

from __future__ import annotations

import logging

from code_review.diff.line_index import build_diff_line_index
from code_review.diff.utils import normalize_path
from code_review.refinement.filters.contradiction import _patch_tokens
from code_review.schemas.findings import FindingV1

logger = logging.getLogger(__name__)


def validate_suggested_patches(
    findings: list[FindingV1],
    diff_text: str,
) -> list[FindingV1]:
    """Strip suggested_patch from findings where the patch doesn't match the anchored line.

    For each finding with a suggested_patch, look up the actual content of finding.line
    in the diff. If there is no meaningful token overlap between the patch's first line
    and the actual diff line, the patch is almost certainly misplaced (the LLM named a
    visible line but wrote a patch for a completely different piece of code).

    In that case, clear suggested_patch and log a warning so the finding is still posted
    as a plain comment rather than an incorrectly-placed suggestion block.

    Findings without a suggested_patch are returned unchanged.
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

        patch_first_line = next(
            (ln.strip() for ln in f.suggested_patch.splitlines() if ln.strip()), ""
        )

        actual_tokens = _patch_tokens(actual_content)
        patch_tokens = _patch_tokens(patch_first_line)

        if not actual_tokens or not patch_tokens:
            result.append(f)
            continue

        overlap = actual_tokens & patch_tokens
        is_plausible = bool(overlap) or len(actual_content) <= 5

        if not is_plausible:
            logger.warning(
                "Stripping misplaced suggested_patch from finding %s:%d: "
                "patch first line %r has no token overlap with actual diff line %r",
                f.path,
                f.line,
                patch_first_line,
                actual_content,
            )
            f = f.model_copy(update={"suggested_patch": None})

        result.append(f)
    return result
