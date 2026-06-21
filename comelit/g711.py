"""G.711 A-law (PCMA, RTP payload type 8) codec — the ViP audio wire format.

The Comelit ViP entrance panel streams and accepts audio as G.711 A-law: 8 kHz,
mono, 8-bit samples (one byte per sample). The only audio codec compiled into
``libvipcomelit.so`` is ``Comelit::Utils::AlawUtils`` (a-law); confirmed live, the
panel sends RTP PT=8 with 160-byte payloads at a 160-sample (20 ms) timestamp
increment.

``alaw_decode`` is byte-identical to ffmpeg's ``pcm_alaw`` decoder. ``alaw_encode``
is the ITU-T G.711 reference encoder (standard a-law, tolerant decoders included).
PCM here is signed 16-bit little-endian, the format the rest of the world uses for
WAV/`av`.
"""
from __future__ import annotations
import struct

PAYLOAD_TYPE = 8       # RTP PT for PCMA (G.711 a-law)
SAMPLE_RATE = 8000     # Hz, mono
PTIME_MS = 20          # panel uses 20 ms packets
SAMPLES_PER_FRAME = SAMPLE_RATE * PTIME_MS // 1000  # 160


def _build_decode_table() -> list[int]:
    table = []
    for a in range(256):
        v = a ^ 0x55
        sign = v & 0x80
        exponent = (v & 0x70) >> 4
        mantissa = v & 0x0F
        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        table.append(sample if sign else -sample)
    return table


_DECODE = _build_decode_table()
_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _encode_sample(pcm_val: int) -> int:
    pcm_val >>= 3
    if pcm_val >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm_val = -pcm_val - 1
    seg = 8
    for i, bound in enumerate(_SEG_AEND):
        if pcm_val <= bound:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm_val >> 1) & 0x0F
    else:
        aval |= (pcm_val >> seg) & 0x0F
    return aval ^ mask


_ENCODE = bytes(_encode_sample(s) for s in range(-32768, 32768))


def alaw_decode(data: bytes) -> bytes:
    """Decode A-law bytes to signed 16-bit little-endian PCM."""
    return struct.pack(f"<{len(data)}h", *(_DECODE[b] for b in data))


def alaw_encode(pcm: bytes) -> bytes:
    """Encode signed 16-bit little-endian PCM to A-law bytes."""
    if len(pcm) & 1:
        raise ValueError("PCM length must be a whole number of 16-bit samples")
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return bytes(_ENCODE[s + 32768] for s in samples)
