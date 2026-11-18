"""Command-line entry point: ``reader-assistant`` and ``python -m reader_assistant``.

Commands:

- ``serve``: run the service in the foreground. It owns the engines and the audio
  player, and listens on a Unix socket for speech requests.
- ``read-clipboard``: read the clipboard and send its text to the running service.
  Intended for a GNOME keyboard shortcut. Prints nothing on success.
- ``info``: show the effective configuration and file locations.

Exit codes:
    0  success
    1  operation failed (empty or unsupported clipboard, rejected text, device or
       engine error, invalid reply)
    2  configuration error (message on stderr)
    3  the service is not running (``read-clipboard`` only)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Final

from reader_assistant import __version__
from reader_assistant.app.reader_service import ReaderService, SpeechError
from reader_assistant.audio.player import AudioPlayer
from reader_assistant.audio.sink import SinkFactory, default_sink
from reader_assistant.clipboard.reader import ClipboardError, read_clipboard_text
from reader_assistant.config import (
    LOG_LEVELS,
    AppConfig,
    AppPaths,
    ConfigError,
    load_config,
    resolve_paths,
)
from reader_assistant.engines.catalog import default_registry
from reader_assistant.engines.registry import EngineRegistry
from reader_assistant.ipc.client import ServiceUnavailableError, send_speak
from reader_assistant.ipc.paths import SocketPathError, socket_path
from reader_assistant.ipc.protocol import ProtocolError, Response, SpeakRequest
from reader_assistant.ipc.server import ServiceAlreadyRunningError, SpeechServer
from reader_assistant.logging_setup import configure_logging
from reader_assistant.notify import notify_service_not_running

EXIT_OK: Final = 0
EXIT_OPERATION_FAILED: Final = 1
EXIT_CONFIG_ERROR: Final = 2
EXIT_SERVICE_NOT_RUNNING: Final = 3
_SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0

ClipboardReader = Callable[[], str]

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
    serve = subparsers.add_parser(
        "serve", help="run the speech service in the foreground (listens on a local socket)"
    )
    serve.add_argument(
        "--gui", action="store_true", help="also open the window, in the same process"
    )
    subparsers.add_parser(
        "read-clipboard", help="read the clipboard text aloud through the running service"
    )
    subparsers.add_parser("info", help="show the effective configuration and file locations")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    clipboard_reader: ClipboardReader | None = None,
) -> int:
    """Run the CLI and return the process exit code.

    ``env``, ``home`` and ``clipboard_reader`` default to the real environment, home
    directory and ``wl-paste``. Tests pass fakes so that nothing outside them is touched.
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

    # A keyboard shortcut must stay silent on success, so its console shows only warnings.
    console_level = "WARNING" if args.command == "read-clipboard" else None
    configure_logging(
        config.log_level,
        config.log_dir if config.log_to_file else None,
        console_level=console_level,
    )
    _log.info("cli started", extra={"version": __version__, "command": args.command or "help"})

    if args.command == "info":
        print(format_info(config, paths))
        return EXIT_OK
    real_home = home if home is not None else Path.home()
    if args.command == "serve":
        return run_service(config, environment, real_home, gui=args.gui)
    if args.command == "read-clipboard":
        reader = clipboard_reader or read_clipboard_text
        return run_read_clipboard(environment, real_home, reader)

    parser.print_help()
    return EXIT_OK


def run_service(
    config: AppConfig,
    environment: Mapping[str, str],
    home: Path,
    *,
    registry: EngineRegistry | None = None,
    sink_factory: SinkFactory | None = None,
    gui: bool = False,
    clipboard_reader: Callable[[], str] | None = None,
) -> int:
    """Run the speech service in the foreground until it is stopped.

    Without ``gui``, the socket server runs on this thread and SIGINT or SIGTERM stops it.
    With ``gui``, the Tkinter window runs on this (the main) thread, and the socket server
    runs on a helper thread. Closing the window, or a stop signal, shuts both down.

    ``registry``, ``sink_factory`` and ``clipboard_reader`` default to the real engines,
    sound output and ``wl-paste``. Tests pass fakes.
    """
    try:
        sock = socket_path(environment, home)
    except SocketPathError as exc:
        return _fail(str(exc))

    default_language = "en" if config.default_language == "auto" else config.default_language
    service = ReaderService(
        registry if registry is not None else default_registry(config.models_dir),
        AudioPlayer(sink_factory or default_sink, volume=config.volume),
        default_language=default_language,
    )
    service.start()

    def handle(request: SpeakRequest) -> Response:
        try:
            accepted = service.submit(request.text)
        except SpeechError as exc:
            raise ProtocolError(exc.code, exc.message) from exc
        return Response(accepted=True, language=accepted.language, voice=accepted.voice_id)

    servers: list[SpeechServer] = []
    stop_requested = threading.Event()

    def _on_signal(signum: int, frame: FrameType | None) -> None:
        if gui:
            stop_requested.set()  # the window polls this and closes itself
            return
        if not servers:
            # Stopped before the socket existed: nothing to clean up yet.
            raise SystemExit(EXIT_OK)
        # serve_forever() runs on this thread, so shutdown() must run elsewhere.
        threading.Thread(target=servers[0].shutdown, name="service-shutdown", daemon=True).start()

    # Install the handlers before the socket exists, so a stop request can never
    # slip past them and kill the process without removing the socket.
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        server = SpeechServer(sock, handle)
    except ServiceAlreadyRunningError as exc:
        service.close()
        return _fail(str(exc))
    except SocketPathError as exc:
        service.close()
        return _fail(str(exc))
    servers.append(server)

    _log.info("service listening", extra={"socket": str(sock), "gui": gui})
    if not gui:
        try:
            with server:
                server.serve_forever()
        finally:
            service.close()
            _log.info("service stopped")
        return EXIT_OK

    return _run_with_window(
        server,
        service,
        stop_requested,
        clipboard_reader or read_clipboard_text,
    )


def _run_with_window(
    server: SpeechServer,
    service: ReaderService,
    stop_requested: threading.Event,
    clipboard_reader: Callable[[], str],
) -> int:
    """Serve the socket on a helper thread and run the Qt window on this (the main) thread."""
    from PySide6.QtWidgets import QApplication

    from reader_assistant.ui.controller import GuiController
    from reader_assistant.ui.window import MainWindow

    serve_thread = threading.Thread(
        target=server.serve_forever, name="reader-assistant-ipc", daemon=True
    )
    serve_thread.start()

    def shut_everything_down() -> None:
        if serve_thread.is_alive():
            server.shutdown()
            serve_thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        server.close()
        service.close()

    def on_close() -> threading.Event:
        done = threading.Event()

        def run() -> None:
            try:
                shut_everything_down()
            finally:
                _log.info("service stopped")
                done.set()

        threading.Thread(target=run, name="reader-assistant-shutdown", daemon=True).start()
        return done

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        GuiController(service, clipboard_reader=clipboard_reader),
        on_close=on_close,
        stop_requested=stop_requested,
    )
    window.show()
    app.exec()
    return EXIT_OK


def run_read_clipboard(
    environment: Mapping[str, str],
    home: Path,
    reader: ClipboardReader,
) -> int:
    """Read the clipboard and hand its text to the running service."""
    try:
        text = reader()
    except ClipboardError as exc:
        return _fail(str(exc))

    try:
        sock = socket_path(environment, home)
    except SocketPathError as exc:
        return _fail(str(exc))

    try:
        response = send_speak(sock, SpeakRequest(text=text))
    except ServiceUnavailableError as exc:
        if exc.not_running:
            notify_service_not_running()
            return _fail(
                "The speech service is not running. Start it with: reader-assistant serve",
                code=EXIT_SERVICE_NOT_RUNNING,
            )
        return _fail(str(exc), code=EXIT_OPERATION_FAILED)
    except ProtocolError as exc:
        return _fail(f"unexpected reply from the service: {exc.message}")

    if response.accepted:
        _log.info(
            "clipboard text accepted",
            extra={"language": response.language, "voice": response.voice},
        )
        return EXIT_OK
    return _fail(response.message)


def _fail(message: str, *, code: int = EXIT_OPERATION_FAILED) -> int:
    print(f"reader-assistant: {message}", file=sys.stderr)
    return code


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
