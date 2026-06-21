#!/usr/bin/env python3
"""Open the configured entrance relay over the LAN."""
from comelit import Intercom

with Intercom.from_secrets() as panel:
    result = panel.open_door(relay=1)
    print("door opened:", result)
