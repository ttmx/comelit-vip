#!/usr/bin/env python3
"""Full-duplex demo: record the panel's video + audio at the same time, and
optionally talk back from a WAV file.

Video is written as Annex-B H.264 (mux with `ffmpeg -r 16 -i out.h264 -c copy
out.mp4`), audio as an 8 kHz mono WAV. Recording both at once exercises the
background media pump. Talk-back (`--talk FILE --yes`) makes noise at the door.
"""
import argparse
import pathlib
import threading
import time
import wave

from comelit import Intercom, g711


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=pathlib.Path, default=pathlib.Path("out.h264"))
    parser.add_argument("--audio", type=pathlib.Path, default=pathlib.Path("out.wav"))
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--hd", action="store_true")
    parser.add_argument("--talk", type=pathlib.Path, help="WAV to play at the door")
    parser.add_argument("--yes", action="store_true", help="confirm talk makes noise")
    args = parser.parse_args()

    stop = threading.Event()
    try:
        with Intercom.from_secrets(timeout=15) as panel:
            with panel.video(hd=args.hd) as stream:
                stream.enable_audio()

                def record_video():
                    with args.video.open("wb") as f:
                        for unit in stream.h264():
                            f.write(unit)
                            if stop.is_set():
                                return

                def record_audio():
                    with wave.open(str(args.audio), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(g711.SAMPLE_RATE)
                        for pcm in stream.audio():
                            wav.writeframes(pcm)
                            if stop.is_set():
                                return

                threads = [
                    threading.Thread(target=record_video, daemon=True),
                    threading.Thread(target=record_audio, daemon=True),
                ]
                for t in threads:
                    t.start()

                if args.talk and args.yes:
                    import av

                    resampler = av.AudioResampler(
                        format="s16", layout="mono", rate=g711.SAMPLE_RATE
                    )
                    pcm = bytearray()
                    container = av.open(str(args.talk))
                    for frame in container.decode(audio=0):
                        for out in resampler.resample(frame):
                            pcm.extend(bytes(out.planes[0])[: out.samples * 2])
                    container.close()
                    # Lead with silence to cover the panel's ~4.4s voice warm-up.
                    silence = b"\x00\x00" * int(g711.SAMPLE_RATE * 4.5)
                    stream.send_audio_pcm(silence + bytes(pcm))

                time.sleep(args.seconds)
                stop.set()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()

    print(f"wrote {args.video} and {args.audio}", flush=True)


if __name__ == "__main__":
    main()
