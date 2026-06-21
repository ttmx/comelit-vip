"""LAN-direct Comelit ViP protocol client (TCP :64100), reverse-engineered from the app.

Wire framing (little-endian):
    frame = b'\\x00\\x06' + u16(len(payload)) + u16(handle) + b'\\x00\\x00' + payload
    handle 0x0000 is the management channel; non-zero handles are opened channels.

Channel management (payload on handle 0):
    open       : b'\\xcd\\xab\\x01\\x00' + u32(4+2+1) + NAME(4 ascii) + u16(handle) + b'\\x00'
    open-ack   : b'\\xcd\\xab\\x02\\x00' + u32(4) + u16(handle) + b'\\x00\\x00'
    close      : b'\\xef\\x01\\x03\\x00' + u32(2) + u16(handle)
    close-ack  : b'\\xef\\x01\\x04\\x00' + u32(4) + u16(handle) + b'\\x00\\x00'

On an opened channel the payload is either JSON (UAUT/UCFG/INFO/PUSH/FRCG) or binary (CTPP/RTPC).
JSON requests look like {"message":..,"message-type":"request","message-id":N,..}; the panel
replies with the same message-id and "response-code":200 on success.

Auth: open UAUT, send {"message":"access","user-token":TOKEN,...}, expect response-code 200.
"""
from __future__ import annotations
import json, queue, socket, struct, threading, time, itertools, secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from . import g711

HDR = b"\x00\x06"
MGMT = 0x0000
OPEN_MAGIC = b"\xcd\xab\x01\x00"
OPENACK_MAGIC = b"\xcd\xab\x02\x00"
CLOSE_MAGIC = b"\xef\x01\x03\x00"
CLOSEACK_MAGIC = b"\xef\x01\x04\x00"


@dataclass
class Frame:
    handle: int
    payload: bytes


@dataclass
class CtpPacket:
    """Decoded CTPP channel packet.

    The two connection bytes and sequence/acknowledgement bytes are maintained by
    the native CTP state machine.  ``body`` is the application message.  The
    fixed trailer contains routing addresses as two 10-byte ``logaddr_t`` values.
    """

    flags: int
    connection: bytes
    sequence: int
    acknowledgement: int
    body: bytes
    source: str
    destination: str

    @property
    def opcode(self) -> int | None:
        return struct.unpack_from(">H", self.body)[0] if len(self.body) >= 2 else None

    @property
    def open_door(self) -> tuple[str, int] | None:
        """Return ``(target_address, relay)`` for an OPEN DOOR (opcode 0x002d)."""
        if self.opcode != 0x002D or len(self.body) != 13:
            return None
        return decode_logaddr(self.body[2:12]), self.body[12]


@dataclass
class RingEvent:
    source: str
    destination: str
    connection: bytes
    sequence: int
    body: bytes
    received_at: datetime


@dataclass
class RtpPacket:
    """One RTP packet received from an RTPC video channel."""

    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    raw: bytes


def parse_rtp_packet(raw: bytes) -> RtpPacket:
    """Parse an RTP v2 packet, including CSRC and extension headers."""
    if len(raw) < 12:
        raise ValueError(f"RTP packet too short: {len(raw)}")
    first, second = raw[0], raw[1]
    if first >> 6 != 2:
        raise ValueError(f"unsupported RTP version: {first >> 6}")
    csrc_count = first & 0x0F
    offset = 12 + (csrc_count * 4)
    if offset > len(raw):
        raise ValueError("truncated RTP CSRC list")
    if first & 0x10:
        if offset + 4 > len(raw):
            raise ValueError("truncated RTP extension header")
        extension_words = struct.unpack_from(">H", raw, offset + 2)[0]
        offset += 4 + (extension_words * 4)
        if offset > len(raw):
            raise ValueError("truncated RTP extension data")
    end = len(raw)
    if first & 0x20:
        padding = raw[-1]
        if padding == 0 or padding > end - offset:
            raise ValueError("invalid RTP padding")
        end -= padding
    return RtpPacket(
        marker=bool(second & 0x80),
        payload_type=second & 0x7F,
        sequence=struct.unpack_from(">H", raw, 2)[0],
        timestamp=struct.unpack_from(">I", raw, 4)[0],
        ssrc=struct.unpack_from(">I", raw, 8)[0],
        payload=raw[offset:end],
        raw=raw,
    )


class H264Depacketizer:
    """Convert RFC 6184 RTP payloads to Annex-B H.264 NAL units."""

    def __init__(self):
        self._fragment: bytearray | None = None
        self._next_sequence: int | None = None

    def feed(self, packet: RtpPacket) -> list[bytes]:
        payload = packet.payload
        if not payload:
            return []
        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            self._fragment = None
            return [b"\x00\x00\x00\x01" + payload]
        if nal_type == 24:  # STAP-A
            units = []
            offset = 1
            while offset < len(payload):
                if offset + 2 > len(payload):
                    raise ValueError("truncated H.264 STAP-A length")
                size = struct.unpack_from(">H", payload, offset)[0]
                offset += 2
                if size == 0 or offset + size > len(payload):
                    raise ValueError("invalid H.264 STAP-A unit")
                units.append(b"\x00\x00\x00\x01" + payload[offset : offset + size])
                offset += size
            return units
        if nal_type != 28 or len(payload) < 2:  # FU-A
            return []

        start = bool(payload[1] & 0x80)
        end = bool(payload[1] & 0x40)
        reconstructed_header = bytes(((payload[0] & 0xE0) | (payload[1] & 0x1F),))
        if start:
            self._fragment = bytearray(b"\x00\x00\x00\x01" + reconstructed_header)
            self._fragment.extend(payload[2:])
        elif (
            self._fragment is None
            or self._next_sequence is None
            or packet.sequence != self._next_sequence
        ):
            self._fragment = None
            self._next_sequence = None
            return []
        else:
            self._fragment.extend(payload[2:])
        self._next_sequence = (packet.sequence + 1) & 0xFFFF
        if end and self._fragment is not None:
            unit = bytes(self._fragment)
            self._fragment = None
            self._next_sequence = None
            return [unit]
        return []


def decode_logaddr(raw: bytes) -> str:
    """Decode the on-wire 10-byte ViP logical address."""
    if len(raw) != 10:
        raise ValueError(f"logaddr_t must be 10 bytes, got {len(raw)}")
    return raw.rstrip(b"\x00").decode("ascii")


def encode_logaddr(address: str) -> bytes:
    """Encode a ViP logical address as the native 10-byte ``logaddr_t``."""
    raw = address.encode("ascii")
    if len(raw) > 10 or b"\x00" in raw:
        raise ValueError("ViP logical address must be at most 10 ASCII bytes")
    return raw.ljust(10, b"\x00")


def parse_ctp_packet(payload: bytes) -> CtpPacket:
    """Parse one binary payload carried by the CTPP Viper channel.

    Layout observed in native captures:

      flags:u8, version=0x18, connection:2B, seq:u8, ack:u8,
      body_length:u16be, body, zero padding to 4-byte alignment,
      ff ff ff ff, source:logaddr_t, destination:logaddr_t
    """
    if len(payload) < 32:
        raise ValueError(f"CTPP packet too short: {len(payload)}")
    if payload[1] != 0x18:
        raise ValueError(f"unsupported CTPP version byte: {payload[1]:#x}")
    body_len = struct.unpack_from(">H", payload, 6)[0]
    body_end = 8 + body_len
    padding = (-body_len) % 4
    trailer_start = body_end + padding
    expected = trailer_start + 24
    if len(payload) != expected:
        raise ValueError(
            f"CTPP length mismatch: body={body_len}, packet={len(payload)}, "
            f"expected={expected}"
        )
    trailer = payload[trailer_start:]
    if trailer[:4] != b"\xff\xff\xff\xff":
        raise ValueError(f"bad CTPP trailer marker: {trailer[:4].hex()}")
    return CtpPacket(
        flags=payload[0],
        connection=payload[2:4],
        sequence=payload[4],
        acknowledgement=payload[5],
        body=payload[8:body_end],
        source=decode_logaddr(trailer[4:14]),
        destination=decode_logaddr(trailer[14:24]),
    )


def build_ctp_packet(
    *,
    flags: int,
    connection: bytes,
    sequence: int,
    acknowledgement: int,
    body: bytes,
    source: str,
    destination: str,
) -> bytes:
    """Build one binary CTPP channel payload."""
    if len(connection) != 2:
        raise ValueError("CTPP connection id must be two bytes")
    if not 0 <= flags <= 0xFF:
        raise ValueError("CTPP flags must fit in one byte")
    if not 0 <= sequence <= 0xFF or not 0 <= acknowledgement <= 0xFF:
        raise ValueError("CTPP sequence values must fit in one byte")
    return (
        bytes((flags, 0x18))
        + connection
        + bytes((sequence, acknowledgement))
        + struct.pack(">H", len(body))
        + body
        + (b"\x00" * ((-len(body)) % 4))
        + b"\xff\xff\xff\xff"
        + encode_logaddr(source)
        + encode_logaddr(destination)
    )


class ViperClient:
    def __init__(self, host: str, port: int = 64100, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        self._handles = itertools.count(0x7474)   # mimic the app's handle range
        self._msg_id = itertools.count(1)
        self._lock = threading.Lock()

    # --- transport -------------------------------------------------------
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try: self.sock.close()
            finally: self.sock = None

    def _send_frame(self, handle: int, payload: bytes):
        pkt = HDR + struct.pack("<HH", len(payload), handle) + b"\x00\x00" + payload
        with self._lock:
            self.sock.sendall(pkt)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("panel closed connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_frame(self) -> Frame:
        hdr = self._recv_exact(8)
        if hdr[:2] != HDR:
            raise ValueError(f"bad frame magic: {hdr[:2].hex()}")
        length, handle = struct.unpack_from("<HH", hdr, 2)
        return Frame(handle, self._recv_exact(length))

    # --- channel management ---------------------------------------------
    def open_channel(self, name: str, registration: bytes = b"") -> int:
        assert len(name) == 4
        handle = next(self._handles)
        body = (
            OPEN_MAGIC
            + struct.pack("<I", 7)
            + name.encode()
            + struct.pack("<H", handle)
            + b"\x00"
        )
        if registration:
            body += b"\x00" + struct.pack("<I", len(registration)) + registration
        self._send_frame(MGMT, body)
        # expect open-ack on mgmt handle for this handle
        f = self._wait_mgmt(OPENACK_MAGIC, handle)
        return handle

    def close_channel(self, handle: int):
        body = CLOSE_MAGIC + struct.pack("<I", 2) + struct.pack("<H", handle)
        self._send_frame(MGMT, body)

    def _wait_mgmt(self, magic: bytes, handle: int, tries: int = 20) -> Frame:
        for _ in range(tries):
            f = self.recv_frame()
            if f.handle == MGMT and f.payload[:4] == magic:
                ack_handle = struct.unpack_from("<H", f.payload, 8)[0]
                if ack_handle == handle:
                    return f
        raise TimeoutError(f"no {magic.hex()} ack for handle {handle:#06x}")

    def _accept_channel(self, frame: Frame) -> tuple[str, int]:
        """Accept a channel opened by the panel and return ``(name, handle)``."""
        payload = frame.payload
        if frame.handle != MGMT or len(payload) < 15 or payload[:4] != OPEN_MAGIC:
            raise ValueError("frame is not an incoming channel-open request")
        name = payload[8:12].decode("ascii")
        handle = struct.unpack_from("<H", payload, 12)[0]
        ack = OPENACK_MAGIC + struct.pack("<I", 4) + struct.pack("<H", handle) + b"\x00\x00"
        self._send_frame(MGMT, ack)
        return name, handle

    # --- JSON request/response on a channel ------------------------------
    def send_json(self, handle: int, message: str, **fields) -> int:
        mid = next(self._msg_id)
        obj = {"message": message, "message-type": "request", "message-id": mid}
        obj.update(fields)
        self._send_frame(handle, json.dumps(obj).encode())
        return mid

    def request(self, handle: int, message: str, **fields) -> dict:
        mid = self.send_json(handle, message, **fields)
        for _ in range(40):
            f = self.recv_frame()
            if f.handle != handle:
                continue
            try:
                obj = json.loads(f.payload.decode("utf-8", "replace"))
            except Exception:
                continue
            if obj.get("message-id") == mid and obj.get("message-type") in ("response", "notification"):
                return obj
        raise TimeoutError(f"no response to {message} (id {mid})")

    # --- high level ------------------------------------------------------
    def authenticate(self, user_token: str) -> dict:
        h = self.open_channel("UAUT")
        try:
            resp = self.request(h, "access", **{"user-token": user_token})
            if resp.get("response-code") != 200:
                raise PermissionError(f"auth failed: {resp}")
            return resp
        finally:
            self.close_channel(h)

    def get_configuration(self, addressbooks: str = "none") -> dict:
        """Initialize the ViP session and return the panel configuration."""
        h = self.open_channel("UCFG")
        try:
            resp = self.request(h, "get-configuration", addressbooks=addressbooks)
            if resp.get("response-code") != 200:
                raise RuntimeError(f"get-configuration failed: {resp}")
            return resp
        finally:
            self.close_channel(h)

    def listen_rings(self, source: str):
        """Yield LAN doorbell events from the registered CTPP channel.

        Incoming call setup uses CTP opcode ``0x0001``. Each packet is
        acknowledged so the panel does not retransmit it indefinitely.
        """
        handle = self.open_channel("CTPP", encode_logaddr(source))
        seen: set[tuple[bytes, int]] = set()
        try:
            while True:
                frame = self.recv_frame()
                if frame.handle != handle:
                    continue
                try:
                    packet = parse_ctp_packet(frame.payload)
                except ValueError:
                    continue

                # For an inbound connection the peer owns the low-15-bit id;
                # our direction is represented by setting bit 15.
                peer_id = struct.unpack(">H", packet.connection)[0]
                local_connection = struct.pack(">H", peer_id | 0x8000)
                ack = build_ctp_packet(
                    flags=0x00,
                    connection=local_connection,
                    sequence=packet.acknowledgement,
                    acknowledgement=(packet.sequence + 1) & 0xFF,
                    body=b"",
                    source=source,
                    destination=packet.source,
                )
                self._send_frame(handle, ack)

                key = (packet.connection, packet.sequence)
                if packet.opcode == 0x0001 and key not in seen:
                    seen.add(key)
                    yield RingEvent(
                        source=packet.source,
                        destination=packet.destination,
                        connection=packet.connection,
                        sequence=packet.sequence,
                        body=packet.body,
                        received_at=datetime.now(timezone.utc),
                    )
        finally:
            self.close_channel(handle)

    def open_door(
        self,
        source: str,
        target: str,
        relay: int = 1,
        *,
        tries: int = 40,
    ) -> CtpPacket:
        """Open a relay using the LAN-direct, out-of-call CTP transaction.

        ``source`` is this client's full ViP address (for example ``SB0000011``);
        ``target`` is the entrance panel address (for example ``SB100001``).
        The panel reports success as RELEASE opcode ``0x000e`` with cause ``0``.
        """
        if not 0 <= relay <= 0xFF:
            raise ValueError("relay must fit in one byte")

        # CTPP channel opening is special: the app appends the local 10-byte
        # logaddr_t so the panel can register and route this CTP endpoint.
        handle = self.open_channel("CTPP", encode_logaddr(source))
        try:
            # Native ctp_new_connection_out() chooses a non-zero 15-bit id.
            # The wire value is network-order; the peer direction sets bit 15
            # (captured 7d90 -> fd90).
            connection_id = secrets.randbelow(0x7FFE) + 1
            local_connection = struct.pack(">H", connection_id)
            peer_connection = struct.pack(">H", connection_id | 0x8000)
            sequence = secrets.randbelow(256)
            peer_sequence = secrets.randbelow(256)
            body = struct.pack(">H", 0x002D) + encode_logaddr(target) + bytes((relay,))

            start = build_ctp_packet(
                flags=0xC0,
                connection=local_connection,
                sequence=sequence,
                acknowledgement=peer_sequence,
                body=body,
                source=source,
                destination=target,
            )
            self._send_frame(handle, start)

            response = None
            seen: list[str] = []
            for _ in range(tries):
                try:
                    frame = self.recv_frame()
                except TimeoutError as exc:
                    detail = ", ".join(seen) if seen else "no channel packets"
                    raise TimeoutError(f"no CTPP response to open-door request ({detail})") from exc
                if frame.handle != handle:
                    seen.append(f"handle={frame.handle:#06x} len={len(frame.payload)}")
                    continue
                try:
                    packet = parse_ctp_packet(frame.payload)
                except ValueError as exc:
                    seen.append(f"invalid CTPP len={len(frame.payload)}: {exc}")
                    continue
                seen.append(
                    f"flags={packet.flags:#04x} conn={packet.connection.hex()} "
                    f"seq={packet.sequence:#04x} ack={packet.acknowledgement:#04x} "
                    f"opcode={packet.opcode!r} {packet.source}->{packet.destination}"
                )
                if (
                    packet.connection == peer_connection
                    and packet.source == target
                    and packet.destination == source
                    and packet.acknowledgement == ((sequence + 1) & 0xFF)
                    and packet.opcode == 0x000E
                ):
                    response = packet
                    break
            if response is None:
                raise TimeoutError(
                    "no matching CTPP response to open-door request: " + ", ".join(seen)
                )

            next_sequence = (sequence + 1) & 0xFF
            next_ack = (response.sequence + 1) & 0xFF
            common = dict(
                connection=local_connection,
                sequence=next_sequence,
                acknowledgement=next_ack,
                body=b"",
                source=source,
                destination=target,
            )
            self._send_frame(handle, build_ctp_packet(flags=0x00, **common))
            self._send_frame(handle, build_ctp_packet(flags=0x20, **common))

            if response.opcode != 0x000E:
                raise RuntimeError(f"unexpected open-door response opcode: {response.opcode!r}")
            cause = response.body[2] if len(response.body) >= 3 else None
            if cause != 0:
                raise RuntimeError(f"panel rejected open-door request: cause={cause!r}")
            return response
        finally:
            self.close_channel(handle)

    def open_video_stream(
        self,
        source: str,
        target: str,
        *,
        hd: bool = False,
        resolution: tuple[int, int] | None = None,
        bitrate: int | None = None,
        tries: int = 100,
    ) -> "VideoStream":
        """Start a LAN self-ignition call and return its receive-only video stream.

        Authentication and ``get_configuration()`` must have already completed on
        this connection. The returned object owns the temporary CTPP/RTPC/UDPM
        channels and must be closed (or used as a context manager).

        Pass ``hd=True`` (or an explicit ``resolution=(w, h)``) to request the
        high-definition stream. This mirrors the app's "HD" button, which raises
        the preferred video resolution to the panel's advertised maximum
        (800x480) instead of the default SD 320x240, and sets a bitrate target of
        1000. On this MSVF panel the top mode is actually 640x240; see
        ``VideoStream`` for the supported modes.

        ``bitrate`` overrides the requested encoder bitrate (0 = let the panel
        decide, the SD default). ``hd=True`` defaults it to ``HD_BITRATE`` (1000).
        """
        if resolution is None:
            resolution = (
                VideoStream.HD_RESOLUTION if hd else VideoStream.SD_RESOLUTION
            )
        if bitrate is None:
            bitrate = VideoStream.HD_BITRATE if hd else 0
        stream = VideoStream(
            self, source, target, resolution=resolution, bitrate=bitrate
        )
        stream.open(tries=tries)
        return stream


class VideoStream:
    """Receive-only local ViP video call."""

    _CAPABILITIES = bytes.fromhex("00 03 49 00 27 00 00 00")
    _AUDIO_OFFER = bytes.fromhex("00 11 18 02 00 00 00 00")
    # Same media request with media-type bit 0x80 set = "stop" (csp_send_mediareq,
    # media-type 0x98); the app sends this to tear an audio stream down.
    _AUDIO_OFFER_STOP = bytes.fromhex("00 11 98 02 00 00 00 00")
    _VIDEO_OFFER_PREFIX = bytes.fromhex("00 11 14 32 00 00 00 00")

    # The video offer (CTPP opcode 0x0011, csp_send_mediareq26) carries a 16-byte
    # settings block (body[10:26]) that the panel reads to pick the stream it
    # sends. Fully reversed from libvipcomelit.so; decoded as little-endian:
    #   [10:12] u16  max RTP payload size (0xffff)  <- setVipUnitMaxRtpPayloadSize
    #   [12:16] u32  bitrate (0 = disabled)         <- setVipUnitBitrate(en, value)
    #   [16:18] u16  MAX  xMax   } setVipUnitRtpMaxVideoResolution(xMax, yMax, fps)
    #   [18:20] u16  MAX  yMax   }
    #   [20:22] u16  PREF xPref  } setVipUnitRtpPreferredVideoResolution(xPref, yPref)
    #   [22:24] u16  PREF yPref  }
    #   [24]    u8   fps
    #   [25]    u8   (0)
    # The panel streams the PREFERRED resolution, quantized to its supported modes.
    # The app's "HD" button is setVipUnitRtpPreferredVideoResolution(800, 480) +
    # setVipUnitBitrate(true, 1000). This MSVF entrance panel exposes only two
    # video modes (verified by sweeping preferred values live):
    #   SD: request <=~352 wide -> 320x240
    #   HD: request  >~352 wide -> 640x240   (height is always 240)
    # so requesting 800x480 yields the panel's top mode, 640x240 (~2x SD pixels).
    SD_RESOLUTION = (320, 240)  # Constants.DEFAULT_MIN_RESOLUTION
    HD_RESOLUTION = (800, 480)  # Constants.DEFAULT_MAX_RESOLUTION
    HD_BITRATE = 1000  # Utilities.updateCallResolution() HD branch value
    _MAX_RESOLUTION = (800, 480)
    _VIDEO_FPS = 16

    @classmethod
    def _build_video_settings(
        cls, preferred: tuple[int, int], bitrate: int = 0
    ) -> bytes:
        return struct.pack(
            "<HIHHHHBB",
            0xFFFF,
            bitrate & 0xFFFFFFFF,
            cls._MAX_RESOLUTION[0],
            cls._MAX_RESOLUTION[1],
            preferred[0],
            preferred[1],
            cls._VIDEO_FPS,
            0,
        )

    def __init__(
        self,
        client: ViperClient,
        source: str,
        target: str,
        *,
        resolution: tuple[int, int] = SD_RESOLUTION,
        bitrate: int = 0,
    ):
        self.client = client
        self.source = source
        self.target = target
        self.resolution = resolution
        self.bitrate = bitrate
        self.ctp_handle: int | None = None
        self.udp_handle: int | None = None
        self.audio_handle: int | None = None
        self.video_handle: int | None = None
        self.media_handle: int | None = None
        self.remote_media_handle: int | None = None
        # Handle the panel requests our talk-back audio on (from its audio
        # mediareq during setup); TX must use this, not our own audio_handle.
        self.audio_tx_handle: int | None = None
        self.connection = b""
        self.peer_connection = b""
        self.sequence = 0
        self.acknowledgement = 0
        self._closed = False
        # CTP sequence/ack state is touched by both the pump thread (acks) and
        # the caller (offers, voicestatus, release), so guard it.
        self._ctp_lock = threading.Lock()
        # Background media pump (lazily started by the iterators).
        self._pump_thread: threading.Thread | None = None
        # Bounded so an unconsumed stream (e.g. talk-only) can't grow without
        # bound; the pump drops the oldest packet when a queue is full.
        self._audio_q: "queue.Queue[RtpPacket | None]" = queue.Queue(maxsize=512)
        self._video_q: "queue.Queue[RtpPacket | None]" = queue.Queue(maxsize=256)
        self.audio_enabled = False
        # RTP TX state for talk-back (G.711 a-law, PT 8).
        self._tx_ssrc = struct.unpack(">I", secrets.token_bytes(4))[0]
        self._tx_seq = secrets.randbelow(0x10000)
        self._tx_ts = struct.unpack(">I", secrets.token_bytes(4))[0]
        self._tx_first = True

    def __enter__(self) -> "VideoStream":
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _send_ctp(self, body: bytes, flags: int = 0x40):
        with self._ctp_lock:
            self.client._send_frame(
                self.ctp_handle,
                build_ctp_packet(
                    flags=flags,
                    connection=self.connection,
                    sequence=self.sequence,
                    acknowledgement=self.acknowledgement,
                    body=body,
                    source=self.source,
                    destination=self.target,
                ),
            )
            if body:
                self.sequence = (self.sequence + 1) & 0xFF

    def _ack(self, packet: CtpPacket):
        with self._ctp_lock:
            self.acknowledgement = (packet.sequence + 1) & 0xFF
        self._send_ctp(b"", flags=0x00)

    def _recv_ctp(self, *, opcode: int | None = None, tries: int = 100) -> CtpPacket:
        for _ in range(tries):
            frame = self.client.recv_frame()
            if frame.handle == MGMT and frame.payload[:4] == OPEN_MAGIC:
                name, handle = self.client._accept_channel(frame)
                if name == "RTPC":
                    self.remote_media_handle = handle
                continue
            if frame.handle != self.ctp_handle:
                continue
            try:
                packet = parse_ctp_packet(frame.payload)
            except ValueError:
                continue
            if packet.connection != self.peer_connection:
                continue
            if packet.body:
                self._ack(packet)
            if opcode is None or packet.opcode == opcode:
                return packet
        expected = "a CTP packet" if opcode is None else f"CTP opcode {opcode:#06x}"
        raise TimeoutError(f"timed out waiting for {expected}")

    def open(self, *, tries: int = 100):
        if self.ctp_handle is not None:
            return
        self.ctp_handle = self.client.open_channel("CTPP", encode_logaddr(self.source))
        connection_id = secrets.randbelow(0x7FFE) + 1
        self.connection = struct.pack(">H", connection_id)
        self.peer_connection = struct.pack(">H", connection_id | 0x8000)
        self.sequence = secrets.randbelow(256)
        self.acknowledgement = secrets.randbelow(256)

        call_id = secrets.token_bytes(4)
        start = (
            struct.pack(">H", 0x0001)
            + encode_logaddr(self.source)
            + encode_logaddr(self.target)
            + b"\x01\x20"
            + call_id
            + encode_logaddr(self.source)
            + b"II"
        )
        self._send_ctp(start, flags=0xC0)
        initial = self._recv_ctp(tries=tries)
        self.acknowledgement = initial.sequence

        self.udp_handle = self.client.open_channel("UDPM")
        self._send_ctp(self._CAPABILITIES)
        self._recv_ctp(opcode=0x0003, tries=tries)
        self._recv_ctp(opcode=0x000C, tries=tries)

        self.audio_handle = self.client.open_channel("RTPC")
        self.video_handle = self.client.open_channel("RTPC")
        self.media_handle = (self.video_handle + 1) & 0xFFFF

        self._send_ctp(
            self._AUDIO_OFFER + struct.pack("<H", self.audio_handle) + b"\x00\x00"
        )
        # The panel replies with its own audio mediareq naming the handle it wants
        # our talk-back audio on (it also opens that RTPC channel). Capture it.
        panel_audio = self._recv_ctp(opcode=0x0011, tries=tries)
        if len(panel_audio.body) >= 10:
            self.audio_tx_handle = struct.unpack_from("<H", panel_audio.body, 8)[0]
        self._send_ctp(
            self._VIDEO_OFFER_PREFIX
            + struct.pack("<H", self.video_handle)
            + self._build_video_settings(self.resolution, self.bitrate)
        )

    # --- audio (G.711 a-law, RTP PT 8, on the audio RTPC channel) --------
    def enable_audio(self):
        """Ask the panel to start streaming audio (and accept talk-back).

        Mirrors ``CallFsm::start_fonica`` / ``st_out_alerting``: send a
        VOICESTATUS (CTP opcode ``0x0070``) with status ``1`` for the peer, then
        (re-)send the audio media request. Without the VOICESTATUS the panel
        accepts the audio offer but never emits any audio. Audio then arrives as
        RTP PT 8 on ``audio_handle`` over the same TCP tunnel as video.
        """
        if self.ctp_handle is None:
            raise RuntimeError("call is not open")
        if self.audio_enabled:
            return
        # Send the enable handshake single-threaded before the pump starts so the
        # CTP sequence state stays consistent (the pump also sends acks).
        self._send_voicestatus(1)
        self._send_ctp(
            self._AUDIO_OFFER + struct.pack("<H", self.audio_handle) + b"\x00\x00"
        )
        self.audio_enabled = True
        self._ensure_pump()

    def disable_audio(self):
        """Stop the audio flow: VOICESTATUS ``0`` plus an audio media-stop request."""
        if self.ctp_handle is None or not self.audio_enabled:
            return
        self.audio_enabled = False
        try:
            self._send_voicestatus(0)
            self._send_ctp(
                self._AUDIO_OFFER_STOP
                + struct.pack("<H", self.audio_handle)
                + b"\x00\x00"
            )
        except (OSError, ConnectionError):
            pass

    def _send_voicestatus(self, status: int):
        body = struct.pack(">H", 0x0070) + encode_logaddr(self.target) + bytes((status, 0))
        self._send_ctp(body, flags=0x40)

    def send_audio_pcm(self, pcm: bytes, *, paced: bool = True):
        """Send 16-bit little-endian 8 kHz mono PCM to the panel as G.711 a-law.

        The PCM is split into 20 ms (160-sample) RTP packets (PT 8) and written on
        the audio channel. ``enable_audio()`` must have been called first. With
        ``paced`` (the default) packets are emitted in real time so the panel's
        jitter buffer plays them back correctly; this blocks for the clip's
        duration. Note: talk-back plays out of the entrance-panel speaker (it
        makes noise at the door).
        """
        if not self.audio_enabled:
            raise RuntimeError("call enable_audio() before sending audio")
        frame_bytes = g711.SAMPLES_PER_FRAME * 2
        period = g711.PTIME_MS / 1000.0
        start = time.monotonic()
        sent = 0
        for off in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            self._send_audio_frame(g711.alaw_encode(pcm[off : off + frame_bytes]))
            sent += 1
            if paced:
                delay = start + sent * period - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

    def _send_audio_frame(self, alaw: bytes):
        # The panel plays our audio only on the handle it requested via its audio
        # mediareq during setup (handle_mediareq -> startAudioTX(handle)); fall
        # back to our offered handle if for some reason it never asked.
        handle = self.audio_tx_handle or self.audio_handle
        header = struct.pack(
            ">BBHII",
            0x80,
            (0x80 if self._tx_first else 0x00) | g711.PAYLOAD_TYPE,
            self._tx_seq & 0xFFFF,
            self._tx_ts & 0xFFFFFFFF,
            self._tx_ssrc,
        )
        self.client._send_frame(handle, header + alaw)
        self._tx_first = False
        self._tx_seq = (self._tx_seq + 1) & 0xFFFF
        self._tx_ts = (self._tx_ts + len(alaw)) & 0xFFFFFFFF

    # --- background media pump ------------------------------------------
    def _ensure_pump(self):
        if self.media_handle is None:
            raise RuntimeError("video stream is not open")
        if self._pump_thread is None:
            self._pump_thread = threading.Thread(target=self._pump, daemon=True)
            self._pump_thread.start()

    def _pump(self):
        video_handles = {self.media_handle, self.remote_media_handle, self.video_handle}
        try:
            while not self._closed:
                frame = self.client.recv_frame()
                if frame.handle == MGMT and frame.payload[:4] == OPEN_MAGIC:
                    name, handle = self.client._accept_channel(frame)
                    if name == "RTPC":
                        self.remote_media_handle = handle
                        video_handles.add(handle)
                    continue
                if frame.handle == self.ctp_handle:
                    try:
                        packet = parse_ctp_packet(frame.payload)
                    except ValueError:
                        continue
                    if packet.body:
                        self._ack(packet)
                    if packet.opcode == 0x000E:
                        break
                    continue
                if frame.handle == self.audio_handle:
                    target_q = self._audio_q
                elif frame.handle in video_handles:
                    target_q = self._video_q
                else:
                    continue
                try:
                    rtp = parse_rtp_packet(frame.payload)
                except ValueError:
                    continue
                try:
                    target_q.put_nowait(rtp)
                except queue.Full:
                    try:
                        target_q.get_nowait()  # drop oldest, then enqueue
                    except queue.Empty:
                        pass
                    target_q.put_nowait(rtp)
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self._closed = True
            self._signal_end()

    def _signal_end(self):
        for q in (self._audio_q, self._video_q):
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    @staticmethod
    def _drain(q: "queue.Queue[RtpPacket | None]") -> Iterator[RtpPacket]:
        while True:
            packet = q.get()
            if packet is None:
                return
            yield packet

    def packets(self) -> Iterator[RtpPacket]:
        """Yield video RTP packets until the call ends or the stream is closed."""
        self._ensure_pump()
        yield from self._drain(self._video_q)

    def h264(self) -> Iterator[bytes]:
        """Yield Annex-B H.264 NAL units."""
        depacketizer = H264Depacketizer()
        for packet in self.packets():
            yield from depacketizer.feed(packet)

    def audio_packets(self) -> Iterator[RtpPacket]:
        """Yield audio RTP packets (PT 8). Call ``enable_audio()`` first."""
        self._ensure_pump()
        yield from self._drain(self._audio_q)

    def audio(self) -> Iterator[bytes]:
        """Yield decoded 16-bit little-endian 8 kHz mono PCM from the panel."""
        for packet in self.audio_packets():
            if packet.payload_type == g711.PAYLOAD_TYPE and packet.payload:
                yield g711.alaw_decode(packet.payload)

    def close(self):
        if self._closed:
            return
        if self.audio_enabled:
            self.disable_audio()
        self._closed = True
        if self.ctp_handle is not None and self.connection:
            try:
                self._send_ctp(struct.pack(">HB", 0x000E, 0))
            except (OSError, ConnectionError):
                pass
        for handle in (
            self.audio_handle,
            self.video_handle,
            self.udp_handle,
            self.ctp_handle,
        ):
            if handle is not None:
                try:
                    self.client.close_channel(handle)
                except (OSError, ConnectionError):
                    pass
        # Unblock any iterators waiting on the queues.
        self._signal_end()
