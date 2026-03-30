"""Finding refinement pipeline."""

from __future__ import annotations

from code_review.refinement.filters.anchor_relocator import relocate_findings_by_anchor
from code_review.refinement.filters.contradiction import filter_obviously_contradicted_findings
from code_review.refinement.filters.patch_validator import validate_suggested_patches
from code_review.refinement.filters.self_retraction import filter_self_retracted_findings
from code_review.schemas.findings import FindingV1


class FindingRefinementPipeline:
    def run(self, findings: list[FindingV1], diff_text: str) -> list[FindingV1]:
        findings = relocate_findings_by_anchor(findings, diff_text)
        findings = filter_self_retracted_findings(findings)
        findings = filter_obviously_contradicted_findings(findings, diff_text)
        findings = validate_suggested_patches(findings, diff_text)
        return findings
