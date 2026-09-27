"""AI-driven code review agent for CI/CD pipelines.

The names in ``__all__`` are the supported public API (semver-covered):
everything else in this package is internal and may change without notice.

Imports are lazy (module-level ``__getattr__``) so that ``import code_review``
stays cheap and does not pull in heavy dependencies such as Google ADK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "1.1.2"

__all__ = [
    "__version__",
    "run_review",
    "FindingV1",
    "ReviewDecisionConfig",
    "ReviewDecisionEventContext",
    "SCMConfig",
    "LLMConfig",
    "CodeReviewAppConfig",
    "get_scm_config",
    "get_llm_config",
    "get_code_review_app_config",
    "get_provider",
    "configure_logging",
    "LOG_LEVEL_ENV",
]

if TYPE_CHECKING:
    from code_review.config import (
        CodeReviewAppConfig,
        LLMConfig,
        SCMConfig,
        get_code_review_app_config,
        get_llm_config,
        get_scm_config,
    )
    from code_review.logging_config import LOG_LEVEL_ENV, configure_logging
    from code_review.providers import get_provider
    from code_review.runner import run_review
    from code_review.schemas.findings import FindingV1
    from code_review.schemas.review_decision_event import (
        ReviewDecisionConfig,
        ReviewDecisionEventContext,
    )

_LAZY_ATTRS = {
    "run_review": ("code_review.runner", "run_review"),
    "FindingV1": ("code_review.schemas.findings", "FindingV1"),
    "ReviewDecisionConfig": (
        "code_review.schemas.review_decision_event",
        "ReviewDecisionConfig",
    ),
    "ReviewDecisionEventContext": (
        "code_review.schemas.review_decision_event",
        "ReviewDecisionEventContext",
    ),
    "SCMConfig": ("code_review.config", "SCMConfig"),
    "LLMConfig": ("code_review.config", "LLMConfig"),
    "CodeReviewAppConfig": ("code_review.config", "CodeReviewAppConfig"),
    "get_scm_config": ("code_review.config", "get_scm_config"),
    "get_llm_config": ("code_review.config", "get_llm_config"),
    "get_code_review_app_config": ("code_review.config", "get_code_review_app_config"),
    "get_provider": ("code_review.providers", "get_provider"),
    "configure_logging": ("code_review.logging_config", "configure_logging"),
    "LOG_LEVEL_ENV": ("code_review.logging_config", "LOG_LEVEL_ENV"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value
