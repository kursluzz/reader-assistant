"""Play a WAV file through the real audio player, with a pause, resume and stop.

This needs the audio extra and the system library libportaudio2:

    sudo apt install libportaudio2
    uv run --extra audio python scripts/play_sample.py SAMPLE.wav

Timeline: play from the start, pause after 1.5 s, resume after 3 s, stop after 5 s.
Each step is printed so the result can be checked by ear and by the log.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

from reader_assistant.audio.player import AudioPlayer
from reader_assistant.audio.sink import AudioDeviceError, default_sink
from reader_assistant.engines.base import AudioBuffer


def read_wav(path: Path) -> AudioBuffer:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected mono 16-bit WAV")
        return AudioBuffer(
            pcm16=handle.readframes(handle.getnframes()), sample_rate=handle.getframerate()
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("wav", type=Path)
    args = parser.parse_args()

    audio = read_wav(args.wav)
    print(f"playing {args.wav.name}: {audio.duration_seconds:.2f} s at {audio.sample_rate} Hz")

    player = AudioPlayer(default_sink)
    try:
        player.play(audio)
    except AudioDeviceError as exc:
        print(f"no audio output: {exc}", file=sys.stderr)
        return 1

    start = time.monotonic()
    schedule = [(1.5, "pause"), (3.0, "resume"), (5.0, "stop")]
    for at, action in schedule:
        time.sleep(max(0.0, at - (time.monotonic() - start)))
        if action == "pause":
            player.pause()
        elif action == "resume":
            player.resume()
        else:
            player.stop()
        print(f"{action:<7} -> state {player.state.value}")

    if player.last_error:
        print(f"error: {player.last_error}", file=sys.stderr)
        return 1
    player.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
