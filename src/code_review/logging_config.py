"""Logging configuration for the code review agent.

Log level is controlled by the CODE_REVIEW_LOG_LEVEL environment variable
(or the optional argument to configure_logging). Default is WARNING so that
normal runs stay quiet; set to INFO for progress messages, DEBUG for verbose.
"""

import logging
import os

LOG_LEVEL_ENV = "CODE_REVIEW_LOG_LEVEL"
DEFAULT_LEVEL = "WARNING"
LOG_FORMAT = "%(levelname)s: %(message)s"


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
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(numeric)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        log.addHandler(handler)
    # Prevent propagation to root so we don't double-print if root is configured
    log.propagate = False
