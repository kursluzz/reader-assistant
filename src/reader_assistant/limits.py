"""Size limits shared by the clipboard, IPC and service layers."""

from typing import Final

# Largest text, in UTF-8 bytes, accepted for speech. This is a temporary V1 limit:
# synthesis runs before any sound plays, so long text means a long wait. Chunked
# synthesis, planned for M6, will allow longer input.
MAX_TEXT_BYTES: Final = 5_000
