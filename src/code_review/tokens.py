"""Model-aware token counting.

`count_tokens` uses litellm's tokenizer when a model name is given and the
optional litellm extra is installed, and falls back to the chars/4 heuristic in
`diff.utils.estimate_tokens` whenever litellm is missing or the model lookup
fails. Models that fail are remembered so repeated batching estimates do not
pay the exception cost per segment.
"""

from __future__ import annotations

import logging

from code_review.diff.utils import estimate_tokens

logger = logging.getLogger(__name__)

# Models for which litellm.token_counter already failed; retrying would only
# raise again on every segment of every batch.
_failed_models: set[str] = set()
_litellm_missing = False


def count_tokens(text: str, *, model: str | None = None) -> int:
    """Return the token count of ``text``, model-aware when possible."""
    global _litellm_missing
    if not text:
        return 0
    if (
        not isinstance(model, str)
        or not model
        or _litellm_missing
        or model in _failed_models
    ):
        return estimate_tokens(text)
    try:
        import litellm  # noqa: PLC0415 — optional dependency, lazy by design
    except ImportError:
        _litellm_missing = True
        return estimate_tokens(text)
    try:
        return int(litellm.token_counter(model=model, text=text))
    except Exception:
        _failed_models.add(model)
        logger.debug(
            "litellm.token_counter failed for model %r; using chars/4 estimate",
            model,
            exc_info=True,
        )
        return estimate_tokens(text)


def _reset_caches_for_tests() -> None:
    _failed_models.clear()
    global _litellm_missing
    _litellm_missing = False
