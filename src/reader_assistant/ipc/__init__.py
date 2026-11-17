"""Local IPC between the CLI and the running service over a Unix domain socket.

- ``protocol``: newline-delimited JSON messages, validation and size limits.
- ``paths``: where the socket lives.
- ``server``: the service side. Owns the socket file while running.
- ``client``: the CLI side.

This package does not know about the clipboard or the TTS engines.
"""
