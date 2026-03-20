class ContextAwareFatalError(Exception):
    """Enabled context source is misconfigured, or remote authentication/authorization failed."""


class ContextAwareAuthError(ContextAwareFatalError):
    """Remote returned 401/403 – credentials are invalid or missing (always fatal)."""
