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
    list_pull_request_comments,
    list_pull_request_tasks,
    load_script_credentials,
)
from code_review.formatters.comment import infer_severity_from_comment_body


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
    state = str(comment.get("state") or "").strip().upper()
    body = str(comment.get("text") or comment.get("body") or "").strip()
    if state == "RESOLVED":
        return False, "resolved"
    if comment_is_outdated(comment):
        return False, "outdated_or_orphaned"
    if not body:
        return False, "empty_body"
    return True, "open"


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
    comment_reports = [
        build_comment_report(comment)
        for comment in list_pull_request_comments(
            project_key,
            repo_slug,
            pull_request_id,
            username=username,
            password=password,
        )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect whether Bitbucket Server/DC PR comments or tasks count toward Viper's quality gate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    comment_parser = subparsers.add_parser(
        "comment",
        help="Inspect one pull request comment and report whether it currently counts toward the gate",
    )
    comment_parser.add_argument("project_key", help="Bitbucket project key, for example PRJ")
    comment_parser.add_argument("repo_slug", help="Bitbucket repository slug")
    comment_parser.add_argument("pull_request_id", type=int, help="Pull request id")
    comment_parser.add_argument("comment_id", type=int, help="Pull request comment id")

    pr_parser = subparsers.add_parser(
        "pr",
        help="Inspect the full pull request quality-gate snapshot (comments plus tasks)",
    )
    pr_parser.add_argument("project_key", help="Bitbucket project key, for example PRJ")
    pr_parser.add_argument("repo_slug", help="Bitbucket repository slug")
    pr_parser.add_argument("pull_request_id", type=int, help="Pull request id")
    return parser


def main() -> int:
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
        print(json.dumps(build_comment_report(comment), indent=2))
        return 0

    report = build_pr_gate_report(
        args.project_key,
        args.repo_slug,
        args.pull_request_id,
        username=username,
        password=password,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
