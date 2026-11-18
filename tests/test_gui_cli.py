"""`serve --gui` end to end: Qt window, socket, CLI and shutdown in one process.

Runs offscreen. Engines and the sound output are fakes.
"""

from __future__ import annotations

import os
import shutil
import signal
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from fakes import FakeEngine, FakeSink, registry_with, short_dir, wait_for
from reader_assistant.cli import EXIT_OK, main, run_service
from reader_assistant.config import load_config, resolve_paths
from reader_assistant.engines.catalog import ENGLISH_VOICE, HEBREW_VOICE, RUSSIAN_VOICE
from reader_assistant.ipc.paths import socket_path


@pytest.fixture(autouse=True)
def _restore_signals() -> Iterator[None]:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


@pytest.fixture
def layout() -> Iterator[dict[str, Path]]:
    base = short_dir()
    dirs = {name: base / name for name in ("rt", "state", "data", "cfg", "home")}
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    yield dirs
    shutil.rmtree(base, ignore_errors=True)


def _env(layout: dict[str, Path]) -> dict[str, str]:
    return {
        "XDG_RUNTIME_DIR": str(layout["rt"]),
        "XDG_STATE_HOME": str(layout["state"]),
        "XDG_DATA_HOME": str(layout["data"]),
        "XDG_CONFIG_HOME": str(layout["cfg"]),
    }


def test_serve_gui_accepts_clipboard_text_through_the_socket_and_shuts_down_on_sigterm(
    layout: dict[str, Path],
) -> None:
    env = _env(layout)
    sink = FakeSink(write_delay=0.0)
    engines = registry_with(
        FakeEngine("kokoro", "en", ENGLISH_VOICE, samples=4800),
        FakeEngine("piper", "he", HEBREW_VOICE, samples=4800),
        FakeEngine("piper", "ru", RUSSIAN_VOICE, samples=4800),
    )
    config = load_config(paths=resolve_paths(env, layout["home"]), env=env)
    sock = socket_path(env, layout["home"])
    outcome: dict[str, object] = {}

    def cli_and_stop() -> None:
        try:
            wait_for(sock.exists)
            outcome["cli"] = main(
                ["read-clipboard"],
                env=env,
                home=layout["home"],
                clipboard_reader=lambda: "Good morning, this is English.",
            )
            wait_for(lambda: bool(sink.closed_with_drain), timeout=10.0)
        except BaseException as exc:  # reported back to the test thread
            outcome["error"] = exc
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # closes the window, as a stop signal would

    thread = threading.Thread(target=cli_and_stop, daemon=True)
    thread.start()
    code = run_service(
        config,
        env,
        layout["home"],
        registry=engines,
        sink_factory=lambda: sink,
        gui=True,
        clipboard_reader=lambda: "unused",
    )
    thread.join(timeout=30)

    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("cli") == EXIT_OK
    assert code == EXIT_OK
    assert sink.closed_with_drain == [True], "the spoken text reached the sound output"
    assert not sock.exists(), "the socket is removed on shutdown"
