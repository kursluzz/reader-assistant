"""Text-to-speech engines behind one interface.

- ``base``: ``TTSEngine`` protocol and engine-neutral types. No heavy imports.
- ``registry``: choose an engine by language. No heavy imports.
- ``kokoro``: English adapter (optional extra ``kokoro``).
- ``piper``: adapter for Piper voices (optional extra ``piper``).

Adapters import their libraries lazily, so the package works without any extra.
"""
