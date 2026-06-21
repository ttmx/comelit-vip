#!/usr/bin/env python3
"""Diagnostic: enable audio and report the inbound media (codec, packet size,
packetization) for both audio and video, using the stream iterators.

Confirms the panel streams G.711 a-law (RTP PT 8, 160-byte / 20 ms payloads) on
the audio channel and H.264 (PT 99) on the video channel. RX-only: makes no noise
at the door.
"""
import statistics
import threading
import time

from comelit import Intercom


def summarize(label, packets):
    if not packets:
        print(f"{label}: no packets")
        return
    pts = {p.payload_type for p in packets}
    sizes = [len(p.payload) for p in packets]
    tss = sorted({p.timestamp for p in packets})
    deltas = [b - a for a, b in zip(tss, tss[1:]) if b > a]
    d = int(statistics.median(deltas)) if deltas else 0
    print(
        f"{label}: count={len(packets)} PT={sorted(pts)} "
        f"payload min/med/max={min(sizes)}/{int(statistics.median(sizes))}/{max(sizes)} "
        f"ts_delta_med={d}"
    )


def main():
    audio, video = [], []
    stop = threading.Event()
    try:
        with Intercom.from_secrets(timeout=15) as panel:
            with panel.video() as stream:
                stream.enable_audio()  # VOICESTATUS + audio mediareq -> panel streams PT 8

                def drain(it, sink):
                    for p in it:
                        sink.append(p)
                        if stop.is_set():
                            return

                threads = [
                    threading.Thread(
                        target=drain,
                        args=(stream.audio_packets(), audio),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=drain,
                        args=(stream.packets(), video),
                        daemon=True,
                    ),
                ]
                for t in threads:
                    t.start()
                time.sleep(12)
                stop.set()
    finally:
        stop.set()

    summarize("audio", audio)
    summarize("video", video)


if __name__ == "__main__":
    main()
