"""Structured logging for the application.

All application loggers live under the ``reader_assistant`` namespace. Records
are written as single lines of ``timestamp level logger message key=value...``,
where the key/value pairs come from the ``extra=`` argument of a log call. That
keeps logs greppable without adding a logging dependency.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Final

from reader_assistant.config import LOG_FILE_NAME

ROOT_LOGGER_NAME: Final = "reader_assistant"
_HANDLER_MARKER: Final = "_reader_assistant_handler"
_MAX_LOG_BYTES: Final = 1_000_000
_BACKUP_COUNT: Final = 3

# Attributes every LogRecord has. Anything else on a record was passed via ``extra``.
_STANDARD_ATTRIBUTES: Final = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


class KeyValueFormatter(logging.Formatter):
    """Formatter that appends ``extra`` fields as sorted ``key=value`` pairs."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_")
        }
        if extras:
            pairs = " ".join(f"{key}={value!r}" for key, value in sorted(extras.items()))
            line = f"{line} {pairs}"
        return line


def configure_logging(
    level: str,
    log_dir: Path | None = None,
    *,
    console_level: str | None = None,
) -> logging.Logger:
    """Configure the application logger. Safe to call more than once.

    Previously installed handlers from this module are replaced, so repeated calls
    do not duplicate output. Logs go to stderr at ``console_level`` (default: the same
    as ``level``). If ``log_dir`` is given they also go to a rotating file at ``level``.
    Commands that run from a keyboard shortcut pass ``console_level="WARNING"`` so they
    stay silent when they succeed.

    Returns:
        The configured ``reader_assistant`` logger.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

    console = logging.StreamHandler()
    console.setLevel(console_level or level)
    console.setFormatter(KeyValueFormatter())
    _mark(console)
    logger.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / LOG_FILE_NAME,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(KeyValueFormatter())
        _mark(file_handler)
        logger.addHandler(file_handler)

    return logger


def _mark(handler: logging.Handler) -> None:
    setattr(handler, _HANDLER_MARKER, True)
