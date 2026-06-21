#!/usr/bin/env python3
"""Record a fixed-length clip and measure the true framerate from RTP timestamps.

Raw Annex-B H.264 carries no timing, so players guess. The ViP video RTP stream
uses a 90 kHz clock; the spacing between distinct RTP timestamps gives the real
frame interval, which we report so the clip can be muxed with correct timing
(`ffmpeg -r <fps> -i out.h264 -c copy out.mp4`). (`comelit record` is the
installed CLI equivalent.)
"""
import argparse
import pathlib
import statistics
import time

from comelit import Intercom
from comelit.viper import H264Depacketizer

RTP_CLOCK_HZ = 90000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=pathlib.Path, help="raw .h264 output path")
    parser.add_argument("--hd", action="store_true", help="request HD (640x240)")
    parser.add_argument("--bitrate", type=int, default=None, help="encoder bitrate (0=panel default)")
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()

    timestamps: list[int] = []
    frames = bytes_written = 0
    started = None
    with Intercom.from_secrets(timeout=None) as panel:
        quality = "HD" if args.hd else "SD"
        print(f"recording {quality} for {args.seconds}s -> {args.output}", flush=True)
        depacketizer = H264Depacketizer()
        with panel.video(hd=args.hd, bitrate=args.bitrate) as stream, args.output.open("wb") as file:
            for packet in stream.packets():
                if started is None:
                    started = time.monotonic()
                if not timestamps or timestamps[-1] != packet.timestamp:
                    timestamps.append(packet.timestamp)
                for unit in depacketizer.feed(packet):
                    file.write(unit)
                    bytes_written += len(unit)
                    if (unit[4] & 0x1F) in (1, 5):  # coded picture NAL units
                        frames += 1
                if time.monotonic() - started >= args.seconds:
                    break

    elapsed = (time.monotonic() - started) if started else 0.0
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    rtp_fps = RTP_CLOCK_HZ / statistics.median(deltas) if deltas else 0.0
    print(f"done: {frames} frames, {bytes_written} bytes in {elapsed:.1f}s", flush=True)
    if elapsed > 0:
        print(f"  fps (RTP 90kHz timestamps): {rtp_fps:.3f}")
        print(f"  ~bitrate                  : {bytes_written * 8 / elapsed / 1000:.0f} kbps")
    print(f"MEASURED_FPS={rtp_fps:.3f}")


if __name__ == "__main__":
    main()
