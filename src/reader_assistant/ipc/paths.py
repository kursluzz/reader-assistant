"""Where the service socket lives.

Preferred: ``$XDG_RUNTIME_DIR/reader-assistant/service.sock``. The runtime directory is
per-user, mode 0700, and cleared at logout. If ``XDG_RUNTIME_DIR`` is not set, the socket
falls back to the application's state directory, which is also per-user.

The socket's directory is created with mode 0700 and must belong to the current user.
Those two facts are what restrict access to the socket, so the socket file itself is
also set to 0600.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from reader_assistant.config import APP_DIR_NAME, resolve_paths

SOCKET_NAME: Final = "service.sock"
# Linux limits Unix socket paths to 108 bytes, including the terminating NUL.
_MAX_SOCKET_PATH_BYTES: Final = 107


class SocketPathError(Exception):
    """The socket directory cannot be used safely."""


def socket_path(env: Mapping[str, str], home: Path) -> Path:
    """Return the socket path, creating its directory with mode 0700 if needed."""
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if runtime and Path(runtime).is_absolute():
        base = Path(runtime) / APP_DIR_NAME
    else:
        base = resolve_paths(env, home).state_dir
    _ensure_private_directory(base)
    path = base / SOCKET_NAME
    if len(str(path).encode("utf-8")) > _MAX_SOCKET_PATH_BYTES:
        raise SocketPathError(f"socket path is too long for Unix sockets: {path}")
    return path


def _ensure_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    except OSError as exc:
        raise SocketPathError(f"cannot create {directory}: {exc}") from exc
    if directory.stat().st_uid != os.getuid():
        raise SocketPathError(f"{directory} is not owned by the current user")
