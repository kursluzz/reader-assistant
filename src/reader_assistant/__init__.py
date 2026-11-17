"""Reader Assistant: local, offline text-to-speech for clipboard text."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("reader-assistant")
except PackageNotFoundError:  # pragma: no cover - only when run without installing
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
