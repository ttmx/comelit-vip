#!/usr/bin/env python3
"""Talk back to the entrance panel: send an audio file as G.711 a-law audio.

WARNING: this plays out of the entrance-panel loudspeaker at the door -- it makes
audible noise outside. Only run it when you intend to.

The file is resampled to 8 kHz mono 16-bit (via PyAV; `uv sync --extra talk`)
and streamed in real time. (`comelit talk` is the installed
CLI equivalent.)
"""
import argparse
import pathlib
import sys
import time

import av

from comelit import Intercom, g711


def load_pcm_8k_mono(path: pathlib.Path) -> bytes:
    """Decode any audio file to signed 16-bit little-endian 8 kHz mono PCM."""
    container = av.open(str(path))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=g711.SAMPLE_RATE)
    pcm = bytearray()
    for frame in container.decode(audio=0):
        for out in resampler.resample(frame):
            pcm.extend(bytes(out.planes[0])[: out.samples * 2])
    for out in resampler.resample(None):
        pcm.extend(bytes(out.planes[0])[: out.samples * 2])
    container.close()
    return bytes(pcm)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=pathlib.Path, help="audio file to play")
    parser.add_argument("--yes", action="store_true", help="confirm: this makes noise at the door")
    parser.add_argument(
        "--warmup",
        type=float,
        default=4.5,
        help="seconds of leading silence to cover the panel's voice warm-up (0 to disable)",
    )
    args = parser.parse_args()
    if not args.yes:
        print("Refusing without --yes: this plays sound at the door.", file=sys.stderr)
        return 2

    pcm = load_pcm_8k_mono(args.audio)
    print(f"loaded {len(pcm)//2} samples ({len(pcm)/2/g711.SAMPLE_RATE:.1f}s)", flush=True)

    with Intercom.from_secrets(timeout=15) as panel:
        with panel.video() as stream:
            stream.enable_audio()
            # The panel takes ~4.4 s after VOICESTATUS to open its voice path;
            # send leading silence to keep the RTP flow continuous, then the audio.
            silence = b"\x00\x00" * int(g711.SAMPLE_RATE * args.warmup)
            print(f"warm-up {args.warmup}s, then talking...", flush=True)
            stream.send_audio_pcm(silence + pcm)  # real-time paced
            print("done talking", flush=True)
            time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
