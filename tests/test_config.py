from __future__ import annotations

from pathlib import Path

import pytest

from reader_assistant.config import (
    ENV_LOG_LEVEL,
    ENV_MODELS_DIR,
    AppPaths,
    ConfigError,
    load_config,
    resolve_paths,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


@pytest.fixture
def paths(home: Path) -> AppPaths:
    return resolve_paths({}, home)


def _write_config(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_resolve_paths_uses_xdg_defaults_under_home(home: Path) -> None:
    paths = resolve_paths({}, home)

    assert paths.config_dir == home / ".config" / "reader-assistant"
    assert paths.data_dir == home / ".local" / "share" / "reader-assistant"
    assert paths.state_dir == home / ".local" / "state" / "reader-assistant"
    assert paths.config_file == paths.config_dir / "config.toml"
    assert paths.models_dir == paths.data_dir / "models"
    assert paths.log_dir == paths.state_dir / "logs"


def test_resolve_paths_honours_absolute_xdg_variables(home: Path, tmp_path: Path) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    paths = resolve_paths(env, home)

    assert paths.config_dir == tmp_path / "cfg" / "reader-assistant"
    assert paths.data_dir == tmp_path / "data" / "reader-assistant"
    assert paths.state_dir == tmp_path / "state" / "reader-assistant"


def test_resolve_paths_ignores_relative_xdg_variables(home: Path) -> None:
    # The XDG specification says relative values must be ignored.
    paths = resolve_paths({"XDG_CONFIG_HOME": "relative/dir"}, home)

    assert paths.config_dir == home / ".config" / "reader-assistant"


def test_defaults_are_used_when_no_file_and_no_environment(paths: AppPaths) -> None:
    config = load_config(paths=paths, env={})

    assert config.log_level == "INFO"
    assert config.log_to_file is True
    assert config.log_dir == paths.log_dir
    assert config.models_dir == paths.models_dir
    assert config.default_language == "auto"
    assert config.default_voice is None
    assert config.speech_rate == 1.0
    assert config.volume == 1.0


def test_values_are_read_from_default_config_file(paths: AppPaths) -> None:
    _write_config(
        paths.config_file,
        'log_level = "debug"\n'
        "log_to_file = false\n"
        'default_language = "RU"\n'
        'default_voice = "ru_RU-irina-medium"\n'
        "speech_rate = 1.25\n"
        "volume = 0.5\n",
    )

    config = load_config(paths=paths, env={})

    assert config.log_level == "DEBUG"
    assert config.log_to_file is False
    assert config.default_language == "ru"
    assert config.default_voice == "ru_RU-irina-medium"
    assert config.speech_rate == 1.25
    assert config.volume == 0.5


def test_integer_numbers_are_accepted_for_float_settings(paths: AppPaths) -> None:
    _write_config(paths.config_file, "speech_rate = 2\nvolume = 0\n")

    config = load_config(paths=paths, env={})

    assert config.speech_rate == 2.0
    assert isinstance(config.speech_rate, float)
    assert config.volume == 0.0


def test_explicit_config_file_overrides_default_location(paths: AppPaths, home: Path) -> None:
    _write_config(paths.config_file, 'default_language = "en"\n')
    explicit = _write_config(home / "other.toml", 'default_language = "he"\n')

    config = load_config(paths=paths, env={}, config_file=explicit)

    assert config.default_language == "he"


def test_missing_explicit_config_file_is_an_error(paths: AppPaths, home: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(paths=paths, env={}, config_file=home / "missing.toml")


def test_environment_overrides_file_values(paths: AppPaths, home: Path) -> None:
    _write_config(paths.config_file, 'log_level = "WARNING"\nmodels_dir = "/from/file"\n')

    config = load_config(
        paths=paths,
        env={ENV_LOG_LEVEL: "error", ENV_MODELS_DIR: str(home / "models")},
    )

    assert config.log_level == "ERROR"
    assert config.models_dir == home / "models"


def test_blank_models_dir_environment_value_is_ignored(paths: AppPaths) -> None:
    config = load_config(paths=paths, env={ENV_MODELS_DIR: "   "})

    assert config.models_dir == paths.models_dir


def test_malformed_toml_is_reported_as_config_error(paths: AppPaths) -> None:
    _write_config(paths.config_file, "log_level = [unclosed\n")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(paths=paths, env={})


def test_unknown_key_is_rejected_to_catch_typos(paths: AppPaths) -> None:
    _write_config(paths.config_file, 'defualt_language = "en"\n')

    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(paths=paths, env={})


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('log_level = "TRACE"', "log_level must be one of"),
        ('default_language = "fr"', "default_language must be one of"),
        ("speech_rate = 0.1", "speech_rate must be between"),
        ("speech_rate = 9", "speech_rate must be between"),
        ("volume = 1.5", "volume must be between"),
        ('speech_rate = "fast"', "must be a number"),
        ("volume = true", "must be a number"),
        ("log_to_file = 1", "must be true or false"),
        ('log_dir = ""', "non-empty path"),
        ("default_voice = 3", "must be a string"),
    ],
)
def test_invalid_values_are_rejected(paths: AppPaths, line: str, message: str) -> None:
    _write_config(paths.config_file, line + "\n")

    with pytest.raises(ConfigError, match=message):
        load_config(paths=paths, env={})


def test_invalid_environment_log_level_is_rejected(paths: AppPaths) -> None:
    with pytest.raises(ConfigError, match="log_level must be one of"):
        load_config(paths=paths, env={ENV_LOG_LEVEL: "LOUD"})


def test_empty_default_voice_means_engine_default(paths: AppPaths) -> None:
    _write_config(paths.config_file, 'default_voice = ""\n')

    config = load_config(paths=paths, env={})

    assert config.default_voice is None


def test_tilde_in_models_dir_is_expanded(paths: AppPaths) -> None:
    _write_config(paths.config_file, 'models_dir = "~/ra-models"\n')

    config = load_config(paths=paths, env={})

    assert config.models_dir == Path("~/ra-models").expanduser()
    assert "~" not in str(config.models_dir)
