"""Minimal local evaluation harness for checked-in corpora."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from code_review.agent import create_reply_dismissal_agent, create_review_agent
from code_review.agent.reply_dismissal_agent import reply_dismissal_verdict_from_llm_text
from code_review.evals.corpus import (
    GoldenPrReviewCase,
    ReplyDismissalEvalCase,
    load_golden_pr_review_cases,
    load_reply_dismissal_eval_cases,
)
from code_review.orchestration.execution import run_agent_and_collect_response
from code_review.orchestration_deps import _findings_from_response
from code_review.providers.base import ProviderCapabilities
from code_review.standards import get_review_standards

EvalExecutionMode = Literal["parser", "adk"]


class _EvalProvider:
    """Minimal provider used only to satisfy agent construction for eval runs."""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            resolvable_comments=False,
            supports_suggestions=False,
        )


@dataclass(frozen=True)
class EvalCaseResult:
    """Result for one local evaluation case."""

    case_id: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EvalRunSummary:
    """Summary of a local evaluation run."""

    suite_name: str
    total: int
    passed: int
    failed: int
    results: list[EvalCaseResult]


def run_local_golden_pr_review_eval(*, execution: EvalExecutionMode = "parser") -> EvalRunSummary:
    """Evaluate the checked-in golden PR review cases."""
    if execution not in {"parser", "adk"}:
        raise ValueError(f"Unknown eval execution mode: {execution}")
    cases = load_golden_pr_review_cases()
    if execution == "parser":
        results = [_run_one_golden_pr_review_case_parser(case) for case in cases]
    else:
        results = [_run_one_golden_pr_review_case_adk(case) for case in cases]
    return _build_summary(f"golden_pr_review[{execution}]", results)


def run_local_reply_dismissal_eval(*, execution: EvalExecutionMode = "parser") -> EvalRunSummary:
    """Evaluate the checked-in reply-dismissal cases."""
    if execution not in {"parser", "adk"}:
        raise ValueError(f"Unknown eval execution mode: {execution}")
    cases = load_reply_dismissal_eval_cases()
    if execution == "parser":
        results = [_run_one_reply_dismissal_case_parser(case) for case in cases]
    else:
        results = [_run_one_reply_dismissal_case_adk(case) for case in cases]
    return _build_summary(f"reply_dismissal[{execution}]", results)


def _run_one_golden_pr_review_case_parser(case: GoldenPrReviewCase) -> EvalCaseResult:
    actual = _findings_from_response(case.expected.model_dump_json())
    expected = case.expected.findings
    passed = [finding.model_dump() for finding in actual] == [
        finding.model_dump() for finding in expected
    ]
    detail = "exact parser round-trip match" if passed else "parser output differed from expected"
    return EvalCaseResult(case_id=case.case_id, passed=passed, detail=detail)


def _run_one_reply_dismissal_case_parser(case: ReplyDismissalEvalCase) -> EvalCaseResult:
    actual = reply_dismissal_verdict_from_llm_text(case.expected.model_dump_json())
    passed = actual is not None and actual.model_dump() == case.expected.model_dump()
    detail = "exact parser round-trip match" if passed else "parser output differed from expected"
    return EvalCaseResult(case_id=case.case_id, passed=passed, detail=detail)


def _run_one_golden_pr_review_case_adk(case: GoldenPrReviewCase) -> EvalCaseResult:
    try:
        agent = create_review_agent(
            _EvalProvider(),
            get_review_standards(case.language, None),
        )
        response_text = _run_agent_text(
            agent,
            _golden_pr_review_user_message(case),
        )
        actual = _findings_from_response(response_text)
        expected = case.expected.findings
        passed = [finding.model_dump() for finding in actual] == [
            finding.model_dump() for finding in expected
        ]
        detail = "exact ADK agent match" if passed else "ADK agent output differed from expected"
        return EvalCaseResult(case_id=case.case_id, passed=passed, detail=detail)
    except Exception as exc:  # pragma: no cover - exercised via mocked failure cases if added later
        return EvalCaseResult(case_id=case.case_id, passed=False, detail=f"ADK eval failed: {exc}")


def _run_one_reply_dismissal_case_adk(case: ReplyDismissalEvalCase) -> EvalCaseResult:
    try:
        agent = create_reply_dismissal_agent()
        response_text = _run_agent_text(agent, case.user_message)
        actual = reply_dismissal_verdict_from_llm_text(response_text)
        passed = actual is not None and actual.model_dump() == case.expected.model_dump()
        detail = "exact ADK agent match" if passed else "ADK agent output differed from expected"
        return EvalCaseResult(case_id=case.case_id, passed=passed, detail=detail)
    except Exception as exc:  # pragma: no cover - exercised via mocked failure cases if added later
        return EvalCaseResult(case_id=case.case_id, passed=False, detail=f"ADK eval failed: {exc}")


def _run_agent_text(agent, user_text: str) -> str:
    """Execute one agent run and return the concatenated final response text."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="code_review_eval",
        session_service=session_service,
        auto_create_session=True,
    )
    content = types.Content(role="user", parts=[types.Part(text=user_text)])
    return run_agent_and_collect_response(runner, session_service, "eval-session", content)


def _golden_pr_review_user_message(case: GoldenPrReviewCase) -> str:
    """Build the embedded-diff user message for a golden PR review eval case."""
    return (
        f"Evaluate golden PR review case: {case.case_id}\n"
        f"Description: {case.description}\n\n"
        "```diff\n"
        f"{case.pr_diff.rstrip()}\n"
        "```"
    )


def _build_summary(suite_name: str, results: list[EvalCaseResult]) -> EvalRunSummary:
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    return EvalRunSummary(
        suite_name=suite_name,
        total=total,
        passed=passed,
        failed=total - passed,
        results=results,
    )
