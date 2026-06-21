#!/usr/bin/env python3
"""Listen to the entrance-panel microphone over the LAN and write a WAV file.

Opens a call, enables audio (VOICESTATUS), decodes the panel's G.711 a-law stream
to PCM. RX-only: this makes us hear the door; it makes no noise at the door.
(`comelit listen` is the installed CLI equivalent.)
"""
import argparse
import pathlib
import time
import wave

from comelit import Intercom, g711


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=pathlib.Path, help="output .wav path")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args()

    samples = 0
    with Intercom.from_secrets(timeout=15) as panel:
        print(f"listening to {panel.entrance} for {args.seconds}s -> {args.output}", flush=True)
        with panel.video() as stream:
            stream.enable_audio()
            with wave.open(str(args.output), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(g711.SAMPLE_RATE)
                deadline = time.monotonic() + args.seconds
                for pcm in stream.audio():
                    wav.writeframes(pcm)
                    samples += len(pcm) // 2
                    if time.monotonic() >= deadline:
                        break

    print(f"done: {samples} samples ({samples / g711.SAMPLE_RATE:.1f}s of audio)", flush=True)


if __name__ == "__main__":
    main()
