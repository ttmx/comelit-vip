#!/usr/bin/env python3
"""Print doorbell/call events received directly from the panel over the LAN."""
from comelit import Intercom

# timeout=None: block indefinitely waiting for the next ring.
with Intercom.from_secrets(timeout=None) as panel:
    print(f"listening for rings as {panel.source} ...", flush=True)
    for event in panel.rings():
        door = event.body[2:12].rstrip(b"\x00").decode("ascii", "replace")
        print(
            f"RING {event.received_at.isoformat()} "
            f"door={door} route={event.source}->{event.destination}",
            flush=True,
        )
