"""Repo content safety: max size and explicit delimiter when truncating."""

MAX_REPO_FILE_BYTES = 16 * 1024  # 16KB
TRUNCATE_SUFFIX = "\n\n--- (truncated, max size exceeded)\n"


def truncate_repo_content(content: str, max_bytes: int = MAX_REPO_FILE_BYTES) -> str:
    """
    Return content as-is if within max_bytes; otherwise truncate and append
    an explicit delimiter so the model knows context was cut.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    # Truncate to leave room for suffix
    suffix_bytes = TRUNCATE_SUFFIX.encode("utf-8")
    take = max_bytes - len(suffix_bytes)
    if take <= 0:
        return TRUNCATE_SUFFIX.strip()
    truncated = encoded[:take].decode("utf-8", errors="ignore")
    return truncated + TRUNCATE_SUFFIX
