#!/usr/bin/env python3
"""Record ten seconds of entrance-camera video to ``door.h264``.

Run ``comelit bootstrap-local PANEL_IP`` once before using this example.
The output is a raw H.264 video that VLC and ffplay can open directly.
"""
import time
from pathlib import Path

from comelit import Intercom

OUTPUT = Path("door.h264")
SECONDS = 10

with Intercom.from_secrets() as panel:
    print(f"recording {panel.entrance} for {SECONDS} seconds...")
    deadline = time.monotonic() + SECONDS

    with panel.video(hd=True) as video, OUTPUT.open("wb") as output:
        for h264_unit in video.h264():
            output.write(h264_unit)
            if time.monotonic() >= deadline:
                break

print(f"saved {OUTPUT}")
print(f"play it with: ffplay {OUTPUT}")
