"""Audio playback: a background player with play, pause, resume, stop and volume.

- ``player``: :class:`AudioPlayer`, the engine-independent playback controller.
- ``sink``: output sinks. ``SoundDeviceSink`` uses PortAudio through the optional
  ``audio`` extra; tests use an in-memory fake.
"""
