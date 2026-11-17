from __future__ import annotations

import re
from pathlib import Path

import pytest

from reader_assistant import __version__
from reader_assistant.cli import EXIT_CONFIG_ERROR, EXIT_OK, format_info, main
from reader_assistant.config import AppConfig, AppPaths, resolve_paths


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


def _run(home: Path, *args: str, env: dict[str, str] | None = None) -> int:
    return main(list(args), env=env or {}, home=home)


def _value(output: str, label: str) -> str:
    """Return the value printed for ``label``, ignoring column padding.

    The label must be followed by at least two spaces, so "config file" does not
    match the "config file exists" row.
    """
    match = re.search(rf"^{re.escape(label)}\s{{2,}}(.*)$", output, flags=re.MULTILINE)
    assert match is not None, f"no '{label}' row in output:\n{output}"
    return match.group(1)


def test_version_flag_prints_package_version_and_exits_zero(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(home, "--version")

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"reader-assistant {__version__}"


def test_package_version_is_the_declared_release_version() -> None:
    # Guards against __init__ falling back to the "unknown" placeholder, which would
    # happen if the package were imported from source without being installed.
    assert __version__ == "0.1.0"


def test_no_command_prints_help_and_succeeds(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(home) == EXIT_OK

    output = capsys.readouterr().out
    assert "usage: reader-assistant" in output
    assert "info" in output


def test_info_reports_effective_configuration_and_paths(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(home, "info") == EXIT_OK

    output = capsys.readouterr().out
    expected_config = home / ".config" / "reader-assistant" / "config.toml"
    assert _value(output, "config file") == str(expected_config)
    assert _value(output, "config file exists") == "no"
    assert _value(output, "log level") == "INFO"
    assert _value(output, "default language") == "auto"
    assert _value(output, "default voice") == "(engine default)"


def test_info_uses_config_file_and_reports_its_values(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file = tmp_path / "custom.toml"
    config_file.write_text('default_language = "he"\nspeech_rate = 1.5\n', encoding="utf-8")

    assert _run(home, "--config", str(config_file), "info") == EXIT_OK

    output = capsys.readouterr().out
    assert _value(output, "default language") == "he"
    assert _value(output, "speech rate") == "1.5"


def test_log_level_flag_overrides_configuration(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file = tmp_path / "custom.toml"
    config_file.write_text('log_level = "ERROR"\n', encoding="utf-8")

    assert _run(home, "--config", str(config_file), "--log-level", "debug", "info") == EXIT_OK

    assert _value(capsys.readouterr().out, "log level") == "DEBUG"


def test_invalid_log_level_flag_is_rejected_by_parser(home: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(home, "--log-level", "loud", "info")

    assert excinfo.value.code == 2


def test_configuration_error_returns_exit_code_two(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file = tmp_path / "bad.toml"
    config_file.write_text('default_language = "klingon"\n', encoding="utf-8")

    code = _run(home, "--config", str(config_file), "info")

    assert code == EXIT_CONFIG_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration error" in captured.err
    assert "default_language must be one of" in captured.err


def test_run_writes_log_file_in_state_directory(home: Path) -> None:
    assert _run(home, "info") == EXIT_OK

    log_file = home / ".local" / "state" / "reader-assistant" / "logs" / "reader-assistant.log"
    assert log_file.exists()


def test_logging_can_be_disabled_by_configuration(home: Path, tmp_path: Path) -> None:
    config_file = tmp_path / "quiet.toml"
    config_file.write_text("log_to_file = false\n", encoding="utf-8")

    assert _run(home, "--config", str(config_file), "info") == EXIT_OK

    assert not (home / ".local" / "state" / "reader-assistant" / "logs").exists()


def test_format_info_aligns_labels_and_shows_optional_voice(tmp_path: Path) -> None:
    paths: AppPaths = resolve_paths({}, tmp_path)
    config = AppConfig(
        log_level="INFO",
        log_to_file=False,
        log_dir=paths.log_dir,
        models_dir=paths.models_dir,
        default_language="en",
        default_voice="en_US-lessac-medium",
        speech_rate=1.0,
        volume=0.8,
    )

    text = format_info(config, paths)

    longest_label = len("config file exists")  # the widest label in format_info
    value_column = longest_label + 2
    for line in text.splitlines():
        assert line[longest_label:value_column] == "  ", line
        assert line[value_column] != " ", line  # every value starts at the same column
    assert _value(text, "default voice") == "en_US-lessac-medium"
    assert _value(text, "volume") == "0.8"
