"""Python client for a Comelit ViP intercom over the LAN.

Reverse-engineered from the Comelit app ``com.comelit.bigapp`` 7.4.0-4. Supports
opening the entrance relay, receiving ring/call events, live H.264 video, and
two-way G.711 audio — all directly over the local network (TCP :64100).

Start with :class:`Intercom`, the high-level facade::

    from comelit import Intercom

    with Intercom.from_secrets() as panel:
        panel.open_door()

The lower-level ``ViperClient`` and ``VideoStream`` LAN protocol building
blocks remain available for advanced use.
"""
from . import g711
from .credentials import ViperCredentials
from .intercom import Intercom
from .web import (
    PanelBackup,
    PanelUser,
    PanelWebClient,
    PanelWebError,
    parse_panel_backup,
    parse_users_backup,
)
from .viper import (
    CtpPacket,
    H264Depacketizer,
    RingEvent,
    RtpPacket,
    VideoStream,
    ViperClient,
)

__version__ = "0.1.0"

__all__ = [
    "Intercom",
    "ViperCredentials",
    "ViperClient",
    "VideoStream",
    "RingEvent",
    "RtpPacket",
    "CtpPacket",
    "H264Depacketizer",
    "PanelUser",
    "PanelBackup",
    "PanelWebClient",
    "PanelWebError",
    "parse_users_backup",
    "parse_panel_backup",
    "g711",
    "__version__",
]
