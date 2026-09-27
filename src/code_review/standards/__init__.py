"""Language/framework detection and review standards."""

from code_review.standards.detector import (
    DetectedContext,
    detect_from_paths,
    detect_from_paths_and_content,
    detect_from_paths_per_folder_root,
    detect_review_contexts,
)
from code_review.standards.prompts import (
    get_review_standards,
    get_review_standards_multi,
)

__all__ = [
    "DetectedContext",
    "detect_from_paths",
    "detect_from_paths_and_content",
    "detect_from_paths_per_folder_root",
    "detect_review_contexts",
    "get_review_standards",
    "get_review_standards_multi",
]
