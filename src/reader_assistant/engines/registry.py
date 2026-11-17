"""Select an engine by language, so callers never name a specific engine.

The first engine registered for a language is its default. Later registrations
for the same language are kept as fallbacks in registration order, which lets the
application move to fallback chains without changing callers.

The registry holds references only; it never loads a model. Loading remains the
caller's explicit decision (see ``TTSEngine.load``).
"""

from __future__ import annotations

from collections import defaultdict

from reader_assistant.engines.base import EngineError, TTSEngine


class EngineRegistry:
    """Maps product language codes to the engines that can speak them."""

    def __init__(self) -> None:
        self._engines_by_language: defaultdict[str, list[TTSEngine]] = defaultdict(list)

    def register(self, engine: TTSEngine) -> None:
        """Add ``engine`` for every language it declares.

        Raises:
            ValueError: if the same engine instance is registered twice.
        """
        for language in sorted(engine.languages):
            engines = self._engines_by_language[language]
            if any(existing is engine for existing in engines):
                raise ValueError(f"engine {engine.engine_id!r} already registered for {language}")
            engines.append(engine)

    def engines_for(self, language: str) -> tuple[TTSEngine, ...]:
        """All engines for ``language``, default first. Empty if none."""
        return tuple(self._engines_by_language.get(language, ()))

    def default_for(self, language: str) -> TTSEngine:
        """The default engine for ``language``.

        Raises:
            EngineError: if no engine supports ``language``.
        """
        engines = self.engines_for(language)
        if not engines:
            raise EngineError(f"no TTS engine available for language {language!r}")
        return engines[0]

    def languages(self) -> frozenset[str]:
        """Every language covered by at least one registered engine."""
        return frozenset(lang for lang, engines in self._engines_by_language.items() if engines)
