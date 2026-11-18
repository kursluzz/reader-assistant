"""``serve --gui`` end to end: the window is a client, the service is started only if needed.

Runs offscreen. The service is a real socket service with fake engines and a fake sound output.
The "start" callback stands in for spawning ``reader-assistant serve``: it starts the same service
in this process, so no real process is spawned.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from fakes import FakeSink, short_dir, wait_for
from reader_assistant.cli import EXIT_OK, main, run_gui
from reader_assistant.config import load_config, resolve_paths
from reader_assistant.ipc.client import request
from reader_assistant.ipc.paths import socket_path
from service_harness import live_service


class _Process:
    """Stands in for the spawned service process: it is still running."""

    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


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


def _run_window(
    layout: dict[str, Path],
    driver: Callable[[], None],
    *,
    starter: Callable[[], Any],
) -> tuple[int, dict[str, object]]:
    """Run ``serve --gui`` on this thread. ``driver`` runs beside it, then closes the window."""
    env = _env(layout)
    config = load_config(paths=resolve_paths(env, layout["home"]), env=env)
    outcome: dict[str, object] = {}

    def drive() -> None:
        try:
            driver()
        except BaseException as exc:  # reported back to the test thread
            outcome["error"] = exc
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # closes the window, as a stop signal would

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    code = run_gui(config, env, layout["home"], starter=starter)
    thread.join(timeout=30)
    return code, outcome


def test_serve_gui_starts_the_service_speaks_through_it_and_leaves_it_running(
    layout: dict[str, Path],
) -> None:
    env = _env(layout)
    path = socket_path(env, layout["home"])
    sink = FakeSink(write_delay=0.0)
    contexts: list[Any] = []
    starts: list[int] = []

    def starter() -> _Process:
        starts.append(1)
        ctx = live_service(path, sink=sink)
        ctx.__enter__()
        contexts.append(ctx)
        return _Process()

    def driver() -> None:
        wait_for(path.exists)
        outcome = main(
            ["read-clipboard"],
            env=env,
            home=layout["home"],
            clipboard_reader=lambda: "Good morning, this is English.",
        )
        assert outcome == EXIT_OK
        wait_for(lambda: bool(sink.written), timeout=10.0)
        wait_for(lambda: request(path, "status")["state"] == "idle", timeout=10.0)

    try:
        code, outcome = _run_window(layout, driver, starter=starter)

        assert "error" not in outcome, outcome.get("error")
        assert code == EXIT_OK
        assert starts == [1], "the window started exactly one service"
        # Closing the window disconnected the window only. The service is still there.
        assert path.exists()
        assert request(path, "status")["state"] == "idle"
    finally:
        for ctx in contexts:
            ctx.__exit__(None, None, None)


def test_serve_gui_connects_to_a_running_service_without_starting_another(
    layout: dict[str, Path],
) -> None:
    env = _env(layout)
    path = socket_path(env, layout["home"])
    starts: list[int] = []

    def starter() -> _Process:  # pragma: no cover - must not run
        starts.append(1)
        raise AssertionError("a second service must not be started")

    def driver() -> None:
        wait_for(path.exists)
        outcome = main(
            ["read-clipboard"],
            env=env,
            home=layout["home"],
            clipboard_reader=lambda: "Hello there.",
        )
        assert outcome == EXIT_OK

    with live_service(path) as live:
        code, outcome = _run_window(layout, driver, starter=starter)
        assert live.service.status().state.value in {"idle", "playing", "synthesizing", "loading"}

    assert "error" not in outcome, outcome.get("error")
    assert code == EXIT_OK
    assert starts == []


def test_serve_gui_without_the_gui_extra_says_how_to_install_it_and_starts_nothing(
    layout: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _env(layout)
    path = socket_path(env, layout["home"])
    starts: list[int] = []
    # A missing optional package makes its import fail, as it does in a clean install.
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", None)
    config = load_config(paths=resolve_paths(env, layout["home"]), env=env)

    def starter() -> Any:
        starts.append(1)
        return _Process()

    code = run_gui(config, env, layout["home"], starter=starter)

    err = capsys.readouterr().err
    assert code == 1
    assert "uv sync --extra gui" in err
    assert "Traceback" not in err
    assert starts == []
    assert not path.exists()
