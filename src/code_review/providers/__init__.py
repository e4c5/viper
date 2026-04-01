"""SCM provider adapters (Gitea, GitLab, Bitbucket)."""

from code_review.providers.base import (
    FileInfo,
    InlineComment,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
    UnresolvedReviewItem,
)
from code_review.providers.http_base import HttpXProvider
from code_review.providers.bitbucket import BitbucketProvider
from code_review.providers.bitbucket_server import BitbucketServerProvider
from code_review.providers.gitea import GiteaProvider
from code_review.providers.github import GitHubProvider
from code_review.providers.gitlab import GitLabProvider


def get_provider(
    name: str,
    base_url: str,
    token: str,
    *,
    bitbucket_server_user_slug: str = "",
) -> ProviderInterface:
    """Factory for SCM providers."""
    if name == "gitea":
        return GiteaProvider(base_url=base_url, token=token)
    if name == "github":
        return GitHubProvider(base_url=base_url, token=token)
    if name == "gitlab":
        return GitLabProvider(base_url=base_url, token=token)
    if name == "bitbucket":
        return BitbucketProvider(base_url=base_url, token=token)
    if name == "bitbucket_server":
        return BitbucketServerProvider(
            base_url=base_url,
            token=token,
            participant_user_slug=bitbucket_server_user_slug,
        )
    raise ValueError(f"Unknown provider: {name}")


__all__ = [
    "BitbucketProvider",
    "BitbucketServerProvider",
    "FileInfo",
    "GiteaProvider",
    "GitHubProvider",
    "GitLabProvider",
    "HttpXProvider",
    "InlineComment",
    "PRInfo",
    "ProviderCapabilities",
    "ProviderInterface",
    "ReviewComment",
    "UnresolvedReviewItem",
    "get_provider",
]
