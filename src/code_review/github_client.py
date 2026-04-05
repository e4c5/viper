"""Shared PyGithub helpers for GitHub REST operations."""

from __future__ import annotations

from typing import Any

from github import Auth, Github


class GitHubApiClient:
    """Thin wrapper over PyGithub with a stable surface for Viper code."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        auth = Auth.Token(token) if token else None
        self._github = Github(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=max(1, int(timeout)),
            per_page=100,
        )

    def get_repo(self, owner: str, repo: str):
        return self._github.get_repo(f"{owner}/{repo}")

    def get_pull(self, owner: str, repo: str, pr_number: int):
        return self.get_repo(owner, repo).get_pull(int(pr_number))

    def get_issue(self, owner: str, repo: str, issue_number: int | str):
        return self.get_repo(owner, repo).get_issue(int(issue_number))

    def get_authenticated_user(self):
        return self._github.get_user()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> Any:
        _, data = self._github.requester.requestJsonAndCheck(
            method,
            path,
            parameters=params,
            headers=headers,
            input=body,
        )
        return data

    def request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> str:
        _, data = self._github.requester.requestBlobAndCheck(
            method,
            path,
            parameters=params,
            headers=headers,
            input=body,
        )
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)

    def graphql_query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        _, data = self._github.requester.graphql_query(query, variables)
        return data

    def create_pull_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        event: str,
        body: str,
        head_sha: str = "",
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        pull = self.get_pull(owner, repo, pr_number)
        kwargs: dict[str, Any] = {"body": body, "event": event}
        if comments is not None:
            kwargs["comments"] = comments
        if head_sha:
            kwargs["commit"] = self.get_repo(owner, repo).get_commit(head_sha)
        pull.create_review(**kwargs)

    def reply_to_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reply_to_comment_id: int,
        body: str,
    ) -> None:
        self.request_json(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            body={"body": body, "in_reply_to": reply_to_comment_id},
        )