"""Command-line entry point: ``reader-assistant`` and ``python -m reader_assistant``.

Commands:

- ``serve``: run the service in the foreground, headless. It owns the engines and the audio
  player, and listens on a Unix socket for speech requests and GUI control.
- ``serve --gui``: open the window as a client of that service. If no service is running, it
  starts one in a separate process first. Closing the window does not stop the service.
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
from reader_assistant.app.control import ServiceControl
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
from reader_assistant.ui.service_link import ServiceLink, Starter, start_background_service

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
        "--gui",
        action="store_true",
        help="open the window as a client; start the background service first if needed",
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
    if args.command == "serve" and args.gui:
        return run_gui(
            config,
            environment,
            real_home,
            service_args=_service_args(args.config, args.log_level),
        )
    if args.command == "serve":
        return run_service(config, environment, real_home)
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
) -> int:
    """Run the speech service in the foreground, headless, until it is stopped.

    The socket server runs on this thread. SIGINT or SIGTERM stops it. Engines and the sound
    output belong to this process only. The GUI never creates them; it connects to this service.

    ``registry`` and ``sink_factory`` default to the real engines and sound output.
    Tests pass fakes.
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

    def _on_signal(signum: int, frame: FrameType | None) -> None:
        if not servers:
            # Stopped before the socket existed: nothing to clean up yet.
            raise SystemExit(EXIT_OK)
        # serve_forever() runs on this thread, so shutdown() must run elsewhere.
        threading.Thread(target=servers[0].shutdown, name="service-shutdown", daemon=True).start()

    # Install the handlers before the socket exists, so a stop request can never
    # slip past them and kill the process without removing the socket.
    _install_stop_signals(_on_signal)

    try:
        server = SpeechServer(sock, handle, control=ServiceControl(service))
    except (ServiceAlreadyRunningError, SocketPathError) as exc:
        service.close()
        return _fail(str(exc))
    servers.append(server)

    _log.info("service listening", extra={"socket": str(sock)})
    try:
        with server:
            server.serve_forever()
    finally:
        service.close()
        _log.info("service stopped")
    return EXIT_OK


def run_gui(
    config: AppConfig,
    environment: Mapping[str, str],
    home: Path,
    *,
    service_args: tuple[str, ...] = (),
    starter: Starter | None = None,
    clipboard_reader: ClipboardReader | None = None,
) -> int:
    """Open the window as a client of the background service, and return when it is closed.

    The window never owns a speech engine or the sound output. If no service is running, it
    starts one (``reader-assistant serve`` in a separate process, with ``service_args``), and
    connects. Closing the window disconnects; the service keeps running.

    ``starter`` replaces the process start, and ``clipboard_reader`` replaces ``wl-paste``. Tests
    pass fakes.
    """
    try:
        sock = socket_path(environment, home)
    except SocketPathError as exc:
        return _fail(str(exc))

    log_hint = f"Its log is in {config.log_dir}." if config.log_to_file else ""
    link = ServiceLink(
        sock,
        starter=starter or (lambda: start_background_service(service_args)),
        log_hint=log_hint,
    )
    stop_requested = threading.Event()
    _install_stop_signals(lambda signum, frame: stop_requested.set())
    link.start()

    from PySide6.QtWidgets import QApplication

    from reader_assistant.ui.controller import GuiController
    from reader_assistant.ui.window import MainWindow

    app = QApplication.instance() or QApplication([])
    controller = GuiController(link, clipboard_reader=clipboard_reader or read_clipboard_text)
    window = MainWindow(controller, stop_requested=stop_requested)
    window.show()
    app.exec()
    return EXIT_OK


def _install_stop_signals(handler: Callable[[int, FrameType | None], None]) -> None:
    """Route SIGINT and SIGTERM to ``handler``. Only possible on the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _service_args(config_file: Path | None, log_level: str | None) -> tuple[str, ...]:
    """The global options a window must pass on when it starts the service itself."""
    args: list[str] = []
    if config_file is not None:
        args += ["--config", str(config_file)]
    if log_level is not None:
        args += ["--log-level", log_level]
    return tuple(args)


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
