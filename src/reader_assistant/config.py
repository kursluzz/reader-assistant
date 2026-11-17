"""Application configuration: defaults, TOML file and environment overrides.

Precedence (lowest to highest):

1. Built-in defaults (see :func:`default_config`).
2. The TOML file at ``$XDG_CONFIG_HOME/reader-assistant/config.toml`` (or ``--config``).
3. Environment variables ``RA_LOG_LEVEL`` and ``RA_MODELS_DIR``.
4. Command-line options (applied by the CLI after :func:`load_config`).

The module is deliberately free of global state: every function receives the
environment and home directory explicitly so that it can be tested in isolation.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

APP_DIR_NAME: Final = "reader-assistant"
CONFIG_FILE_NAME: Final = "config.toml"
LOG_FILE_NAME: Final = "reader-assistant.log"

ENV_LOG_LEVEL: Final = "RA_LOG_LEVEL"
ENV_MODELS_DIR: Final = "RA_MODELS_DIR"

LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
# "auto" means automatic language identification. The other codes are the
# product languages (English, Russian, Hebrew).
SUPPORTED_LANGUAGES: Final = frozenset({"auto", "en", "ru", "he"})

MIN_SPEECH_RATE: Final = 0.25
MAX_SPEECH_RATE: Final = 4.0


class ConfigError(Exception):
    """Raised when the configuration file or an override is malformed or invalid."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Per-user directories following the XDG Base Directory specification."""

    config_dir: Path
    data_dir: Path
    state_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILE_NAME

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Effective, validated application settings."""

    log_level: str
    log_to_file: bool
    log_dir: Path
    models_dir: Path
    default_language: str
    default_voice: str | None
    speech_rate: float
    volume: float


def resolve_paths(env: Mapping[str, str], home: Path) -> AppPaths:
    """Resolve XDG directories for the app.

    Per the XDG specification, an environment variable that is empty or holds a
    relative path is ignored and the default under ``home`` is used.
    """

    def _base(variable: str, default_relative: str) -> Path:
        value = env.get(variable, "")
        root = Path(value) if value and Path(value).is_absolute() else home / default_relative
        return root / APP_DIR_NAME

    return AppPaths(
        config_dir=_base("XDG_CONFIG_HOME", ".config"),
        data_dir=_base("XDG_DATA_HOME", ".local/share"),
        state_dir=_base("XDG_STATE_HOME", ".local/state"),
    )


def default_config(paths: AppPaths) -> AppConfig:
    """Return the built-in defaults for the given directory layout."""
    return AppConfig(
        log_level="INFO",
        log_to_file=True,
        log_dir=paths.log_dir,
        models_dir=paths.models_dir,
        default_language="auto",
        default_voice=None,
        speech_rate=1.0,
        volume=1.0,
    )


def load_config(
    *,
    paths: AppPaths,
    env: Mapping[str, str],
    config_file: Path | None = None,
) -> AppConfig:
    """Load the effective configuration.

    If ``config_file`` is given it must exist. Otherwise the default file location
    is used when present and silently skipped when absent.

    Raises:
        ConfigError: if the file cannot be parsed, contains unknown keys, or holds
            invalid values; or if an explicitly requested file does not exist.
    """
    config = default_config(paths)

    path = config_file if config_file is not None else paths.config_file
    if path.exists():
        config = _apply_file(config, path)
    elif config_file is not None:
        raise ConfigError(f"config file not found: {path}")

    return _apply_env(config, env)


def _apply_file(config: AppConfig, path: Path) -> AppConfig:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc

    unknown = sorted(set(data) - _FILE_KEYS)
    if unknown:
        raise ConfigError(f"{path}: unknown setting(s): {', '.join(unknown)}")

    updates = {key: _coerce(key, value, source=str(path)) for key, value in data.items()}
    return _validate(replace(config, **updates))


def _apply_env(config: AppConfig, env: Mapping[str, str]) -> AppConfig:
    updates: dict[str, Any] = {}
    if (level := env.get(ENV_LOG_LEVEL)) is not None:
        updates["log_level"] = level
    if (models := env.get(ENV_MODELS_DIR)) is not None and models.strip():
        updates["models_dir"] = models
    if not updates:
        return config
    coerced = {key: _coerce(key, value, source="environment") for key, value in updates.items()}
    return _validate(replace(config, **coerced))


# Keys accepted in the TOML file. Unknown keys are rejected to catch typos early.
_FILE_KEYS: Final = frozenset(
    {
        "log_level",
        "log_to_file",
        "log_dir",
        "models_dir",
        "default_language",
        "default_voice",
        "speech_rate",
        "volume",
    }
)


def _coerce(key: str, value: object, *, source: str) -> Any:
    """Convert a raw TOML/environment value to the type expected for ``key``."""
    if key in {"log_dir", "models_dir"}:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{source}: '{key}' must be a non-empty path string")
        return Path(value).expanduser()
    if key == "log_level":
        if not isinstance(value, str):
            raise ConfigError(f"{source}: 'log_level' must be a string")
        return value.upper()
    if key == "log_to_file":
        if not isinstance(value, bool):
            raise ConfigError(f"{source}: 'log_to_file' must be true or false")
        return value
    if key == "default_language":
        if not isinstance(value, str):
            raise ConfigError(f"{source}: 'default_language' must be a string")
        return value.lower()
    if key == "default_voice":
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ConfigError(f"{source}: 'default_voice' must be a string")
        return value
    if key in {"speech_rate", "volume"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{source}: '{key}' must be a number")
        return float(value)
    raise ConfigError(f"{source}: unknown setting '{key}'")  # pragma: no cover - guarded above


def _validate(config: AppConfig) -> AppConfig:
    if config.log_level not in LOG_LEVELS:
        raise ConfigError(
            f"log_level must be one of {', '.join(sorted(LOG_LEVELS))}, got {config.log_level!r}"
        )
    if config.default_language not in SUPPORTED_LANGUAGES:
        raise ConfigError(
            "default_language must be one of "
            f"{', '.join(sorted(SUPPORTED_LANGUAGES))}, got {config.default_language!r}"
        )
    if not MIN_SPEECH_RATE <= config.speech_rate <= MAX_SPEECH_RATE:
        raise ConfigError(
            f"speech_rate must be between {MIN_SPEECH_RATE} and {MAX_SPEECH_RATE}, "
            f"got {config.speech_rate}"
        )
    if not 0.0 <= config.volume <= 1.0:
        raise ConfigError(f"volume must be between 0.0 and 1.0, got {config.volume}")
    return config
