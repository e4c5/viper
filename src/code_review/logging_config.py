"""Logging configuration for the code review agent.

Log level is controlled by the CODE_REVIEW_LOG_LEVEL environment variable
(or the optional argument to configure_logging). Default is WARNING so that
normal runs stay quiet; set to INFO for progress messages, DEBUG for verbose.
"""

import logging
import os

LOG_LEVEL_ENV = "CODE_REVIEW_LOG_LEVEL"
DEFAULT_LEVEL = "WARNING"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _filter_non_text_parts_warning(record: logging.LogRecord) -> bool:
    """Suppress expected 'non-text parts' warning from google-genai."""
    msg = record.getMessage()
    return "non-text parts" not in msg


def _suppress_third_party_loggers() -> None:
    """Suppress noisy logging from third-party libraries."""
    # Suppress litellm's own verbose output and logging
    try:
        import litellm

        litellm.suppress_debug_info = True
        litellm.set_verbose = False
        litellm_logger = logging.getLogger("LiteLLM")
        litellm_logger.setLevel(logging.ERROR)
    except ImportError:
        pass
    # Suppress opentelemetry's potentially noisy loggers
    logging.getLogger("opentelemetry").setLevel(logging.ERROR)

    # Suppress google-genai's non-text parts warnings
    genai_logger = logging.getLogger("google_genai.types")
    if _filter_non_text_parts_warning not in genai_logger.filters:
        genai_logger.addFilter(_filter_non_text_parts_warning)


def configure_logging(level: str | None = None) -> None:
    """Configure logging for the code_review package.

    If level is None, uses CODE_REVIEW_LOG_LEVEL env var, falling back to
    DEFAULT_LEVEL. Valid values: DEBUG, INFO, WARNING, ERROR (case-insensitive).
    """
    raw = (level or os.environ.get(LOG_LEVEL_ENV) or DEFAULT_LEVEL).strip().upper()
    try:
        numeric = getattr(logging, raw)
    except AttributeError:
        numeric = logging.WARNING
    log = logging.getLogger("code_review")
    log.setLevel(numeric)
    _suppress_third_party_loggers()
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(numeric)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        log.addHandler(handler)
    # Prevent propagation to root so we don't double-print if root is configured
    log.propagate = False
