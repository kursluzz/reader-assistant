# Audio playback (M3)

## Linux dependencies

The audio backend is PortAudio, through the Python package `sounddevice`. Install the system library first:

```bash
sudo apt install libportaudio2
uv sync --extra audio          # or: uv sync --all-extras
```

The application still starts without these. If the library is missing, playback reports `audio backend unavailable: PortAudio library not found` and the rest of the app keeps working.

Audio server: the development machine runs PipeWire (user service active). Playback through PipeWire has not yet been verified on real hardware; see Limitations.

## Architecture

```
AudioPlayer  (audio/player.py)           engine-independent controller
   │  takes AudioBuffer (mono 16-bit PCM + sample rate)
   │  opens the sink synchronously; plays on one worker thread
   ▼
AudioSink protocol  (audio/sink.py)      the only place that touches hardware
   ├── SoundDeviceSink   PortAudio via sounddevice (optional extra "audio")
   └── test fakes        in-memory, used by the test suite
```

- `AudioPlayer` knows nothing about engines. Any engine that produces an `AudioBuffer` can use it.
- `AudioSink` is the only hardware boundary. Tests substitute a fake, so the suite never opens an audio device.
- The sink factory is injected into `AudioPlayer`, so no device is opened until `play()` is called.

## Behaviour

| Operation | Behaviour |
|---|---|
| `play(audio)` | Validates the audio, stops any current playback, opens the device on the caller's thread, then starts the worker. Raises `InvalidAudioError` for empty or malformed audio, and `AudioDeviceError` if the device cannot be opened. |
| `pause()` | Takes effect at the next block boundary. No effect unless playing. |
| `resume()` | Continues from the same position. No effect unless paused. |
| `stop()` | Discards queued audio, joins the worker (at most one block, about 0.17 s at 24 kHz), and returns to idle. Idempotent. |
| `set_volume(v)` | Volume in [0, 1], applied per block. Takes effect within about 0.17 s. |
| `wait_until_finished(t)` | Blocks until playback ends or the timeout passes. Intended for scripts and tests, not the GUI thread. |
| `close()` | Stops playback and refuses further `play()` calls. |

States: `IDLE`, `PLAYING`, `PAUSED`.

## Threading and cleanup

- Playback runs on one daemon thread, named `reader-assistant-playback`. The GUI thread only sets flags and waits on a condition variable, so it never blocks on audio.
- Stop and pause are checked between blocks. The worker never calls back into the caller.
- Every playback ends with the sink closed and the worker finished. A normal end drains queued audio first (`close(drain=True)`). Stop and errors discard it (`close(drain=False)`).
- Device failures during playback are recorded in `last_error` and returned to idle. They are not raised on the worker thread.
- `stop()` waits up to 5 s for the worker. If it still runs, `last_error` says so.

## Testing

- `tests/test_audio_player.py`: play, pause and resume, stop, volume, invalid audio, missing devices, device failures, thread cleanup, lazy opening.
- `tests/test_audio_sink.py`: the PortAudio sink, using a fake `sounddevice` module.
- No test opens a real device.

Real hardware check (needs `libportaudio2`):

```bash
uv run --extra audio python scripts/play_sample.py ~/.local/share/reader-assistant/samples/kokoro/en_02.wav
```

It plays the file, pauses at 1.5 s, resumes at 3 s and stops at 5 s, printing each step. Without `libportaudio2` it reports `no audio output` and exits with status 1.

## Limitations

- Pause keeps the audio device open, so there can be a short gap on resume.
- Volume is applied in software, at the cost of some CPU per block.
- Stop latency is up to one block (about 0.17 s at 24 kHz).
- Real-device playback was not tested on this machine, because `libportaudio2` is not installed. It is tested only through the fake sink.

## Concurrency and lifecycle (M5-2)

- Each playback is a session with its own sink, flags and worker thread. Every decision looks at the session it concerns, so a stale worker cannot affect a newer one.
- `_lifecycle` serialises `play` and `close`. It is held while the previous playback is joined and the device is opened. Stop, pause, resume and volume never take it.
- `_cond` guards the session pointer, flags, the stop generation, volume and the last error. It is held only briefly, and never while waiting for a thread or opening a device.
- `request_stop()` is non-blocking: it marks the playback stopped and returns. `stop()` also waits for every stopped playback to finish its cleanup.
- The worker closes its sink exactly once, and runs the end callback only when it finished by itself. A stop or a replacement never runs the callback.
- A stop during device opening cancels that playback, because the stop generation changes.
