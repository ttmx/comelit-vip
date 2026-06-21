"""High-level facade for a Comelit ViP intercom over the LAN.

``Intercom`` owns the whole session lifecycle — load credentials, open the TCP
connection, authenticate, and initialise the ViP configuration — so callers
don't repeat that boilerplate. Credentials are bootstrapped from the panel's
local installer UI.
The low-level :class:`~comelit.viper.ViperClient` is still reachable as
``intercom.client`` for anything the facade doesn't wrap.

    from comelit import Intercom

    with Intercom.from_secrets() as panel:
        panel.open_door()                 # buzz the entrance relay
        with panel.video(hd=True) as v:   # live H.264 + optional audio
            for nal in v.h264():
                ...
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .credentials import ViperCredentials
from .viper import CtpPacket, RingEvent, VideoStream, ViperClient

# Fallbacks used only when the secrets file doesn't carry an explicit value.
DEFAULT_SOURCE = "SB0000011"
DEFAULT_ENTRANCE = "SB100001"


class Intercom:
    """A connected ViP session: door, rings, video and two-way audio."""

    def __init__(self, credentials: ViperCredentials, *, timeout: float | None = 10.0):
        self.credentials = credentials
        self.cfg = credentials.ensure_connection_config()
        self.client = ViperClient(
            self.cfg["panel_host"],
            self.cfg.get("panel_port", 64100),
            timeout=timeout,
        )
        self._connected = False

    @classmethod
    def from_secrets(
        cls, path: str | Path | None = None, *, timeout: float | None = 10.0
    ) -> "Intercom":
        """Build an intercom from a ``secrets.json`` file (default path resolved
        from ``$COMELIT_SECRETS`` / ``./secrets.json`` / ``~/.config/comelit``)."""
        return cls(ViperCredentials(path), timeout=timeout)

    # --- addresses -------------------------------------------------------
    @property
    def source(self) -> str:
        """This client's ViP address (the apartment unit we act as)."""
        return self.cfg.get("source_address", DEFAULT_SOURCE)

    @property
    def entrance(self) -> str:
        """The entrance panel's ViP address (door / actuator target)."""
        return (
            self.cfg.get("entrance_address")
            or self.cfg.get("door_address")
            or DEFAULT_ENTRANCE
        )

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> "Intercom":
        """Open the LAN connection, authenticate, and initialise the session.

        Idempotent: calling it again on a live session is a no-op.
        """
        if self._connected:
            return self
        self.client.connect()
        self.credentials.ensure_authenticated(self.client)
        self.client.get_configuration("none")
        self._connected = True
        return self

    def close(self):
        self.client.close()
        self._connected = False

    def __enter__(self) -> "Intercom":
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # --- actions ---------------------------------------------------------
    def open_door(self, relay: int = 1, *, target: str | None = None) -> CtpPacket:
        """Open an entrance relay (default relay 1 on the configured entrance)."""
        return self.client.open_door(self.source, target or self.entrance, relay)

    def rings(self) -> Iterator[RingEvent]:
        """Yield doorbell/call events addressed to this unit until interrupted."""
        return self.client.listen_rings(self.source)

    def video(
        self,
        *,
        hd: bool = False,
        resolution: tuple[int, int] | None = None,
        bitrate: int | None = None,
        target: str | None = None,
    ) -> VideoStream:
        """Start a receive-only video call to the entrance panel.

        Returns a :class:`~comelit.viper.VideoStream` context manager. Call
        ``stream.enable_audio()`` on it to also receive panel audio and to be
        able to talk back with ``stream.send_audio_pcm(...)``.
        """
        return self.client.open_video_stream(
            self.source,
            target or self.entrance,
            hd=hd,
            resolution=resolution,
            bitrate=bitrate,
        )

    def configuration(self) -> dict:
        """Re-fetch and return the panel configuration document."""
        return self.client.get_configuration("none")
