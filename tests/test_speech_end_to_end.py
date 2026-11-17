"""Complete M4 workflow: clipboard -> CLI -> Unix socket -> service -> engine -> audio sink.

``run_service`` runs on the main thread, so its real signal handlers are exercised: the
test sends SIGTERM to itself to stop the service, as GNOME or systemd would.
Engines and the sound output are fakes; no model is loaded and no device is opened.
"""

from __future__ import annotations

import os
import shutil
import signal
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fakes import FakeEngine, FakeSink, registry_with, short_dir, wait_for
from reader_assistant.cli import (
    EXIT_OK,
    EXIT_OPERATION_FAILED,
    EXIT_SERVICE_NOT_RUNNING,
    main,
    run_service,
)
from reader_assistant.clipboard.reader import ClipboardError
from reader_assistant.config import load_config, resolve_paths
from reader_assistant.engines.catalog import (
    DEFAULT_VOICE_BY_LANGUAGE,
    ENGLISH_VOICE,
    HEBREW_VOICE,
    RUSSIAN_VOICE,
)
from reader_assistant.ipc.paths import socket_path

TEXTS = {
    "en": "Good morning. This is the English voice reading the clipboard.",
    "he": "בוקר טוב. זהו הקול העברי קורא את הלוח.",
    "ru": "Доброе утро. Это русский голос читает буфер обмена.",
}


@pytest.fixture(autouse=True)
def _restore_signal_handlers() -> Iterator[None]:
    """run_service installs its own SIGINT/SIGTERM handlers; put the originals back."""
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


@pytest.fixture
def layout() -> Iterator[dict[str, Path]]:
    base = short_dir()
    dirs = {
        "base": base,
        "runtime": base / "rt",
        "state": base / "state",
        "data": base / "data",
        "config": base / "cfg",
        "home": base / "home",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    yield dirs
    shutil.rmtree(base, ignore_errors=True)


def _env(layout: dict[str, Path]) -> dict[str, str]:
    return {
        "XDG_RUNTIME_DIR": str(layout["runtime"]),
        "XDG_STATE_HOME": str(layout["state"]),
        "XDG_DATA_HOME": str(layout["data"]),
        "XDG_CONFIG_HOME": str(layout["config"]),
    }


def _engines() -> dict[str, FakeEngine]:
    return {
        "en": FakeEngine("kokoro", "en", ENGLISH_VOICE),
        "he": FakeEngine("piper", "he", HEBREW_VOICE),
        "ru": FakeEngine("piper", "ru", RUSSIAN_VOICE),
    }


def _serve_while(
    layout: dict[str, Path],
    engines: dict[str, FakeEngine],
    sink: FakeSink,
    client: Callable[[], object],
) -> tuple[int, object]:
    """Run the service on this thread while ``client`` runs on another.

    The client waits for the socket, runs its work, then this test sends SIGTERM to the
    process, which makes the service shut down through its real signal handler.
    """
    env = _env(layout)
    config = load_config(paths=resolve_paths(env, layout["home"]), env=env)
    outcome: dict[str, object] = {}
    sock = socket_path(env, layout["home"])

    def run_client() -> None:
        try:
            wait_for(sock.exists)
            outcome["client"] = client()
            wait_for(lambda: bool(sink.closed_with_drain) or not sink.written, timeout=10.0)
        except BaseException as exc:  # reported back to the test thread
            outcome["error"] = exc
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()
    serve_code = run_service(
        config,
        env,
        layout["home"],
        registry=registry_with(*engines.values()),
        sink_factory=lambda: sink,
    )
    thread.join(timeout=15)
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise AssertionError("client thread failed") from error
    return serve_code, outcome.get("client")


def _read_clipboard(layout: dict[str, Path], text: str) -> int:
    return main(
        ["read-clipboard"],
        env=_env(layout),
        home=layout["home"],
        clipboard_reader=lambda: text,
    )


@pytest.mark.parametrize("language", ["en", "he", "ru"])
def test_clipboard_text_reaches_the_correct_engine_and_is_played(
    layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    language: str,
) -> None:
    engines = _engines()
    sink = FakeSink()

    serve_code, exit_code = _serve_while(
        layout,
        engines,
        sink,
        lambda: _read_clipboard(layout, TEXTS[language]),
    )

    assert serve_code == EXIT_OK
    assert exit_code == EXIT_OK
    assert engines[language].synthesized == [(TEXTS[language], DEFAULT_VOICE_BY_LANGUAGE[language])]
    for other, engine in engines.items():
        if other != language:
            assert engine.synthesized == [], f"{other} engine should not have been used"
    assert sink.written, "generated audio should reach the sound output"
    assert sink.closed_with_drain == [True]
    assert not socket_path(_env(layout), layout["home"]).exists(), "socket removed on shutdown"


def test_successful_read_clipboard_prints_nothing(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    engines = _engines()
    sink = FakeSink()

    _serve_while(layout, engines, sink, lambda: _read_clipboard(layout, TEXTS["en"]))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_read_clipboard_without_a_service_says_how_to_start_it(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = _read_clipboard(layout, TEXTS["en"])

    err = capsys.readouterr().err
    assert code == EXIT_SERVICE_NOT_RUNNING
    assert "not running" in err
    assert "reader-assistant serve" in err


def test_empty_clipboard_is_reported_and_nothing_is_sent(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    def empty() -> str:
        raise ClipboardError("The clipboard is empty.")

    code = main(
        ["read-clipboard"],
        env=_env(layout),
        home=layout["home"],
        clipboard_reader=empty,
    )

    assert code == EXIT_OPERATION_FAILED
    assert "clipboard is empty" in capsys.readouterr().err


def test_unsupported_clipboard_text_is_rejected_by_the_service(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    engines = _engines()
    sink = FakeSink()

    serve_code, exit_code = _serve_while(
        layout, engines, sink, lambda: _read_clipboard(layout, "12345 !!!")
    )

    assert serve_code == EXIT_OK
    assert exit_code == EXIT_OPERATION_FAILED
    assert "no English, Russian or Hebrew letters" in capsys.readouterr().err
    assert all(engine.synthesized == [] for engine in engines.values())
    assert sink.written == bytearray()


def test_missing_wl_paste_is_reported_as_an_operation_failure(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> str:
        raise ClipboardError(
            "The wl-paste program is not installed. Install the wl-clipboard package."
        )

    code = main(
        ["read-clipboard"],
        env=_env(layout),
        home=layout["home"],
        clipboard_reader=missing,
    )

    assert code == EXIT_OPERATION_FAILED
    assert "wl-clipboard" in capsys.readouterr().err


def test_clipboard_read_error_never_contacts_the_service(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    # A clipboard failure is reported as such, even when no service is running, so the
    # user is not told to start the service for a problem with the clipboard.
    def broken() -> str:
        raise ClipboardError("Could not read the clipboard text: boom")

    code = main(
        ["read-clipboard"],
        env=_env(layout),
        home=layout["home"],
        clipboard_reader=broken,
    )

    assert code == EXIT_OPERATION_FAILED
    assert "boom" in capsys.readouterr().err


def test_serve_returns_the_configuration_error_code_for_a_bad_config(
    layout: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bad = layout["base"] / "bad.toml"
    bad.write_text('default_language = "klingon"\n', encoding="utf-8")

    code = main(["--config", str(bad), "serve"], env=_env(layout), home=layout["home"])

    assert code == 2
    assert "configuration error" in capsys.readouterr().err
