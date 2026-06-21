import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from comelit.viper import (
    H264Depacketizer,
    OPEN_MAGIC,
    RtpPacket,
    VideoStream,
    ViperClient,
    build_ctp_packet,
    decode_logaddr,
    encode_logaddr,
    parse_ctp_packet,
    parse_rtp_packet,
)
from comelit import g711


class ViperProtocolTests(unittest.TestCase):
    def test_logaddr_round_trip(self):
        self.assertEqual(encode_logaddr("SB100001"), b"SB100001\x00\x00")
        self.assertEqual(decode_logaddr(encode_logaddr("SB0000011")), "SB0000011")

    def test_parse_captured_open_door_packet(self):
        # Captured 2026-06-20 at 16:21:46.536. Relay 1, sent to SB100001.
        raw = bytes.fromhex(
            "40 18 d4 bd ae ba 00 0d "
            "00 2d 53 42 31 30 30 30 30 31 00 00 01 "
            "00 00 00 ff ff ff ff "
            "53 42 30 30 30 30 30 31 31 00 "
            "53 42 30 30 30 30 30 31 00 00"
        )
        packet = parse_ctp_packet(raw)
        self.assertEqual(packet.flags, 0x40)
        self.assertEqual(packet.connection, b"\xd4\xbd")
        self.assertEqual(packet.sequence, 0xAE)
        self.assertEqual(packet.acknowledgement, 0xBA)
        self.assertEqual(packet.source, "SB0000011")
        self.assertEqual(packet.destination, "SB000001")
        self.assertEqual(packet.opcode, 0x002D)
        self.assertEqual(packet.open_door, ("SB100001", 1))

    def test_rejects_bad_ctp_length(self):
        with self.assertRaises(ValueError):
            parse_ctp_packet(b"\x00" * 35)

    def test_build_captured_off_call_open_door_packet(self):
        raw = build_ctp_packet(
            flags=0xC0,
            connection=bytes.fromhex("7d 90"),
            sequence=0x65,
            acknowledgement=0xF8,
            body=bytes.fromhex("00 2d") + encode_logaddr("SB100001") + b"\x01",
            source="SB0000011",
            destination="SB100001",
        )
        self.assertEqual(
            raw,
            bytes.fromhex(
                "c0 18 7d 90 65 f8 00 0d "
                "00 2d 53 42 31 30 30 30 30 31 00 00 01 "
                "00 00 00 ff ff ff ff "
                "53 42 30 30 30 30 30 31 31 00 "
                "53 42 31 30 30 30 30 31 00 00"
            ),
        )

    def test_ctpp_channel_registration_shape(self):
        client = ViperClient("unused")
        sent = []
        client._send_frame = lambda handle, payload: sent.append((handle, payload))
        client._wait_mgmt = lambda magic, handle: None
        handle = client.open_channel("CTPP", encode_logaddr("SB0000011"))
        self.assertEqual(handle, 0x7474)
        self.assertEqual(
            sent[0][1],
            OPEN_MAGIC
            + bytes.fromhex("07 00 00 00")
            + b"CTPP"
            + bytes.fromhex("74 74 00")
            + bytes.fromhex("00 0a 00 00 00")
            + encode_logaddr("SB0000011"),
        )

    def test_parse_captured_incoming_ring(self):
        raw = bytes.fromhex(
            "c0 18 54 bd b8 aa 00 28 "
            "00 01 53 42 31 30 30 30 30 31 00 00 "
            "53 42 30 30 30 30 30 31 00 00 "
            "01 00 1d b2 4a 64 "
            "53 42 31 30 30 30 30 31 00 00 "
            "50 50 "
            "ff ff ff ff "
            "53 42 30 30 30 30 30 31 00 00 "
            "53 42 30 30 30 30 30 31 31 00"
        )
        packet = parse_ctp_packet(raw)
        self.assertEqual(packet.opcode, 0x0001)
        self.assertEqual(packet.source, "SB000001")
        self.assertEqual(packet.destination, "SB0000011")
        self.assertEqual(packet.body[2:12], encode_logaddr("SB100001"))

    def test_parse_rtp_packet(self):
        raw = bytes.fromhex(
            "80 e0 12 34 01 02 03 04 aa bb cc dd 65 88 84"
        )
        packet = parse_rtp_packet(raw)
        self.assertTrue(packet.marker)
        self.assertEqual(packet.payload_type, 96)
        self.assertEqual(packet.sequence, 0x1234)
        self.assertEqual(packet.timestamp, 0x01020304)
        self.assertEqual(packet.ssrc, 0xAABBCCDD)
        self.assertEqual(packet.payload, bytes.fromhex("65 88 84"))

    def test_h264_depacketizes_single_and_fragmented_nals(self):
        depacketizer = H264Depacketizer()
        single = RtpPacket(False, 96, 1, 1, 1, b"\x67\x01", b"")
        self.assertEqual(depacketizer.feed(single), [b"\x00\x00\x00\x01\x67\x01"])

        start = RtpPacket(False, 96, 2, 2, 1, b"\x7c\x85abc", b"")
        middle = RtpPacket(False, 96, 3, 2, 1, b"\x7c\x05def", b"")
        end = RtpPacket(True, 96, 4, 2, 1, b"\x7c\x45ghi", b"")
        self.assertEqual(depacketizer.feed(start), [])
        self.assertEqual(depacketizer.feed(middle), [])
        self.assertEqual(
            depacketizer.feed(end),
            [b"\x00\x00\x00\x01\x65abcdefghi"],
        )

    def test_accept_incoming_rtpc_channel(self):
        client = ViperClient("unused")
        sent = []
        client._send_frame = lambda handle, payload: sent.append((handle, payload))
        request = (
            OPEN_MAGIC
            + struct.pack("<I", 7)
            + b"RTPC"
            + struct.pack("<H", 0xC1AC)
            + b"\x01"
        )
        name, handle = client._accept_channel(type("F", (), {"handle": 0, "payload": request})())
        self.assertEqual((name, handle), ("RTPC", 0xC1AC))
        self.assertEqual(sent[0][0], 0)
        self.assertEqual(sent[0][1][-4:], bytes.fromhex("ac c1 00 00"))

    def test_audio_offer_shape(self):
        # csp_send_mediareq: opcode 0x0011, media-type 0x18 (audio), flags 0x02,
        # zeroed [4:8], then the RTPC handle. Matches the app's captured offer.
        body = (
            VideoStream._AUDIO_OFFER + struct.pack("<H", 0x747E) + b"\x00\x00"
        )
        self.assertEqual(body, bytes.fromhex("00 11 18 02 00 00 00 00 7e 74 00 00"))
        # Media-type bit 0x80 set = stop.
        self.assertEqual(VideoStream._AUDIO_OFFER_STOP[2], 0x98)

    def test_voicestatus_shape(self):
        # csp_send_voicestatus: opcode 0x0070, peer logaddr, status, 0.
        body = struct.pack(">H", 0x0070) + encode_logaddr("SB100001") + bytes((1, 0))
        self.assertEqual(
            body, bytes.fromhex("00 70 53 42 31 30 30 30 30 31 00 00 01 00")
        )
        self.assertEqual(len(body), 14)

    def test_g711_alaw_round_trip(self):
        # Decode is bit-exact to ITU/ffmpeg; encode is standard ITU a-law. A
        # decode->encode->decode round trip is stable (idempotent quantization).
        alaw = bytes(range(256))
        pcm = g711.alaw_decode(alaw)
        self.assertEqual(len(pcm), 512)
        self.assertEqual(g711.alaw_decode(g711.alaw_encode(pcm)), pcm)

    def test_g711_frame_size(self):
        self.assertEqual(g711.SAMPLES_PER_FRAME, 160)
        self.assertEqual(g711.PAYLOAD_TYPE, 8)
        self.assertEqual(g711.SAMPLE_RATE, 8000)

    def test_captured_video_offer_shape(self):
        body = (
            VideoStream._VIDEO_OFFER_PREFIX
            + struct.pack("<H", 0x747F)
            + VideoStream._build_video_settings(VideoStream.SD_RESOLUTION, 0)
        )
        self.assertEqual(
            body,
            bytes.fromhex(
                "00 11 14 32 00 00 00 00 "
                "7f 74 ff ff 00 00 00 00 "
                "20 03 e0 01 40 01 f0 00 10 00"
            ),
        )
