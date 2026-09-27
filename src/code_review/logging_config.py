"""Logging configuration for the code review agent.

Log level is controlled by the CODE_REVIEW_LOG_LEVEL environment variable
(or the optional argument to configure_logging). Default is WARNING so that
normal runs stay quiet; set to INFO for progress messages, DEBUG for verbose.
"""

import json
import logging
import os

LOG_LEVEL_ENV = "CODE_REVIEW_LOG_LEVEL"
LOG_FORMAT_ENV = "CODE_REVIEW_LOG_FORMAT"
DEFAULT_LEVEL = "WARNING"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Standard LogRecord attributes; anything else on the record is an "extra" field
# that the JSON formatter includes when JSON-serialisable.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, trace_id, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": str(getattr(record, "trace_id", "-")),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in vars(record).items():
            if key in _STANDARD_RECORD_ATTRS or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


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


class _ManagedStreamHandler(logging.StreamHandler):
    """StreamHandler installed by configure_logging.

    Marked so that repeated configure_logging() calls only reformat/relevel
    handlers this function installed and never clobber caller-supplied ones.
    """

    _code_review_managed = True


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
    formatter: logging.Formatter = (
        JsonFormatter()
        if (os.environ.get(LOG_FORMAT_ENV) or "").strip().lower() == "json"
        else logging.Formatter(LOG_FORMAT)
    )
    if not log.handlers:
        log.addHandler(_ManagedStreamHandler())
    # Only retouch handlers this function installed; a caller-supplied handler
    # keeps its own formatter and level.
    for existing in log.handlers:
        if not getattr(existing, "_code_review_managed", False):
            continue
        existing.setFormatter(formatter)
        existing.setLevel(numeric)
    # Prevent propagation to root so we don't double-print if root is configured
    log.propagate = False


def emit_package_log(logger: logging.Logger, level: int, msg: str, *args) -> None:
    """Log via the package logger and mirror to root when propagation is disabled.

    This keeps normal runtime behavior unchanged while still letting root-based
    capture handlers (for example pytest's ``caplog``) observe package log records
    after ``configure_logging()`` has installed a non-propagating handler.
    """
    logger.log(level, msg, *args)
    package_logger = logging.getLogger("code_review")
    if not package_logger.propagate:
        if not logger.isEnabledFor(level):
            return
        record = logger.makeRecord(
            logger.name,
            level,
            fn="",
            lno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )
        logging.getLogger().handle(record)
