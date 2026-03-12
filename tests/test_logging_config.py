"""Tests for logging configuration."""

import logging
import os
from unittest.mock import patch

from code_review.logging_config import (
    DEFAULT_LEVEL,
    LOG_LEVEL_ENV,
    configure_logging,
)


def test_configure_logging_uses_env():
    """configure_logging uses CODE_REVIEW_LOG_LEVEL when set."""
    with patch.dict(os.environ, {LOG_LEVEL_ENV: "DEBUG"}, clear=False):
        configure_logging()
    log = logging.getLogger("code_review")
    assert log.level == logging.DEBUG


def test_configure_logging_invalid_level_falls_back_to_warning():
    """Invalid log level (e.g. typo) falls back to WARNING."""
    with patch.dict(os.environ, {LOG_LEVEL_ENV: "INVALID_LEVEL"}, clear=False):
        configure_logging()
    log = logging.getLogger("code_review")
    assert log.level == logging.WARNING


def test_configure_logging_default_when_env_unset():
    """When env is unset, default level is WARNING."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(LOG_LEVEL_ENV, None)
        configure_logging(level=None)
    log = logging.getLogger("code_review")
    assert log.level == logging.WARNING
