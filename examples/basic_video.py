#!/usr/bin/env python3
"""Record ten seconds of entrance-camera video to ``door.h264``.

The first run retrieves and caches a LAN token through the installer UI.
Later runs reuse that token and do not contact the web UI. The output is raw
H.264 video that VLC and ffplay can open directly.
"""
import getpass
import time
from pathlib import Path

from comelit import Intercom

OUTPUT = Path("door.h264")
SECONDS = 10
PANEL_IP = "192.168.55.4"

with Intercom.from_installer(
    PANEL_IP,
    getpass.getpass("Installer password: "),
) as panel:
    print(f"recording {panel.entrance} for {SECONDS} seconds...")
    deadline = time.monotonic() + SECONDS

    with panel.video(hd=True) as video, OUTPUT.open("wb") as output:
        for h264_unit in video.h264():
            output.write(h264_unit)
            if time.monotonic() >= deadline:
                break

print(f"saved {OUTPUT}")
print(f"play it with: ffplay {OUTPUT}")
