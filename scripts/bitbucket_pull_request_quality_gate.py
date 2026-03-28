#!/usr/bin/env python3
"""Inspect whether Bitbucket Server/DC PR comments and tasks count toward Viper's quality gate.

Usage:
  python scripts/bitbucket_pull_request_quality_gate.py comment <project> <repo> <pull_request_id> <comment_id>
  python scripts/bitbucket_pull_request_quality_gate.py pr <project> <repo> <pull_request_id>

Authentication:
  SE_USER and SE_PASSWORD are read from the environment; for local repo usage
  this script also auto-loads the repo-root .env when present.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

REPO_ROOT = SCRIPT_DIR.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bitbucket_pull_request_api import (
    get_pull_request_comment,
    list_pull_request_activities,
    list_pull_request_comments,
    list_pull_request_tasks,
    load_script_credentials,
)
from code_review.formatters.comment import infer_severity_from_comment_body
from code_review.providers.base import BotAttributionIdentity, ReviewComment
from code_review.providers.bitbucket_server import (
    BitbucketServerProvider,
    bitbucket_server_persisted_dismissed_root_ids,
)

PROJECT_KEY_HELP = "Bitbucket project key, for example PRJ"
REPO_SLUG_HELP = "Bitbucket repository slug"
PULL_REQUEST_ID_HELP = "Pull request id"


def _is_truthy_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _comment_parent_id(comment: dict[str, Any]) -> str | None:
    for key in ("parentComment", "parent"):
        parent = comment.get(key)
        if isinstance(parent, dict) and parent.get("id") is not None:
            return str(parent["id"])
    return None


def comment_is_outdated(comment: dict[str, Any]) -> bool:
    """Match the provider's orphaned/outdated guard used by the quality gate."""
    anchor = comment.get("anchor")
    if not isinstance(anchor, dict):
        return False
    for key in ("orphaned", "isOrphaned"):
        if _is_truthy_flag(anchor.get(key)):
            return True
    for key in ("state", "anchorState"):
        state = str(anchor.get(key) or "").strip().upper()
        if state == "ORPHANED":
            return True
    return False


def comment_gate_status(comment: dict[str, Any]) -> tuple[bool, str]:
    """Return whether the comment counts for the quality gate and why."""
    return _comment_gate_status_with_provider_context(comment)


def task_gate_status(task: dict[str, Any]) -> tuple[bool, str]:
    """Return whether the task counts for the quality gate and why."""
    state = str(task.get("state") or "").strip().upper()
    body = str(task.get("text") or "").strip()
    if state in ("RESOLVED", "DECLINED"):
        return False, "resolved_or_declined"
    if not body:
        return False, "empty_body"
    return True, "open"


def build_comment_report(comment: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-friendly quality-gate report for one PR comment."""
    counts, reason = comment_gate_status(comment)
    anchor = comment.get("anchor") if isinstance(comment.get("anchor"), dict) else {}
    body = str(comment.get("text") or comment.get("body") or "")
    return {
        "kind": "comment",
        "comment_id": str(comment.get("id") or ""),
        "parent_comment_id": _comment_parent_id(comment),
        "state": str(comment.get("state") or ""),
        "counts_for_quality_gate": counts,
        "quality_gate_reason": reason,
        "inferred_severity": infer_severity_from_comment_body(body),
        "path": str(anchor.get("path") or ""),
        "line": int(anchor.get("line") or 0),
        "anchor_orphaned": comment_is_outdated(comment),
        "anchor_state": str(anchor.get("state") or anchor.get("anchorState") or ""),
        "body": body,
    }


def _provider_comment_gate_context(
    comments: list[dict[str, Any]],
    *,
    bot_login: str,
) -> tuple[dict[str, ReviewComment], frozenset[str]]:
    review_comments = [
        review_comment
        for comment in comments
        if (
            review_comment := BitbucketServerProvider._bbs_review_comment_from_comment_dict(comment)
        )
        is not None
    ]
    by_id = {str(comment.id or "").strip(): comment for comment in review_comments if (comment.id or "").strip()}
    bot = BotAttributionIdentity(login=bot_login, slug=bot_login)
    dismissed_stable_ids = bitbucket_server_persisted_dismissed_root_ids(review_comments, bot)
    return by_id, dismissed_stable_ids


def _load_provider_comment_gate_context_for_pr(
    project_key: str,
    repo_slug: str,
    pull_request_id: int,
    *,
    username: str,
    password: str,
) -> tuple[list[dict[str, Any]], dict[str, ReviewComment], frozenset[str]]:
    comments = list_pull_request_comments(
        project_key,
        repo_slug,
        pull_request_id,
        username=username,
        password=password,
    )
    review_comments_by_id, dismissed_stable_ids = _provider_comment_gate_context(
        comments,
        bot_login=username,
    )
    return comments, review_comments_by_id, dismissed_stable_ids


def _comment_gate_status_with_provider_context(
    comment: dict[str, Any],
    *,
    review_comments_by_id: dict[str, ReviewComment] | None = None,
    dismissed_stable_ids: frozenset[str] | None = None,
) -> tuple[bool, str]:
    """Return the same open/closed decision used by the production provider when possible."""
    review_comment = BitbucketServerProvider._bbs_review_comment_from_comment_dict(comment)
    if review_comment is None:
        state = str(comment.get("state") or "").strip().upper()
        body = str(comment.get("text") or comment.get("body") or "").strip()
        if state == "RESOLVED":
            return False, "resolved"
        if comment_is_outdated(comment):
            return False, "outdated_or_orphaned"
        if not body:
            return False, "empty_body"
        return True, "open"

    comment_id = (review_comment.id or "").strip()
    if comment_id and review_comments_by_id:
        root_id = BitbucketServerProvider._bbs_thread_root_comment_id(review_comments_by_id, comment_id)
        if f"comment:{root_id}" in (dismissed_stable_ids or frozenset()):
            return False, "dismissed_thread"
    if review_comment.resolved:
        return False, "resolved"
    if review_comment.outdated:
        return False, "outdated_or_orphaned"
    if not (review_comment.body or "").strip():
        return False, "empty_body"
    return True, "open"


def _build_comment_report_with_provider_context(
    comment: dict[str, Any],
    *,
    review_comments_by_id: dict[str, ReviewComment],
    dismissed_stable_ids: frozenset[str],
) -> dict[str, Any]:
    counts, reason = _comment_gate_status_with_provider_context(
        comment,
        review_comments_by_id=review_comments_by_id,
        dismissed_stable_ids=dismissed_stable_ids,
    )
    anchor = comment.get("anchor") if isinstance(comment.get("anchor"), dict) else {}
    body = str(comment.get("text") or comment.get("body") or "")
    return {
        "kind": "comment",
        "comment_id": str(comment.get("id") or ""),
        "parent_comment_id": _comment_parent_id(comment),
        "state": str(comment.get("state") or ""),
        "counts_for_quality_gate": counts,
        "quality_gate_reason": reason,
        "inferred_severity": infer_severity_from_comment_body(body),
        "path": str(anchor.get("path") or ""),
        "line": int(anchor.get("line") or 0),
        "anchor_orphaned": comment_is_outdated(comment),
        "anchor_state": str(anchor.get("state") or anchor.get("anchorState") or ""),
        "body": body,
    }


def build_task_report(task: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-friendly quality-gate report for one PR task."""
    counts, reason = task_gate_status(task)
    body = str(task.get("text") or "")
    return {
        "kind": "task",
        "task_id": str(task.get("id") or ""),
        "state": str(task.get("state") or ""),
        "counts_for_quality_gate": counts,
        "quality_gate_reason": reason,
        "inferred_severity": infer_severity_from_comment_body(body),
        "body": body,
    }


def build_pr_gate_report(
    project_key: str,
    repo_slug: str,
    pull_request_id: int,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Return a PR-wide snapshot of comments/tasks that currently affect the gate."""
    comments, review_comments_by_id, dismissed_stable_ids = _load_provider_comment_gate_context_for_pr(
        project_key,
        repo_slug,
        pull_request_id,
        username=username,
        password=password,
    )
    comment_reports = [
        _build_comment_report_with_provider_context(
            comment,
            review_comments_by_id=review_comments_by_id,
            dismissed_stable_ids=dismissed_stable_ids,
        )
        for comment in comments
    ]
    task_reports = [
        build_task_report(task)
        for task in list_pull_request_tasks(
            project_key,
            repo_slug,
            pull_request_id,
            username=username,
            password=password,
        )
    ]
    gate_items = [item for item in [*comment_reports, *task_reports] if item["counts_for_quality_gate"]]
    high_count = sum(1 for item in gate_items if item["inferred_severity"] == "high")
    medium_count = sum(1 for item in gate_items if item["inferred_severity"] == "medium")
    return {
        "project_key": project_key,
        "repo_slug": repo_slug,
        "pull_request_id": pull_request_id,
        "open_high_count": high_count,
        "open_medium_count": medium_count,
        "counted_items": gate_items,
        "comments": comment_reports,
        "tasks": task_reports,
    }


def _activity_contains_comment_id(node: Any, wanted: str) -> bool:
    """Return True when a nested activity/comment structure references ``wanted``."""
    if isinstance(node, dict):
        if str(node.get("id") or "").strip() == wanted:
            return True
        for value in node.values():
            if _activity_contains_comment_id(value, wanted):
                return True
        return False
    if isinstance(node, list):
        return any(_activity_contains_comment_id(item, wanted) for item in node)
    return False


def _activity_comment_tree(node: Any) -> dict[str, Any] | None:
    """Return the raw comment tree attached to one activity, if present."""
    if not isinstance(node, dict):
        return None
    comment = node.get("comment")
    if isinstance(comment, dict):
        return comment
    return None


def build_comment_raw_report(
    project_key: str,
    repo_slug: str,
    pull_request_id: int,
    comment_id: int,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Return raw API payloads for one comment from both /comments and /activities views."""
    direct_comment = get_pull_request_comment(
        project_key,
        repo_slug,
        pull_request_id,
        comment_id,
        username=username,
        password=password,
    )
    _, review_comments_by_id, dismissed_stable_ids = _load_provider_comment_gate_context_for_pr(
        project_key,
        repo_slug,
        pull_request_id,
        username=username,
        password=password,
    )
    wanted = str(comment_id)
    matching_activities = [
        activity
        for activity in list_pull_request_activities(
            project_key,
            repo_slug,
            pull_request_id,
            username=username,
            password=password,
        )
        if _activity_contains_comment_id(activity, wanted)
    ]
    matching_activity_comments = [
        comment_tree
        for activity in matching_activities
        if (comment_tree := _activity_comment_tree(activity)) is not None
    ]
    return {
        "project_key": project_key,
        "repo_slug": repo_slug,
        "pull_request_id": pull_request_id,
        "comment_id": wanted,
        "comment_endpoint": direct_comment,
        "quality_gate_view": _build_comment_report_with_provider_context(
            direct_comment,
            review_comments_by_id=review_comments_by_id,
            dismissed_stable_ids=dismissed_stable_ids,
        ),
        "matching_activities": matching_activities,
        "matching_activity_comments": matching_activity_comments,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect whether Bitbucket Server/DC PR comments or tasks count toward Viper's quality gate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    comment_parser = subparsers.add_parser(
        "comment",
        help="Inspect one pull request comment and report whether it currently counts toward the gate",
    )
    comment_parser.add_argument("project_key", help=PROJECT_KEY_HELP)
    comment_parser.add_argument("repo_slug", help=REPO_SLUG_HELP)
    comment_parser.add_argument("pull_request_id", type=int, help=PULL_REQUEST_ID_HELP)
    comment_parser.add_argument("comment_id", type=int, help="Pull request comment id")

    raw_parser = subparsers.add_parser(
        "raw",
        help="Inspect one pull request comment via raw /comments and /activities API payloads",
    )
    raw_parser.add_argument("project_key", help=PROJECT_KEY_HELP)
    raw_parser.add_argument("repo_slug", help=REPO_SLUG_HELP)
    raw_parser.add_argument("pull_request_id", type=int, help=PULL_REQUEST_ID_HELP)
    raw_parser.add_argument("comment_id", type=int, help="Pull request comment id")

    pr_parser = subparsers.add_parser(
        "pr",
        help="Inspect the full pull request quality-gate snapshot (comments plus tasks)",
    )
    pr_parser.add_argument("project_key", help=PROJECT_KEY_HELP)
    pr_parser.add_argument("repo_slug", help=REPO_SLUG_HELP)
    pr_parser.add_argument("pull_request_id", type=int, help=PULL_REQUEST_ID_HELP)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    username, password = load_script_credentials()
    if not username or not password:
        parser.error("SE_USER and SE_PASSWORD must be set (or present in the repo .env file).")

    if args.command == "comment":
        comment = get_pull_request_comment(
            args.project_key,
            args.repo_slug,
            args.pull_request_id,
            args.comment_id,
            username=username,
            password=password,
        )
        _, review_comments_by_id, dismissed_stable_ids = _load_provider_comment_gate_context_for_pr(
            args.project_key,
            args.repo_slug,
            args.pull_request_id,
            username=username,
            password=password,
        )
        print(
            json.dumps(
                _build_comment_report_with_provider_context(
                    comment,
                    review_comments_by_id=review_comments_by_id,
                    dismissed_stable_ids=dismissed_stable_ids,
                ),
                indent=2,
            )
        )
        return

    if args.command == "raw":
        report = build_comment_raw_report(
            args.project_key,
            args.repo_slug,
            args.pull_request_id,
            args.comment_id,
            username=username,
            password=password,
        )
        print(json.dumps(report, indent=2))
        return

    report = build_pr_gate_report(
        args.project_key,
        args.repo_slug,
        args.pull_request_id,
        username=username,
        password=password,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
