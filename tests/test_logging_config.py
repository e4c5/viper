"""Tests for logging configuration."""

import io
import logging
import os
from unittest.mock import patch

from code_review.logging_config import (
    LOG_LEVEL_ENV,
    configure_logging,
    emit_package_log,
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


def test_configure_logging_updates_existing_handler_levels():
    """Reconfiguring logging updates existing (managed) handler levels."""
    log = logging.getLogger("code_review")
    saved_handlers = log.handlers[:]
    try:
        log.handlers.clear()
        with patch.dict(os.environ, {LOG_LEVEL_ENV: "WARNING"}, clear=False):
            configure_logging()
        with patch.dict(os.environ, {LOG_LEVEL_ENV: "DEBUG"}, clear=False):
            configure_logging()

        assert log.level == logging.DEBUG
        managed = [
            h for h in log.handlers if getattr(h, "_code_review_managed", False)
        ]
        assert managed
        assert all(handler.level == logging.DEBUG for handler in managed)
    finally:
        log.handlers.clear()
        log.handlers.extend(saved_handlers)


def test_configure_logging_preserves_foreign_handler_formatter():
    """Handlers not installed by configure_logging keep their formatter and level."""
    log = logging.getLogger("code_review")
    saved_handlers = log.handlers[:]
    custom_formatter = logging.Formatter("CUSTOM %(message)s")
    foreign = logging.StreamHandler(io.StringIO())
    foreign.setFormatter(custom_formatter)
    foreign.setLevel(logging.CRITICAL)
    try:
        log.handlers.clear()
        configure_logging(level="INFO")  # installs a managed handler
        log.addHandler(foreign)
        configure_logging(level="DEBUG")  # must not clobber the foreign handler
        assert foreign.formatter is custom_formatter
        assert foreign.level == logging.CRITICAL
        managed = [
            h for h in log.handlers if getattr(h, "_code_review_managed", False)
        ]
        assert managed and all(h.level == logging.DEBUG for h in managed)
    finally:
        log.handlers.clear()
        log.handlers.extend(saved_handlers)


def test_emit_package_log_does_not_mirror_disabled_level_to_root():
    stream = io.StringIO()
    root = logging.getLogger()
    package_logger = logging.getLogger("code_review")
    child_logger = logging.getLogger("code_review.test")
    old_root_handlers = list(root.handlers)
    old_root_level = root.level
    old_package_level = package_logger.level
    old_package_propagate = package_logger.propagate
    try:
        root.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.NOTSET)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        package_logger.setLevel(logging.WARNING)
        package_logger.propagate = False

        emit_package_log(child_logger, logging.INFO, "hidden-info")
        handler.flush()

        assert stream.getvalue() == ""
    finally:
        root.handlers.clear()
        root.handlers.extend(old_root_handlers)
        root.setLevel(old_root_level)
        package_logger.setLevel(old_package_level)
        package_logger.propagate = old_package_propagate


def test_emit_package_log_mirrors_enabled_level_to_root():
    stream = io.StringIO()
    root = logging.getLogger()
    package_logger = logging.getLogger("code_review")
    child_logger = logging.getLogger("code_review.test")
    old_root_handlers = list(root.handlers)
    old_root_level = root.level
    old_package_level = package_logger.level
    old_package_propagate = package_logger.propagate
    try:
        root.handlers.clear()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.NOTSET)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        package_logger.setLevel(logging.INFO)
        package_logger.propagate = False

        emit_package_log(child_logger, logging.INFO, "visible-info")
        handler.flush()

        assert "visible-info" in stream.getvalue()
    finally:
        root.handlers.clear()
        root.handlers.extend(old_root_handlers)
        root.setLevel(old_root_level)
        package_logger.setLevel(old_package_level)
        package_logger.propagate = old_package_propagate
