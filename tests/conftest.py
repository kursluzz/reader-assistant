from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from reader_assistant.logging_setup import ROOT_LOGGER_NAME


def _close_app_handlers() -> None:
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def _isolated_app_logger() -> Iterator[None]:
    """The application logger is process-global: clean it before and after every test.

    Without this, file handlers opened by one test would outlive its temporary
    directory and leak open file descriptors into the next.
    """
    _close_app_handlers()
    yield
    _close_app_handlers()
