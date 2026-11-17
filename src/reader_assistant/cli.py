"""Command-line entry point: ``reader-assistant`` and ``python -m reader_assistant``.

M1 provides only ``--version``, ``--config``, ``--log-level`` and the ``info``
command. Reading the clipboard, playback and the GUI arrive in later milestones.

Exit codes:
    0  success
    2  configuration error (the message is printed to stderr)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from reader_assistant import __version__
from reader_assistant.config import (
    LOG_LEVELS,
    AppConfig,
    AppPaths,
    ConfigError,
    load_config,
    resolve_paths,
)
from reader_assistant.logging_setup import configure_logging

EXIT_OK: Final = 0
EXIT_CONFIG_ERROR: Final = 2

_log = logging.getLogger("reader_assistant.cli")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Exposed for tests and documentation tooling."""
    parser = argparse.ArgumentParser(
        prog="reader-assistant",
        description="Read clipboard text aloud using local, offline text-to-speech.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="configuration file to use instead of the default location",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=sorted(LOG_LEVELS),
        help="override the configured log level for this run",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("info", help="show the effective configuration and file locations")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> int:
    """Run the CLI and return the process exit code.

    ``env`` and ``home`` default to the real process environment and home directory;
    tests pass explicit values so that they never touch the user's files.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    environment: Mapping[str, str] = os.environ if env is None else env
    paths = resolve_paths(environment, home if home is not None else Path.home())

    try:
        config = load_config(paths=paths, env=environment, config_file=args.config)
    except ConfigError as exc:
        print(f"reader-assistant: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.log_level is not None:
        config = replace(config, log_level=args.log_level)

    configure_logging(config.log_level, config.log_dir if config.log_to_file else None)
    _log.info("cli started", extra={"version": __version__, "command": args.command or "help"})

    if args.command == "info":
        print(format_info(config, paths))
        return EXIT_OK

    parser.print_help()
    return EXIT_OK


def format_info(config: AppConfig, paths: AppPaths) -> str:
    """Render the effective configuration as ``key: value`` lines."""
    rows: list[tuple[str, str]] = [
        ("version", __version__),
        ("config file", str(paths.config_file)),
        ("config file exists", "yes" if paths.config_file.exists() else "no"),
        ("log level", config.log_level),
        ("log to file", "yes" if config.log_to_file else "no"),
        ("log directory", str(config.log_dir)),
        ("models directory", str(config.models_dir)),
        ("default language", config.default_language),
        ("default voice", config.default_voice or "(engine default)"),
        ("speech rate", f"{config.speech_rate:g}"),
        ("volume", f"{config.volume:g}"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)
