"""Size limits shared by the clipboard, IPC and service layers."""

from typing import Final

# Largest text, in UTF-8 bytes, that the application will accept for speech.
MAX_TEXT_BYTES: Final = 50_000
