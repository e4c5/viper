"""SCM provider adapters (Gitea, GitLab, Bitbucket)."""

from code_review.providers.base import (
    FileInfo,
    PRInfo,
    ProviderCapabilities,
    ProviderInterface,
    ReviewComment,
)
from code_review.providers.gitea import GiteaProvider


def get_provider(name: str, base_url: str, token: str) -> ProviderInterface:
    """Factory for SCM providers."""
    if name == "gitea":
        return GiteaProvider(base_url=base_url, token=token)
    raise ValueError(f"Unknown provider: {name}")


__all__ = [
    "FileInfo",
    "GiteaProvider",
    "PRInfo",
    "ProviderCapabilities",
    "ProviderInterface",
    "ReviewComment",
    "get_provider",
]
