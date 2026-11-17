from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from reader_assistant.logging_setup import ROOT_LOGGER_NAME, configure_logging


def _app_handlers() -> list[logging.Handler]:
    """Handlers installed by configure_logging, excluding pytest's capture handlers.

    pytest's LogCaptureHandler subclasses StreamHandler, so match exact types.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    kinds = {logging.StreamHandler, logging.handlers.RotatingFileHandler}
    return [handler for handler in logger.handlers if type(handler) in kinds]


def test_structured_extras_are_written_as_sorted_key_value_pairs(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    logger = configure_logging("INFO", log_dir)

    logger.info("config loaded", extra={"zeta": 2, "alpha": "x"})

    contents = (log_dir / "reader-assistant.log").read_text(encoding="utf-8")
    line = contents.strip().splitlines()[-1]
    assert " INFO " in line
    assert "reader_assistant config loaded" in line
    assert line.endswith("alpha='x' zeta=2")


def test_messages_below_configured_level_are_suppressed(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    logger = configure_logging("WARNING", log_dir)

    logger.info("routine detail")
    logger.warning("something to notice")

    contents = (log_dir / "reader-assistant.log").read_text(encoding="utf-8")
    assert "routine detail" not in contents
    assert "something to notice" in contents


def test_repeated_configuration_does_not_duplicate_output(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    configure_logging("INFO", log_dir)
    logger = configure_logging("INFO", log_dir)

    logger.info("only once")

    assert len(_app_handlers()) == 2  # one console handler and one file handler
    contents = (log_dir / "reader-assistant.log").read_text(encoding="utf-8")
    assert contents.count("only once") == 1


def test_console_only_when_no_log_directory_is_given(tmp_path: Path) -> None:
    logger = configure_logging("INFO", None)

    handlers = _app_handlers()
    assert len(handlers) == 1
    assert type(handlers[0]) is logging.StreamHandler
    assert logger.propagate is False  # never double-log through the root logger
    assert not list(tmp_path.iterdir())
